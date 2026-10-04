"""SQLite persistence: drafts, channels, scheduled posts."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import aiosqlite

from .config import cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    chat_id  INTEGER PRIMARY KEY,
    username TEXT,
    title    TEXT NOT NULL,
    type     TEXT NOT NULL,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    user_id    INTEGER PRIMARY KEY,
    data       TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    chat_id       INTEGER NOT NULL,
    data          TEXT NOT NULL,
    run_at        REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    result_msg_id INTEGER,
    error         TEXT,
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_due ON posts (status, run_at);
"""


def default_options() -> dict[str, Any]:
    return {
        "link_preview": True,
        "protect": False,
        "silent": False,
        "paid": False,
        "effect": False,
        "effect_id": cfg.effect_id,
    }


@dataclass
class Draft:
    """A post being composed by a user."""

    text: str = ""
    mode: str = "auto"  # auto | html | markdown
    buttons: list[dict[str, str]] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=default_options)
    awaiting: str | None = None  # "button" -> next text parses as "Label | url"
    prev_msg_id: int | None = None
    updated_at: float = 0.0

    @property
    def resolved_mode(self) -> str:
        from .rich import resolve_mode  # local import: rich needs Draft for keyboards

        return resolve_mode(self.text, self.mode)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Draft":
        data = json.loads(raw)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class Storage:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or cfg.database
        self.db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    def _conn(self) -> aiosqlite.Connection:
        if self.db is None:
            raise RuntimeError("Storage is not connected")
        return self.db

    # ---------------------------------------------------------------- drafts
    async def get_draft(self, user_id: int) -> Draft | None:
        cur = await self._conn().execute(
            "SELECT data FROM drafts WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return Draft.from_json(row["data"]) if row else None

    async def save_draft(self, user_id: int, draft: Draft) -> None:
        draft.updated_at = time.time()
        await self._conn().execute(
            "INSERT INTO drafts (user_id, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, "
            "updated_at = excluded.updated_at",
            (user_id, draft.to_json(), draft.updated_at),
        )
        await self._conn().commit()

    async def clear_draft(self, user_id: int) -> None:
        await self._conn().execute("DELETE FROM drafts WHERE user_id = ?", (user_id,))
        await self._conn().commit()

    # -------------------------------------------------------------- channels
    async def upsert_channel(
        self, chat_id: int, title: str, username: str | None, chat_type: str
    ) -> None:
        await self._conn().execute(
            "INSERT INTO channels (chat_id, username, title, type, added_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET username = excluded.username, "
            "title = excluded.title, type = excluded.type",
            (chat_id, username, title, chat_type, time.time()),
        )
        await self._conn().commit()

    async def list_channels(self) -> list[dict[str, Any]]:
        cur = await self._conn().execute(
            "SELECT chat_id, username, title, type FROM channels ORDER BY added_at"
        )
        return [dict(r) for r in await cur.fetchall()]

    async def delete_channel(self, chat_id: int) -> None:
        await self._conn().execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
        await self._conn().commit()

    # ----------------------------------------------------------------- posts
    async def add_post(
        self, user_id: int, chat_id: int, draft: Draft, run_at: float
    ) -> int:
        payload = json.dumps(
            {
                "text": draft.text,
                "mode": draft.mode,
                "buttons": draft.buttons,
                "options": draft.options,
            },
            ensure_ascii=False,
        )
        cur = await self._conn().execute(
            "INSERT INTO posts (user_id, chat_id, data, run_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, chat_id, payload, run_at, time.time()),
        )
        await self._conn().commit()
        return int(cur.lastrowid)

    async def due_posts(self, now: float) -> list[dict[str, Any]]:
        cur = await self._conn().execute(
            "SELECT * FROM posts WHERE status = 'pending' AND run_at <= ? "
            "ORDER BY run_at LIMIT 20",
            (now,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def pending_posts(self, user_id: int) -> list[dict[str, Any]]:
        cur = await self._conn().execute(
            "SELECT * FROM posts WHERE status = 'pending' AND user_id = ? "
            "ORDER BY run_at",
            (user_id,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def finish_post(
        self, post_id: int, status: str, message_id: int | None = None, error: str | None = None
    ) -> None:
        await self._conn().execute(
            "UPDATE posts SET status = ?, result_msg_id = ?, error = ? WHERE id = ?",
            (status, message_id, error, post_id),
        )
        await self._conn().commit()

    async def cancel_post(self, post_id: int, user_id: int) -> bool:
        cur = await self._conn().execute(
            "UPDATE posts SET status = 'cancelled' "
            "WHERE id = ? AND user_id = ? AND status = 'pending'",
            (post_id, user_id),
        )
        await self._conn().commit()
        return cur.rowcount > 0

    async def stats(self) -> dict[str, int]:
        conn = self._conn()
        out: dict[str, int] = {}
        for key, sql in (
            ("channels", "SELECT COUNT(*) AS n FROM channels"),
            ("pending", "SELECT COUNT(*) AS n FROM posts WHERE status='pending'"),
            ("published", "SELECT COUNT(*) AS n FROM posts WHERE status='done'"),
            ("drafts", "SELECT COUNT(*) AS n FROM drafts"),
        ):
            cur = await conn.execute(sql)
            row = await cur.fetchone()
            out[key] = int(row["n"])
        return out
