"""Streaming rich drafts.

Telegram can show a *draft* message while it is still being generated
(``sendRichMessageDraft``). The draft is a 30-second preview: updates with the
same ``draft_id`` animate in place, and it disappears unless the bot finally
sends the complete message with ``sendRichMessage``.

That makes the flow for an AI-style answer:

1. open a :class:`RichDraft` (optionally showing a ``<tg-thinking>`` block),
2. :meth:`~RichDraft.update` as tokens arrive - throttled, because drafting is an
   API call and Telegram will rate-limit aggressive loops,
3. :meth:`~RichDraft.finish` to persist the final message (and, optionally, a
   keyboard).

Example::

    async with RichDraft(bot, chat_id) as draft:
        async for chunk in stream_answer(prompt):
            await draft.append(chunk)
        await draft.finish(reply_markup=kb)

``can_stop`` shows the user a stop button; Telegram then delivers a
``stopped_message_generation`` update which the bot handles in
:mod:`gohan.handlers.stream` to cancel the producer.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup, Message, ReplyParameters

from ..logging_setup import get_logger
from ._richgram import HAS_RICHGRAM, rich_heading, rich_note
from .sender import input_rich, rich_send

log = get_logger("rich.draft")

__all__ = ["RichDraft", "thinking_block"]

#: ``<tg-thinking>`` is recommended by Telegram for the "generating" placeholder.
_THINKING_BLOCK = "<tg-thinking></tg-thinking>"


def thinking_block(emoji: str = "🤖", label: str = "ᴛʜɪɴᴋɪɴɢ") -> str:
    """The placeholder shown while the model is still working."""
    return f"{_THINKING_BLOCK}\n{rich_note(f'{emoji} <i>{label}…</i>')}"


class RichDraft:
    """Streams a rich draft message, then persists the final version.

    Args:
        bot: the aiogram bot.
        chat_id: **private** chat id (drafts only work in private chats).
        draft_id: non-zero id; updates with the same id animate. Random by default.
        interval: minimum seconds between two API updates.
        can_stop: show the user a "stop generating" button.
        keep_on_stop: keep the draft visible after the user stops it.
        message_thread_id: topic id, when the private chat has topics enabled.
        thinking: show the ``<tg-thinking>`` placeholder before the first update.
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        draft_id: int | None = None,
        interval: float = 0.7,
        can_stop: bool = False,
        keep_on_stop: bool = False,
        message_thread_id: int | None = None,
        thinking: bool = True,
    ) -> None:
        if not isinstance(chat_id, int):
            raise TypeError("drafts only work in private chats - chat_id must be an int")
        self.bot = bot
        self.chat_id = chat_id
        self.draft_id = draft_id or secrets.randbelow(2**31 - 2) + 1
        self.interval = max(0.1, float(interval))
        self.can_stop = can_stop
        self.keep_on_stop = keep_on_stop
        self.message_thread_id = message_thread_id
        self.thinking = thinking

        self._buffer = ""
        self._last_sent = 0.0
        self._last_payload = ""
        self._updates = 0
        self._stopped = False
        self._started = time.monotonic()
        self._lock = asyncio.Lock()
        self._final: Message | None = None

    # -- state ---------------------------------------------------------------

    @property
    def text(self) -> str:
        """Everything appended so far."""
        return self._buffer

    @property
    def updates(self) -> int:
        """How many API draft updates were actually sent."""
        return self._updates

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def stopped(self) -> bool:
        """True once ``/stop`` was received for this draft."""
        return self._stopped

    def stop(self) -> None:
        """Mark the draft as cancelled by the user."""
        self._stopped = True

    # -- writing -------------------------------------------------------------

    async def update(self, html: str, *, force: bool = False) -> bool:
        """Replace the draft content. Returns ``True`` if an update was sent."""
        async with self._lock:
            self._buffer = html
            now = time.monotonic()
            if not force and self._last_payload and now - self._last_sent < self.interval:
                return False
            if html == self._last_payload:
                return False
            ok = await self._push(html)
            if ok:
                self._last_sent = now
                self._last_payload = html
            return ok

    async def append(self, chunk: str, *, force: bool = False) -> bool:
        """Append text and flush if the throttle window has passed."""
        return await self.update(self._buffer + chunk, force=force)

    async def _push(self, html: str) -> bool:
        try:
            await self.bot.send_rich_message_draft(
                chat_id=self.chat_id,
                draft_id=self.draft_id,
                rich_message=input_rich(html),
                message_thread_id=self.message_thread_id,
                can_stop=self.can_stop or None,
                keep_on_stop=self.keep_on_stop or None,
            )
            self._updates += 1
            return True
        except TelegramAPIError as exc:
            # A stop/cancel race is normal; anything else is worth knowing about.
            log.debug("draft update failed: %s", exc)
            return False

    # -- finishing -----------------------------------------------------------

    async def finish(
        self,
        html: str | None = None,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        reply_parameters: ReplyParameters | None = None,
        fallback: bool = True,
        validate: bool = True,
    ) -> Message | None:
        """Send the completed message (this is what makes it stick)."""
        from .sender import validate_html

        content = html if html is not None else self._buffer
        if validate:
            content = validate_html(content)
        self._final = await rich_send(
            self.bot,
            self.chat_id,
            content,
            reply_markup=reply_markup,
            message_thread_id=self.message_thread_id,
            reply_parameters=reply_parameters,
            fallback=fallback,
        )
        return self._final

    async def cancel(self, *, keep: bool | None = None) -> None:
        """Abandon the draft without sending a final message.

        Drafts are ephemeral, so "cancelling" simply means not calling
        :meth:`finish`; this method exists so call sites read clearly.
        """
        self._stopped = True
        if keep is None:
            keep = self.keep_on_stop
        if keep and self._buffer:
            await self.finish(validate=False)

    # -- context manager -----------------------------------------------------

    async def __aenter__(self) -> RichDraft:
        if self.thinking:
            await self.update(thinking_block(), force=True)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        # On an exception the draft is simply left to expire - never post a
        # half-finished answer as if it were the result.
        return False

    def stats(self) -> dict[str, Any]:
        return {
            "updates": self._updates,
            "chars": len(self._buffer),
            "elapsed_s": round(self.elapsed, 2),
            "stopped": self._stopped,
            "draft_id": self.draft_id,
        }


def drafts_supported() -> bool:
    """Whether the installed stack advertises rich-message support."""
    return HAS_RICHGRAM


__all__ += ["drafts_supported", "rich_heading"]
