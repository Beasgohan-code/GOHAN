"""SQLite storage (aiosqlite).

One file, no server, no migrations folder - the schema is created on first use
and every statement is idempotent. WAL mode keeps reads from blocking the single
writer, which is plenty for a moderation/games bot.

Tables
------
``users``        everyone who ever started the bot (also feeds the daily report)
``chats``        groups/channels the bot is in, with per-chat settings as JSON
``warnings``     moderation warnings, one row per warning
``notes``        per-chat saved notes (``/save``, ``#note``)
``scores``       game scores - the basis of every leaderboard
``events``       append-only event log (bans, joins, watchdog runs, …)
``kv``           small key/value store for bot-wide state (maintenance, …)

All public methods are coroutines and safe to call from handlers directly.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .logging_setup import get_logger

log = get_logger("storage")

__all__ = ["Database", "Score", "UserRecord", "WarningRecord", "now"]

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    first_name    TEXT,
    language_code TEXT,
    is_premium    INTEGER DEFAULT 0,
    is_bot        INTEGER DEFAULT 0,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    is_banned     INTEGER DEFAULT 0,
    start_count   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT,
    type        TEXT,
    username    TEXT,
    added_by    INTEGER,
    added_at    REAL NOT NULL,
    settings    TEXT NOT NULL DEFAULT '{}',
    is_active   INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS warnings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    admin_id   INTEGER,
    reason     TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_warnings_chat_user ON warnings(chat_id, user_id);

CREATE TABLE IF NOT EXISTS notes (
    chat_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    content    TEXT,
    file_id    TEXT,
    media_type TEXT,
    created_by INTEGER,
    created_at REAL NOT NULL,
    PRIMARY KEY (chat_id, name)
);

CREATE TABLE IF NOT EXISTS scores (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL,
    game      TEXT NOT NULL,
    user_id   INTEGER NOT NULL,
    user_name TEXT,
    score     INTEGER NOT NULL,
    meta      TEXT,
    played_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_board ON scores(chat_id, game, score DESC);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    tag     TEXT NOT NULL,
    chat_id INTEGER,
    user_id INTEGER,
    data    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS filters (
    chat_id    INTEGER NOT NULL,
    trigger    TEXT NOT NULL,
    reply      TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'text',
    file_id    TEXT,
    created_by INTEGER,
    created_at REAL NOT NULL,
    uses       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, trigger)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    is_public  INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playlists_owner ON playlists(owner_id);

CREATE TABLE IF NOT EXISTS playlist_tracks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    duration    INTEGER,
    video_id    TEXT,
    uploader    TEXT,
    thumbnail   TEXT,
    source      TEXT DEFAULT 'youtube',
    added_by    INTEGER,
    added_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_playlist_tracks ON playlist_tracks(playlist_id, position);

CREATE TABLE IF NOT EXISTS afk (
    user_id  INTEGER NOT NULL,
    chat_id  INTEGER NOT NULL,
    reason   TEXT,
    since    REAL NOT NULL,
    PRIMARY KEY (user_id, chat_id)
);

CREATE TABLE IF NOT EXISTS voice_auth (
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    added_by INTEGER,
    added_at REAL NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
"""


def now() -> float:
    return time.time()


