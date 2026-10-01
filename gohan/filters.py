"""Custom aiogram filters and permission helpers.

``IsOwner``    the bot owner (``OWNER_USER_IDS``)
``IsAdmin``    a chat admin or the owner - the gate on every moderation command
``IsGroup``    group/supergroup only
``Command``    a command that also works as ``/cmd@thisbot``
``RateLimit``  a simple per-user cooldown for expensive commands

Handlers read chat settings from ``data["settings"]`` (populated by
:class:`gohan.middleware.UsageMiddleware`), so toggles are one dict lookup away.
"""

from __future__ import annotations

import time
from typing import Any

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message, User

from .config import Settings
from .logging_setup import get_logger

log = get_logger("filters")

__all__ = [
    "ADMIN_STATUSES",
    "IsAdmin",
    "IsGroup",
    "IsOwner",
    "RateLimit",
    "chat_setting",
    "is_admin",
    "is_owner",
]

ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


def is_owner(user: User | None, settings: Settings) -> bool:
    if user is None:
        return False
    owners = settings.owner_ids
    return bool(owners) and user.id in owners


async def is_admin(bot: Bot, chat_id: int, user_id: int, settings: Settings | None = None) -> bool:
    """True when the user administers the chat (or owns the bot)."""
    if settings is not None and is_owner_id(user_id, settings):
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception as exc:
        log.debug("get_chat_member(%s, %s) failed: %s", chat_id, user_id, exc)
        return False
    return member.status in ADMIN_STATUSES


def is_owner_id(user_id: int, settings: Settings) -> bool:
    return user_id in settings.owner_ids


class IsOwner(BaseFilter):
    """Only the bot owner(s) configured in ``OWNER_USER_IDS``."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    async def __call__(self, event: Message | CallbackQuery, **kwargs: Any) -> bool:
        settings = self._settings or kwargs.get("settings")
        user = getattr(event, "from_user", None)
        if settings is None or user is None:
            return False
        return is_owner_id(user.id, settings)


class IsAdmin(BaseFilter):
    """A chat admin, chat creator, or the bot owner."""

    async def __call__(self, event: Message | CallbackQuery, bot: Bot, **kwargs: Any) -> bool:
        settings: Settings | None = kwargs.get("settings")
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
        if user is None or chat is None or settings is None:
            return False
        if is_owner_id(user.id, settings):
            return True
        if chat.type == "private":
            return is_owner_id(user.id, settings)
        # Anonymous group admins have no id we can verify - treat as admin.
        if getattr(user, "id", 0) == 1087968824:
            return True
        return await is_admin(bot, chat.id, user.id)


class IsGroup(BaseFilter):
    """Group or supergroup messages only."""

    async def __call__(self, event: Message | ChatMemberUpdated, **kwargs: Any) -> bool:
        chat = getattr(event, "chat", None)
        return bool(chat and chat.type in ("group", "supergroup"))


class RateLimit(BaseFilter):
    """Allow at most one call per user every ``seconds``.

    Applied per handler, so ``/ask`` can be limited without touching ``/start``.
    """

    def __init__(self, seconds: float = 3.0, *, message: str = "one moment 🙂") -> None:
        self.seconds = seconds
        self.message = message
        self._last: dict[int, float] = {}

    async def __call__(self, event: Message | CallbackQuery, **kwargs: Any) -> bool:
        user = getattr(event, "from_user", None)
        if user is None:
            return True
        now = time.monotonic()
        last = self._last.get(user.id)
        if last is not None and now - last < self.seconds:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer(self.message, show_alert=False)
                except Exception:
                    pass
            return False
        self._last[user.id] = now
        if len(self._last) > 50_000:
            self._last.clear()
        return True

    def forget(self, user_id: int) -> None:
        self._last.pop(user_id, None)


def chat_setting(data: dict[str, Any], key: str, default: Any = None) -> Any:
    """Read one key out of a chat's settings blob from handler ``data``."""
    settings = data.get("chat_settings") or {}
    return settings.get(key, default)
