"""Middleware: the pipeline every update passes through.

Order matters, and the dispatcher registers them in this order:

``db``        records users/chats, blocks banned users, keeps ``last_seen`` fresh
``throttle``  per-user rate limit (protects the bot *and* Telegram's limits)
``rich``      resolves the user's rich/plain preference and custom-emoji flag
``errors``    logs unhandled handler exceptions to the log channel and tells
              the user something went wrong instead of going silent

Everything is dependency-injected through ``data``, so handlers receive
``db``, ``log`` (LogChannel), ``settings`` and a ready-made ``ui`` helper
without importing globals.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message, TelegramObject, User

from .config import Settings
from .filters import is_owner_id
from .logging_setup import get_logger
from .rich import emoji as emoji_registry
from .storage import Database

log = get_logger("middleware")

__all__ = [
    "DatabaseMiddleware",
    "ErrorsMiddleware",
    "MaintenanceMiddleware",
    "ThrottleMiddleware",
    "UsageMiddleware",
]


class DatabaseMiddleware(BaseMiddleware):
    """Upsert users and chats on every update; block banned users early."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["db"] = self.db

        user: User | None = data.get("event_from_user")
        if user is not None:
            if await self.db.is_banned(user.id):
                log.debug("ignoring banned user %s", user.id)
                return None
            try:
                await self.db.upsert_user(
                    user.id,
                    username=user.username,
                    first_name=user.first_name,
                    language_code=user.language_code,
                    is_premium=bool(user.is_premium),
                    is_bot=user.is_bot,
                )
            except Exception as exc:  # storage trouble must not stop updates
                log.warning("user upsert failed: %s", exc)

        chat = data.get("event_chat")
        if chat is not None and chat.type in ("group", "supergroup", "channel"):
            try:
                await self.db.upsert_chat(
                    chat.id, title=chat.title, type=chat.type, username=chat.username
                )
            except Exception as exc:
                log.warning("chat upsert failed: %s", exc)

        if isinstance(event, ChatMemberUpdated):
            data.setdefault("chat_settings", await self.db.get_chat_settings(event.chat.id))
            await self._sync_membership(event)

        return await handler(event, data)

    async def _sync_membership(self, event: ChatMemberUpdated) -> None:
        """Track the bot being added/removed, and DB-flag anyone who leaves."""
        from aiogram.enums import ChatMemberStatus

        me = event.bot.id if event.bot else None
        if me and event.new_chat_member.user.id == me:
            if event.new_chat_member.status in ("administrator", "creator", "member", "restricted"):
                return
            if event.new_chat_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                await self.db.remove_chat(event.chat.id)


class ThrottleMiddleware(BaseMiddleware):
    """Token-bucket rate limit per user.

    Telegram allows ~30 messages/second globally but only ~1/second to a single
    chat; this keeps the bot well under that and stops a single user from
    triggering flood waits that would affect everyone.
    """

    def __init__(self, rate: float = 0.6, burst: int = 5) -> None:
        self.rate = max(0.0, rate)
        self.burst = max(1, burst)
        self._state: dict[int, tuple[float, float]] = {}  # user_id -> (tokens, last)

    def _allow(self, user_id: int) -> bool:
        if self.rate <= 0:
            return True
        now = time.monotonic()
        tokens, last = self._state.get(user_id, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) / self.rate)
        if tokens < 1:
            self._state[user_id] = (tokens, now)
            return False
        self._state[user_id] = (tokens - 1, now)
        if len(self._state) > 100_000:  # bounded memory
            self._state.clear()
        return True

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.is_bot:
            return await handler(event, data)
        if not self._allow(user.id):
            log.debug("throttled %s", user.id)
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("slow down 🙂", show_alert=False)
                except Exception:
                    pass
            return None
        return await handler(event, data)


class UsageMiddleware(BaseMiddleware):
    """Extra context: chat settings, rich preference and the custom-emoji flag."""

    def __init__(self, db: Database, *, rich_default: bool = True) -> None:
        self.db = db
        self.rich_default = rich_default

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        chat = data.get("event_chat")
        if chat is not None and chat.type in ("group", "supergroup"):
            data.setdefault("chat_settings", await self.db.get_chat_settings(chat.id))
            if "ai" in data["chat_settings"]:
                data["ai_enabled"] = bool(data["chat_settings"]["ai"].get("enabled"))
        data.setdefault("custom_emoji", emoji_registry.custom_enabled())
        data.setdefault("rich_default", self.rich_default)
        return await handler(event, data)


class MaintenanceMiddleware(BaseMiddleware):
    """Pause regular users while the owner is working on the bot.

    Owners, and the commands a user needs to understand what is happening, are
    always allowed through - a maintenance mode that makes the bot look dead is
    worse than no maintenance mode at all.
    """

    ALLOWED = ("/start", "/help", "/id", "/ping", "/owner", "/status")

    def __init__(self) -> None:
        self._cache: tuple[float, bool] = (0.0, False)

    async def _enabled(self, db: Database) -> bool:
        stamp, value = self._cache
        if time.time() - stamp < 5:
            return value
        try:
            value = bool(await db.get_kv("maintenance", False))
        except Exception:
            value = False
        self._cache = (time.time(), value)
        return value

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        settings: Settings | None = data.get("settings")
        user: User | None = data.get("event_from_user")
        db: Database | None = data.get("db")
        if settings is None or user is None or db is None:
            return await handler(event, data)
        if is_owner_id(user.id, settings):
            return await handler(event, data)
        if not await self._enabled(db):
            return await handler(event, data)

        text = getattr(event, "text", None) or ""
        command = text.split("@")[0].split()[0].lower() if text.startswith("/") else ""
        if command in self.ALLOWED:
            return await handler(event, data)

        if isinstance(event, Message):
            await rich_reply(
                event,
                ui.screen(
                    "maintenance",
                    icon="tools",
                    blocks_=[
                        ui.panel(
                            "ᴛʜᴇ ᴏᴡɴᴇʀ ɪꜱ ᴡᴏʀᴋɪɴɢ ᴏɴ ᴍᴇ ʀɪɢʜᴛ ɴᴏᴡ.\n"
                            "ᴛʀʏ ᴀɢᴀɪɴ ɪɴ ᴀ ᴍɪɴᴜᴛᴇ - ᴛʜᴀɴᴋ ʏᴏᴜ ꜰᴏʀ ᴛʜᴇ ᴘᴀᴛɪᴇɴᴄᴇ 💛"
                        )
                    ],
                ),
            )
        elif isinstance(event, CallbackQuery):
            try:
                await event.answer("maintenance mode - try again in a minute", show_alert=True)
            except Exception:
                pass
        return None


class ErrorsMiddleware(BaseMiddleware):
    """Turn unhandled exceptions into a log-channel event plus a short apology."""

    def __init__(self, log_channel: Any = None) -> None:
        self.log_channel = log_channel

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception as exc:
            where = f"{type(event).__name__} handler"
            log.exception("unhandled error in %s: %s", where, exc)
            if self.log_channel is not None:
                try:
                    await self.log_channel.error(where, exc)
                except Exception:
                    pass
            await self._apologise(event)
            return None

    @staticmethod
    async def _apologise(event: TelegramObject) -> None:
        try:
            if isinstance(event, Message):
                await event.reply("⚠️ something broke on my side. the owner has been told.")
            elif isinstance(event, CallbackQuery):
                await event.answer("something broke on my side ⚠️", show_alert=True)
        except Exception:
            pass