@dataclass(slots=True)
class UserRecord:
    user_id: int
    username: str | None
    first_name: str | None
    language_code: str | None
    is_premium: bool
    is_banned: bool
    first_seen: float
    last_seen: float
    start_count: int

    @property
    def display(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.first_name or str(self.user_id)


@dataclass(slots=True)
class WarningRecord:
    id: int
    chat_id: int
    user_id: int
    admin_id: int | None
    reason: str | None
    created_at: float


@dataclass(slots=True)
class Score:
    user_id: int
    user_name: str | None
    score: int
    played_at: float


class Database:
    """Async SQLite wrapper. Use :meth:`connect` once at startup."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> Database:
        if self._db is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        log.info("database ready at %s", self.path)
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Database:
        return await self.connect()

    async def __aexit__(self, *exc: Any) -> bool:
        await self.close()
        return False

    @property
    def connected(self) -> bool:
        return self._db is not None

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database.connect() was never awaited")
        return self._db

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self._lock:
            await self._conn().execute(sql, params)
            await self._conn().commit()

    async def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run an ``INSERT`` and return ``lastrowid``."""
        async with self._lock:
            cursor = await self._conn().execute(sql, params)
            await self._conn().commit()
            return int(cursor.lastrowid or 0)

    async def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        """Run the same statement for many parameter tuples."""
        if not rows:
            return 0
        async with self._lock:
            await self._conn().executemany(sql, rows)
            await self._conn().commit()
        return len(rows)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._conn().execute(sql, params) as cursor:
            return list(await cursor.fetchall())

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self._conn().execute(sql, params) as cursor:
            return await cursor.fetchone()

    async def fetch_value(self, sql: str, params: Sequence[Any] = (), default: Any = 0) -> Any:
        row = await self.fetch_one(sql, params)
        if row is None or row[0] is None:
            return default
        return row[0]

    # -- users ---------------------------------------------------------------

    async def upsert_user(
        self,
        user_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        language_code: str | None = None,
        is_premium: bool = False,
        is_bot: bool = False,
        counted_start: bool = False,
    ) -> None:
        """Insert or refresh a user row. ``counted_start`` bumps ``start_count``."""
        ts = now()
        await self.execute(
            """
            INSERT INTO users (user_id, username, first_name, language_code, is_premium,
                               is_bot, first_seen, last_seen, start_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username      = COALESCE(excluded.username, users.username),
                first_name    = COALESCE(excluded.first_name, users.first_name),
                language_code = COALESCE(excluded.language_code, users.language_code),
                is_premium    = excluded.is_premium,
                last_seen     = excluded.last_seen,
                start_count   = users.start_count + ?
            """,
            (
                user_id,
                username,
                first_name,
                language_code,
                int(is_premium),
                int(is_bot),
                ts,
                ts,
                1 if counted_start else 0,
                1 if counted_start else 0,
            ),
        )

    async def get_user(self, user_id: int) -> UserRecord | None:
        row = await self.fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        if row is None:
            return None
        return UserRecord(
            user_id=row["user_id"],
            username=row["username"],
            first_name=row["first_name"],
            language_code=row["language_code"],
            is_premium=bool(row["is_premium"]),
            is_banned=bool(row["is_banned"]),
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            start_count=row["start_count"],
        )

    async def total_users(self) -> int:
        return int(await self.fetch_value("SELECT COUNT(*) FROM users"))

    async def users_since(self, ts: float) -> int:
        return int(await self.fetch_value("SELECT COUNT(*) FROM users WHERE first_seen >= ?", (ts,)))

    async def active_since(self, ts: float) -> int:
        return int(await self.fetch_value("SELECT COUNT(*) FROM users WHERE last_seen >= ?", (ts,)))

    async def set_banned(self, user_id: int, banned: bool = True) -> None:
        await self.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (int(banned), user_id))

    async def is_banned(self, user_id: int) -> bool:
        return bool(await self.fetch_value("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)))

    async def banned_users(self) -> list[int]:
        rows = await self.fetch_all("SELECT user_id FROM users WHERE is_banned = 1")
        return [row["user_id"] for row in rows]

    # -- chats ---------------------------------------------------------------

    async def upsert_chat(
        self,
        chat_id: int,
        *,
        title: str | None = None,
        type: str | None = None,
        username: str | None = None,
        added_by: int | None = None,
    ) -> None:
        await self.execute(
            """
            INSERT INTO chats (chat_id, title, type, username, added_by, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title    = COALESCE(excluded.title, chats.title),
                type     = COALESCE(excluded.type, chats.type),
                username = COALESCE(excluded.username, chats.username),
                is_active = 1
            """,
            (chat_id, title, type, username, added_by, now()),
        )

    async def remove_chat(self, chat_id: int) -> None:
        await self.execute("UPDATE chats SET is_active = 0 WHERE chat_id = ?", (chat_id,))

    async def get_chat_settings(self, chat_id: int) -> dict[str, Any]:
        raw = await self.fetch_value("SELECT settings FROM chats WHERE chat_id = ?", (chat_id,), "{}")
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    async def set_chat_setting(self, chat_id: int, key: str, value: Any) -> dict[str, Any]:
        """Set one key of a chat's settings blob and return the new blob."""
        settings = await self.get_chat_settings(chat_id)
        settings[key] = value
        await self.execute(
            "UPDATE chats SET settings = ? WHERE chat_id = ?",
            (json.dumps(settings, ensure_ascii=False), chat_id),
        )
        return settings

    async def update_chat_settings(self, chat_id: int, values: dict[str, Any]) -> dict[str, Any]:
        settings = await self.get_chat_settings(chat_id)
        settings.update(values)
        await self.execute(
            "UPDATE chats SET settings = ? WHERE chat_id = ?",
            (json.dumps(settings, ensure_ascii=False), chat_id),
        )
        return settings

    async def all_chats(self) -> list[aiosqlite.Row]:
        return await self.fetch_all("SELECT * FROM chats WHERE is_active = 1")

    async def count_chats(self) -> int:
        return int(await self.fetch_value("SELECT COUNT(*) FROM chats WHERE is_active = 1"))


    # -- filters (trigger -> reply) ------------------------------------------

    async def save_filter(
        self,
        chat_id: int,
        trigger: str,
        reply: str,
        *,
        kind: str = "text",
        file_id: str | None = None,
        created_by: int | None = None,
    ) -> None:
        """Create or replace a filter. Triggers are stored lower-case."""
        await self.execute(
            """
            INSERT INTO filters (chat_id, trigger, reply, kind, file_id, created_by, created_at, uses)
            VALUES (?,?,?,?,?,?,?,0)
            ON CONFLICT(chat_id, trigger) DO UPDATE SET
                reply = excluded.reply, kind = excluded.kind, file_id = excluded.file_id,
                created_by = excluded.created_by, created_at = excluded.created_at
            """,
            (chat_id, trigger.lstrip("/#").strip().lower(), reply, kind, file_id, created_by, now()),
        )

    async def get_filter(self, chat_id: int, trigger: str) -> aiosqlite.Row | None:
        return await self.fetch_one(
            "SELECT * FROM filters WHERE chat_id = ? AND trigger = ?",
            (chat_id, trigger.lstrip("/#").strip().lower()),
        )

    async def list_filters(self, chat_id: int) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            "SELECT * FROM filters WHERE chat_id = ? ORDER BY uses DESC, trigger ASC", (chat_id,)
        )

    async def count_filters(self, chat_id: int) -> int:
        return int(
            await self.fetch_value("SELECT COUNT(*) FROM filters WHERE chat_id = ?", (chat_id,))
        )

    async def delete_filter(self, chat_id: int, trigger: str) -> bool:
        row = await self.get_filter(chat_id, trigger)
        if row is None:
            return False
        await self.execute(
            "DELETE FROM filters WHERE chat_id = ? AND trigger = ?",
            (chat_id, trigger.lstrip("/#").strip().lower()),
        )
        return True

    async def clear_filters(self, chat_id: int) -> int:
        count = await self.count_filters(chat_id)
        await self.execute("DELETE FROM filters WHERE chat_id = ?", (chat_id,))
        return count

    async def bump_filter_uses(self, chat_id: int, trigger: str) -> None:
        await self.execute(
            "UPDATE filters SET uses = uses + 1 WHERE chat_id = ? AND trigger = ?",
            (chat_id, trigger.lstrip("/#").strip().lower()),
        )

    async def top_filters(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            "SELECT trigger, COUNT(*) AS triggers FROM filters GROUP BY trigger "
            "ORDER BY triggers DESC LIMIT ?",
            (limit,),
        )

    # -- listings used by the owner panel -------------------------------------

    async def recent_chats(self, limit: int = 10, *, active_only: bool = True) -> list[aiosqlite.Row]:
        where = "WHERE is_active = 1" if active_only else ""
        return await self.fetch_all(
            f"SELECT * FROM chats {where} ORDER BY added_at DESC LIMIT ?", (limit,)
        )

    async def recent_users(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            "SELECT * FROM users ORDER BY last_seen DESC LIMIT ?", (limit,)
        )

    async def set_chat_active(self, chat_id: int, active: bool) -> None:
        await self.execute(
            "UPDATE chats SET is_active = ? WHERE chat_id = ?", (int(active), chat_id)
        )

    async def chat_record(self, chat_id: int) -> aiosqlite.Row | None:
        return await self.fetch_one("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))

    # -- warnings ------------------------------------------------------------

    async def add_warning(
        self, chat_id: int, user_id: int, *, admin_id: int | None = None, reason: str | None = None
    ) -> int:
        await self.execute(
            "INSERT INTO warnings (chat_id, user_id, admin_id, reason, created_at) VALUES (?,?,?,?,?)",
            (chat_id, user_id, admin_id, reason, now()),
        )
        return await self.count_warnings(chat_id, user_id)

    async def count_warnings(self, chat_id: int, user_id: int) -> int:
        return int(
            await self.fetch_value(
                "SELECT COUNT(*) FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
            )
        )

    async def warnings_of(self, chat_id: int, user_id: int) -> list[WarningRecord]:
        rows = await self.fetch_all(
            "SELECT * FROM warnings WHERE chat_id = ? AND user_id = ? ORDER BY created_at DESC",
            (chat_id, user_id),
        )
        return [
            WarningRecord(
                id=row["id"],
                chat_id=row["chat_id"],
                user_id=row["user_id"],
                admin_id=row["admin_id"],
                reason=row["reason"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def clear_warnings(self, chat_id: int, user_id: int) -> int:
        count = await self.count_warnings(chat_id, user_id)
        await self.execute(
            "DELETE FROM warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        return count

    async def top_warned(self, chat_id: int, limit: int = 10) -> list[tuple[int, int]]:
        rows = await self.fetch_all(
            """
            SELECT user_id, COUNT(*) AS n FROM warnings
            WHERE chat_id = ? GROUP BY user_id ORDER BY n DESC LIMIT ?
            """,
            (chat_id, limit),
        )
        return [(row["user_id"], row["n"]) for row in rows]


    # -- moderation queries (used by the web panel) --------------------------

    async def search_users(self, query: str = "", *, limit: int = 25, banned: bool | None = None) -> list[aiosqlite.Row]:
        """Find users by id, username or name - newest activity first."""
        clauses: list[str] = []
        params: list[Any] = []
        needle = (query or "").strip()
        if needle:
            if needle.isdigit():
                clauses.append("(user_id = ? OR CAST(user_id AS TEXT) LIKE ?)")
                params.extend([int(needle), f"{needle}%"])
            else:
                clauses.append(
                    "(LOWER(username) LIKE ? OR LOWER(first_name) LIKE ?)"
                )
                like = f"%{needle.lstrip('@').lower()}%"
                params.extend([like, like])
        if banned is not None:
            clauses.append("is_banned = ?")
            params.append(int(banned))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 200)))
        return await self.fetch_all(
            f"SELECT * FROM users {where} ORDER BY last_seen DESC LIMIT ?", tuple(params)
        )

    async def warnings_by_user(self, user_id: int, *, limit: int = 100) -> list[aiosqlite.Row]:
        """A user's warnings with the chat title joined in, newest first."""
        return await self.fetch_all(
            """
            SELECT w.*, c.title AS chat_title, c.username AS chat_username
            FROM warnings w LEFT JOIN chats c ON c.chat_id = w.chat_id
            WHERE w.user_id = ?
            ORDER BY w.created_at DESC LIMIT ?
            """,
            (user_id, max(1, min(int(limit), 500))),
        )

    async def recent_warnings(
        self,
        *,
        limit: int = 50,
        chat_id: int | None = None,
        query: str = "",
        days: int | None = None,
    ) -> list[aiosqlite.Row]:
        """Warnings across every chat, newest first, with names joined in."""
        clauses: list[str] = []
        params: list[Any] = []
        if chat_id is not None:
            clauses.append("w.chat_id = ?")
            params.append(chat_id)
        if days:
            clauses.append("w.created_at > ?")
            params.append(now() - days * 86400)
        if query:
            like = f"%{query.lower()}%"
            clauses.append("(LOWER(w.reason) LIKE ? OR LOWER(u.username) LIKE ? OR LOWER(u.first_name) LIKE ? OR CAST(w.user_id AS TEXT) LIKE ?)")
            params.extend([like, like, like, f"%{query}%"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        return await self.fetch_all(
            f"""
            SELECT w.*, u.username, u.first_name, c.title AS chat_title
            FROM warnings w
            LEFT JOIN users u ON u.user_id = w.user_id
            LEFT JOIN chats c ON c.chat_id = w.chat_id
            {where}
            ORDER BY w.created_at DESC LIMIT ?
            """,
            tuple(params),
        )

    async def warnings_per_day(self, days: int = 7) -> list[tuple[float, int]]:
        """``(day_start_timestamp, count)`` for the moderation chart."""
        since = now() - days * 86400
        rows = await self.fetch_all(
            "SELECT CAST(created_at / 86400 AS INTEGER) AS day, COUNT(*) AS n "
            "FROM warnings WHERE created_at > ? GROUP BY day ORDER BY day",
            (since,),
        )
        return [(float(row["day"]) * 86400, int(row["n"])) for row in rows]

    async def top_offenders(self, *, limit: int = 10, days: int = 30) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            """
            SELECT w.user_id, COUNT(*) AS n, u.username, u.first_name,
                   COUNT(DISTINCT w.chat_id) AS chats
            FROM warnings w LEFT JOIN users u ON u.user_id = w.user_id
            WHERE w.created_at > ?
            GROUP BY w.user_id ORDER BY n DESC LIMIT ?
            """,
            (now() - days * 86400, max(1, min(int(limit), 100))),
        )

    async def top_warning_chats(self, *, limit: int = 8, days: int = 30) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            """
            SELECT w.chat_id, COUNT(*) AS n, c.title
            FROM warnings w LEFT JOIN chats c ON c.chat_id = w.chat_id
            WHERE w.created_at > ?
            GROUP BY w.chat_id ORDER BY n DESC LIMIT ?
            """,
            (now() - days * 86400, max(1, min(int(limit), 50))),
        )

    async def clear_all_warnings(self, user_id: int) -> int:
        count = int(await self.fetch_value("SELECT COUNT(*) FROM warnings WHERE user_id = ?", (user_id,)))
        await self.execute("DELETE FROM warnings WHERE user_id = ?", (user_id,))
        return count

    async def user_groups(self, user_id: int, *, limit: int = 20) -> list[aiosqlite.Row]:
        """Chats where this user has been warned, or that they administrate."""
        return await self.fetch_all(
            """
            SELECT DISTINCT c.chat_id, c.title,
                   (SELECT COUNT(*) FROM warnings w WHERE w.chat_id = c.chat_id AND w.user_id = ?) AS warnings
            FROM chats c
            WHERE warnings > 0
            ORDER BY warnings DESC LIMIT ?
            """,
            (user_id, max(1, min(int(limit), 100))),
        )

    async def scores_of(self, user_id: int, *, limit: int = 12) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            "SELECT game, chat_id, MAX(score) AS best FROM scores WHERE user_id = ? "
            "GROUP BY game, chat_id ORDER BY best DESC LIMIT ?",
            (user_id, max(1, min(int(limit), 50))),
        )

    async def chats_with_module_key(self, key: str) -> list[int]:
        """Every active chat, for bulk module changes."""
        rows = await self.fetch_all("SELECT chat_id FROM chats WHERE is_active = 1")
        return [int(row["chat_id"]) for row in rows]


    # -- playlists (music) ---------------------------------------------------

    async def create_playlist(self, owner_id: int, name: str, *, is_public: bool = False) -> int:
        stamp = now()
        return await self.insert(
            "INSERT INTO playlists (owner_id, name, is_public, created_at, updated_at) VALUES (?,?,?,?,?)",
            (owner_id, name, int(is_public), stamp, stamp),
        )

    async def playlists_of(self, owner_id: int, *, limit: int = 50) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            """
            SELECT p.*, (SELECT COUNT(*) FROM playlist_tracks t WHERE t.playlist_id = p.id) AS tracks
            FROM playlists p WHERE p.owner_id = ? ORDER BY p.updated_at DESC LIMIT ?
            """,
            (owner_id, max(1, min(int(limit), 200))),
        )

    async def all_playlists(self, *, limit: int = 200) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            """
            SELECT p.*, u.username, u.first_name,
                   (SELECT COUNT(*) FROM playlist_tracks t WHERE t.playlist_id = p.id) AS tracks,
                   (SELECT COALESCE(SUM(t.duration), 0) FROM playlist_tracks t WHERE t.playlist_id = p.id) AS duration
            FROM playlists p LEFT JOIN users u ON u.user_id = p.owner_id
            ORDER BY p.updated_at DESC LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        )

    async def playlist(self, playlist_id: int) -> aiosqlite.Row | None:
        return await self.fetch_one("SELECT * FROM playlists WHERE id = ?", (playlist_id,))

    async def find_playlist(self, owner_id: int, name: str) -> aiosqlite.Row | None:
        return await self.fetch_one(
            "SELECT * FROM playlists WHERE owner_id = ? AND LOWER(name) = ?",
            (owner_id, str(name).strip().lower()),
        )

    async def rename_playlist(self, playlist_id: int, name: str) -> bool:
        await self.execute(
            "UPDATE playlists SET name = ?, updated_at = ? WHERE id = ?", (name, now(), playlist_id)
        )
        row = await self.playlist(playlist_id)
        return row is not None

    async def delete_playlist(self, playlist_id: int) -> int:
        count = await self.playlist_track_count(playlist_id)
        await self.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        await self.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        return count

    async def add_playlist_tracks(
        self, playlist_id: int, tracks: Sequence[dict[str, Any]], *, added_by: int | None = None
    ) -> int:
        if not tracks:
            return 0
        start = int(
            await self.fetch_value(
                "SELECT COALESCE(MAX(position), 0) FROM playlist_tracks WHERE playlist_id = ?",
                (playlist_id,),
                0,
            )
        )
        stamp = now()
        rows = [
            (
                playlist_id,
                start + index,
                str(track.get("title") or "unknown")[:300],
                str(track.get("url") or ""),
                track.get("duration"),
                track.get("video_id"),
                track.get("uploader"),
                track.get("thumbnail"),
                track.get("source") or "youtube",
                added_by,
                stamp,
            )
            for index, track in enumerate(tracks, start=1)
        ]
        await self.executemany(
            "INSERT INTO playlist_tracks (playlist_id, position, title, url, duration, video_id,"
            " uploader, thumbnail, source, added_by, added_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        await self.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (stamp, playlist_id))
        return len(rows)

    async def playlist_tracks(
        self, playlist_id: int, *, offset: int = 0, limit: int = 500
    ) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            "SELECT * FROM playlist_tracks WHERE playlist_id = ? ORDER BY position LIMIT ? OFFSET ?",
            (playlist_id, max(1, min(int(limit), 1000)), max(0, int(offset))),
        )

    async def playlist_track_count(self, playlist_id: int) -> int:
        return int(
            await self.fetch_value(
                "SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,)
            )
        )

    async def remove_playlist_track(self, playlist_id: int, position: int) -> aiosqlite.Row | None:
        """Remove the 1-based ``position`` entry of a playlist."""
        row = await self.fetch_one(
            "SELECT * FROM playlist_tracks WHERE playlist_id = ? ORDER BY position LIMIT 1 OFFSET ?",
            (playlist_id, max(0, int(position) - 1)),
        )
        if row is None:
            return None
        await self.execute("DELETE FROM playlist_tracks WHERE id = ?", (row["id"],))
        await self.execute("UPDATE playlists SET updated_at = ? WHERE id = ?", (now(), playlist_id))
        return row

    async def clear_playlist(self, playlist_id: int) -> int:
        count = await self.playlist_track_count(playlist_id)
        await self.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
        return count

    # -- afk -----------------------------------------------------------------

    async def set_afk(self, user_id: int, chat_id: int, reason: str | None = None) -> None:
        await self.execute(
            "INSERT INTO afk (user_id, chat_id, reason, since) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, chat_id) DO UPDATE SET reason = excluded.reason, since = excluded.since",
            (user_id, chat_id, reason, now()),
        )

    async def afk_entry(self, user_id: int, chat_id: int) -> aiosqlite.Row | None:
        return await self.fetch_one(
            "SELECT * FROM afk WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)
        )

    async def clear_afk(self, user_id: int, chat_id: int) -> bool:
        row = await self.afk_entry(user_id, chat_id)
        if row is None:
            return False
        await self.execute("DELETE FROM afk WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
        return True

    async def afk_in_chat(self, chat_id: int) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            "SELECT a.*, u.first_name, u.username FROM afk a LEFT JOIN users u ON u.user_id = a.user_id "
            "WHERE a.chat_id = ? ORDER BY a.since DESC LIMIT 100",
            (chat_id,),
        )

    async def all_afk(self, *, limit: int = 200) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            "SELECT a.*, u.first_name, u.username, c.title AS chat_title FROM afk a "
            "LEFT JOIN users u ON u.user_id = a.user_id LEFT JOIN chats c ON c.chat_id = a.chat_id "
            "ORDER BY a.since DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        )

    async def count_afk(self) -> int:
        return int(await self.fetch_value("SELECT COUNT(*) FROM afk"))

    # -- voice controllers ---------------------------------------------------

    async def allow_voice(self, chat_id: int, user_id: int, *, added_by: int | None = None) -> bool:
        existing = await self.is_voice_allowed(chat_id, user_id)
        await self.execute(
            "INSERT INTO voice_auth (chat_id, user_id, added_by, added_at) VALUES (?,?,?,?) "
            "ON CONFLICT(chat_id, user_id) DO NOTHING",
            (chat_id, user_id, added_by, now()),
        )
        return not existing

    async def deny_voice(self, chat_id: int, user_id: int) -> bool:
        existed = await self.is_voice_allowed(chat_id, user_id)
        await self.execute("DELETE FROM voice_auth WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        return existed

    async def voice_allowed(self, chat_id: int) -> list[int]:
        rows = await self.fetch_all(
            "SELECT user_id FROM voice_auth WHERE chat_id = ? ORDER BY added_at", (chat_id,)
        )
        return [int(row["user_id"]) for row in rows]

    async def is_voice_allowed(self, chat_id: int, user_id: int) -> bool:
        row = await self.fetch_one(
            "SELECT 1 FROM voice_auth WHERE chat_id = ? AND user_id = ?", (chat_id, user_id)
        )
        return row is not None

    async def voice_controllers(self, *, limit: int = 200) -> list[aiosqlite.Row]:
        return await self.fetch_all(
            "SELECT v.*, u.first_name, u.username, c.title AS chat_title FROM voice_auth v "
            "LEFT JOIN users u ON u.user_id = v.user_id LEFT JOIN chats c ON c.chat_id = v.chat_id "
            "ORDER BY v.added_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        )

    # -- notes ---------------------------------------------------------------

    async def save_note(
        self,
        chat_id: int,
        name: str,
        content: str | None,
        *,
        file_id: str | None = None,
        media_type: str | None = None,
        created_by: int | None = None,
    ) -> None:
        await self.execute(
            """
            INSERT INTO notes (chat_id, name, content, file_id, media_type, created_by, created_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(chat_id, name) DO UPDATE SET
                content = excluded.content, file_id = excluded.file_id,
                media_type = excluded.media_type, created_by = excluded.created_by,
                created_at = excluded.created_at
            """,
            (chat_id, name.lstrip("#").lower(), content, file_id, media_type, created_by, now()),
        )

    async def get_note(self, chat_id: int, name: str) -> aiosqlite.Row | None:
        return await self.fetch_one(
            "SELECT * FROM notes WHERE chat_id = ? AND name = ?", (chat_id, name.lstrip("#").lower())
        )

    async def list_notes(self, chat_id: int) -> list[str]:
        rows = await self.fetch_all(
            "SELECT name FROM notes WHERE chat_id = ? ORDER BY name", (chat_id,)
        )
        return [row["name"] for row in rows]

    async def delete_note(self, chat_id: int, name: str) -> bool:
        row = await self.get_note(chat_id, name)
        if row is None:
            return False
        await self.execute(
            "DELETE FROM notes WHERE chat_id = ? AND name = ?", (chat_id, name.lstrip("#").lower())
        )
        return True

    # -- game scores ---------------------------------------------------------

    async def add_score(
        self,
        chat_id: int,
        game: str,
        user_id: int,
        score: int,
        *,
        user_name: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        await self.execute(
            """
            INSERT INTO scores (chat_id, game, user_id, user_name, score, meta, played_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (chat_id, game, user_id, user_name, score, json.dumps(meta or {}), now()),
        )

    async def leaderboard(
        self, chat_id: int, game: str, *, limit: int = 10, since: float | None = None
    ) -> list[Score]:
        """Best score per user, highest first."""
        sql = """
            SELECT user_id, MAX(score) AS best,
                   (SELECT user_name FROM scores s2
                     WHERE s2.chat_id = s.chat_id AND s2.game = s.game AND s2.user_id = s.user_id
                     ORDER BY s2.played_at DESC LIMIT 1) AS user_name,
                   MAX(played_at) AS last
              FROM scores s
             WHERE chat_id = ? AND game = ? {window}
             GROUP BY user_id
             ORDER BY best DESC
             LIMIT ?
        """.format(window="AND played_at >= ?" if since else "")
        params: list[Any] = [chat_id, game]
        if since:
            params.append(since)
        params.append(limit)
        rows = await self.fetch_all(sql, params)
        return [
            Score(
                user_id=row["user_id"],
                user_name=row["user_name"],
                score=row["best"],
                played_at=row["last"],
            )
            for row in rows
        ]

    async def user_best(self, chat_id: int, game: str, user_id: int) -> int:
        return int(
            await self.fetch_value(
                "SELECT MAX(score) FROM scores WHERE chat_id = ? AND game = ? AND user_id = ?",
                (chat_id, game, user_id),
            )
        )

    async def game_stats(self, chat_id: int | None = None) -> list[tuple[str, int]]:
        if chat_id is None:
            rows = await self.fetch_all(
                "SELECT game, COUNT(*) AS n FROM scores GROUP BY game ORDER BY n DESC"
            )
        else:
            rows = await self.fetch_all(
                "SELECT game, COUNT(*) AS n FROM scores WHERE chat_id = ? GROUP BY game ORDER BY n DESC",
                (chat_id,),
            )
        return [(row["game"], row["n"]) for row in rows]

    async def scores_since(self, ts: float) -> int:
        return int(await self.fetch_value("SELECT COUNT(*) FROM scores WHERE played_at >= ?", (ts,)))

    # -- events --------------------------------------------------------------

    async def log_event(
        self,
        tag: str,
        *,
        chat_id: int | None = None,
        user_id: int | None = None,
        data: dict[str, Any] | str | None = None,
    ) -> None:
        payload = data if isinstance(data, str) else json.dumps(data or {}, ensure_ascii=False)
        await self.execute(
            "INSERT INTO events (ts, tag, chat_id, user_id, data) VALUES (?,?,?,?,?)",
            (now(), tag, chat_id, user_id, payload),
        )

    async def events_since(self, ts: float, *, tag: str | None = None) -> list[aiosqlite.Row]:
        if tag:
            return await self.fetch_all(
                "SELECT * FROM events WHERE ts >= ? AND tag = ? ORDER BY ts DESC", (ts, tag)
            )
        return await self.fetch_all("SELECT * FROM events WHERE ts >= ? ORDER BY ts DESC", (ts,))

    async def event_counts(self, ts: float) -> dict[str, int]:
        rows = await self.fetch_all(
            "SELECT tag, COUNT(*) AS n FROM events WHERE ts >= ? GROUP BY tag ORDER BY n DESC", (ts,)
        )
        return {row["tag"]: row["n"] for row in rows}

    async def prune_events(self, keep_days: int = 30) -> int:
        cutoff = now() - keep_days * 86400
        count = int(await self.fetch_value("SELECT COUNT(*) FROM events WHERE ts < ?", (cutoff,)))
        await self.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        return count

    # -- key/value -----------------------------------------------------------

    async def set_kv(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    async def get_kv(self, key: str, default: Any = None) -> Any:
        raw = await self.fetch_value("SELECT value FROM kv WHERE key = ?", (key,), None)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    async def toggle_kv(self, key: str, default: bool = False) -> bool:
        value = not bool(await self.get_kv(key, default))
        await self.set_kv(key, value)
        return value

    # -- summaries -----------------------------------------------------------

    async def stats(self, since: float | None = None) -> dict[str, Any]:
        """Numbers used by the boot message, the daily report and ``/stats``."""
        since = since if since is not None else now() - 86400
        return {
            "users": await self.total_users(),
            "users_new": await self.users_since(since),
            "users_active": await self.active_since(since),
            "banned": len(await self.banned_users()),
            "chats": await self.count_chats(),
            "games": await self.scores_since(since),
            "events": await self.event_counts(since),
        }

    async def vacuum(self) -> None:
        await self.execute("VACUUM")

    #: every table the schema owns, in a stable order (used by ``--check`` and the panel)
    TABLES: tuple[str, ...] = (
        "users",
        "chats",
        "warnings",
        "notes",
        "filters",
        "scores",
        "events",
        "kv",
        "playlists",
        "playlist_tracks",
        "afk",
        "voice_auth",
    )

    async def table_sizes(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in self.TABLES:
            out[table] = int(await self.fetch_value(f"SELECT COUNT(*) FROM {table}"))
        return out


def _iter_sql(statements: Iterable[str]) -> list[str]:  # pragma: no cover - helper for tooling
    return [s.strip() for s in statements if s.strip()]
