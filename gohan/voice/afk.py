"""Away mode (``/afk``) - ported from the Go bot's ``afk.go``.

Implemented as an **outer message middleware** rather than a handler. A handler
that watches every message would sit in front of the whole router chain and
starve it (aiogram stops at the first match); a middleware can send its notices
and still hand the update on, so a message from someone who just came back is
answered *and* processed normally.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram.types import Message, ReplyParameters

from ..logging_setup import get_logger
from ..rich.sender import rich_send
from .cards import afk_leave_card, afk_ping_card

log = get_logger("voice.afk")

__all__ = ["AfkWatcher", "set_away"]

#: Do not repeat the same "X is away" notice more often than this.
PING_COOLDOWN = 300.0
#: Re-check whether *anybody* is away at most this often (saves a query per message).
PRESENCE_CACHE = 20.0


async def set_away(db: Any, user_id: int, chat_id: int, reason: str | None) -> None:
    """Mark a user as away (used by ``/afk``)."""
    await db.set_afk(user_id, chat_id, (reason or "").strip()[:200] or None)


class AfkWatcher:
    """Sends away notices and clears them when the person speaks again."""

    def __init__(self, db: Any, bot: Any = None, *, cooldown: float = PING_COOLDOWN) -> None:
        self.db = db
        self.bot = bot
        self.cooldown = float(cooldown)
        self._pings: dict[tuple[int, int, int], float] = {}
        self._mentions: dict[tuple[int, int], int] = {}
        self._presence_checked = 0.0
        self._any_away = False

    # -- helpers -------------------------------------------------------------

    async def _somebody_is_away(self) -> bool:
        now = time.monotonic()
        if now - self._presence_checked < PRESENCE_CACHE:
            return self._any_away
        try:
            count = await self.db.count_afk()
        except Exception as exc:  # pragma: no cover - db hiccup must stay silent
            log.debug("afk presence check failed: %s", exc)
            return self._any_away
        self._presence_checked = now
        self._any_away = count > 0
        return self._any_away

    def _mentioned(self, message: Message) -> list[Any]:
        """Users mentioned by ``text_mention``/``mention`` entities."""
        found: list[Any] = []
        text = message.text or message.caption or ""
        for entity in list(message.entities or []) + list(message.caption_entities or []):
            if entity.type == "text_mention" and entity.user is not None:
                found.append(entity.user)
            elif entity.type == "mention":
                username = text[entity.offset : entity.offset + entity.length].lstrip("@")
                found.append(username)
        reply = message.reply_to_message
        if reply is not None and reply.from_user is not None and not reply.from_user.is_bot:
            found.append(reply.from_user)
        return found

    async def _resolve(self, target: Any) -> Any:
        """``@name`` → a user row, so mentions without an id still work."""
        if not isinstance(target, str):
            return target
        try:
            rows = await self.db.search_users(target, limit=1)
        except Exception:  # pragma: no cover - older databases
            return None
        if not rows:
            return None
        return rows[0]

    # -- the middleware ------------------------------------------------------

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        bot = data.get("bot") or self.bot or getattr(event, "bot", None)
        if bot is None or event.from_user is None or event.from_user.is_bot:
            return await handler(event, data)
        chat = event.chat
        if chat is None or chat.type not in ("group", "supergroup"):
            return await handler(event, data)
        if not await self._somebody_is_away():
            return await handler(event, data)

        # 1. coming back: clear the entry and welcome them, then keep going
        try:
            entry = await self.db.afk_entry(event.from_user.id, chat.id)
        except Exception:  # pragma: no cover
            entry = None
        if entry is not None:
            await self.db.clear_afk(event.from_user.id, chat.id)
            mentions = self._mentions.pop((chat.id, event.from_user.id), 0)
            if not (event.text or "").startswith("/afk"):
                try:
                    await rich_send(
                        bot,
                        chat.id,
                        afk_leave_card(count=mentions),
                        reply_parameters=ReplyParameters(message_id=event.message_id),
                    )
                except Exception as exc:  # pragma: no cover
                    log.debug("afk welcome back failed: %s", exc)

        # 2. somebody mentioned an away user: tell the speaker once in a while
        for target in self._mentioned(event):
            row = await self._resolve(target)
            if row is None:
                continue
            user_id = int(row["user_id"] if not isinstance(row, int) else row)
            if user_id == event.from_user.id:
                continue
            try:
                away = await self.db.afk_entry(user_id, chat.id)
            except Exception:  # pragma: no cover
                away = None
            if away is None:
                continue
            self._mentions[(chat.id, user_id)] = self._mentions.get((chat.id, user_id), 0) + 1
            key = (chat.id, event.from_user.id, user_id)
            if time.monotonic() - self._pings.get(key, 0.0) < self.cooldown:
                continue
            self._pings[key] = time.monotonic()
            name = row["first_name"] or (f"@{row['username']}" if row["username"] else str(user_id))
            try:
                await rich_send(
                    bot,
                    chat.id,
                    afk_ping_card(str(name), away["reason"], since=away["since"]),
                    reply_parameters=ReplyParameters(message_id=event.message_id),
                )
            except Exception as exc:  # pragma: no cover
                log.debug("afk ping failed: %s", exc)

        return await handler(event, data)
