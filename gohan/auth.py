"""Access control & chat-type filters."""

from __future__ import annotations

from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from .config import cfg


class OwnerOnly(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        if not cfg.owner_ids:
            return True
        user = event.from_user
        return bool(user and user.id in cfg.owner_ids)


class PrivateChat(Filter):
    """True for private Message *and* private CallbackQuery events."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        chat = getattr(event, "chat", None)
        if chat is None:
            message = getattr(event, "message", None)
            chat = getattr(message, "chat", None) if message is not None else None
        return bool(chat is not None and chat.type == "private")


owner = OwnerOnly()
private = PrivateChat()
