"""Sending rich messages through an aiogram bot.

Three things this module adds on top of the raw ``Bot.send_rich_message``:

1. **A fallback.** Rich messages are only rendered by recent Telegram clients
   and can be rejected by the API (older servers, unsupported tags). On failure
   the same content is re-sent as a plain ``sendMessage`` with the markup
   stripped, so a user never sees an error instead of a menu.
2. **Edits.** ``edit_message_text`` accepts ``rich_message`` but *not* together
   with ``text``; :func:`rich_edit` handles the details (including the
   ``message is not modified`` case, which is not an error for us).
3. **Validation.** Rich messages allow ~32k characters, 50 media files and 500
   blocks; :func:`validate_html` clamps what it can and reports the rest.

All functions are tolerant: they return ``None`` rather than raising when the
message cannot be delivered, and log why.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    Chat,
    EphemeralMessageParameters,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
    MessageEntity,
    ReplyParameters,
)

from ..logging_setup import get_logger
from ._richgram import rich_to_plain

log = get_logger("rich.sender")

__all__ = [
    "MAX_BLOCKS",
    "MAX_CHARS",
    "MAX_MEDIA",
    "input_rich",
    "keyboard",
    "rich_answer",
    "rich_edit",
    "rich_reply",
    "rich_send",
    "rich_send_blocks",
    "validate_html",
]

#: Telegram's published limits for rich messages.
MAX_CHARS = 32_768
MAX_MEDIA = 50
MAX_BLOCKS = 500

_TRUNCATION_NOTE = "\n\n<i>… truncated</i>"


def input_rich(html: str, *, is_rtl: bool | None = None, skip_entity_detection: bool | None = None) -> InputRichMessage:
    """Wrap an HTML string into an :class:`InputRichMessage`."""
    return InputRichMessage(
        html=html,
        is_rtl=is_rtl,
        skip_entity_detection=skip_entity_detection,
    )


#: Tags that never need a closing counterpart.
_VOID_TAGS = {"br", "hr", "img", "tg-map", "tg-emoji", "tg-time", "tg-math", "input"}

_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)[^>]*?(/?)>")


def _open_tags(html: str) -> list[str]:
    """Tags still open at the end of ``html`` (innermost last)."""
    stack: list[str] = []
    for match in _TAG_RE.finditer(html):
        tag = match.group(1).lower()
        closing = html[match.start() + 1] == "/"
        self_closing = bool(match.group(2)) or tag in _VOID_TAGS
        if closing:
            if tag in stack:
                # close back to (and including) this tag
                while stack:
                    popped = stack.pop()
                    if popped == tag:
                        break
        elif not self_closing:
            stack.append(tag)
    return stack


def ephemeral_parameters(
    *,
    receiver_user_id: int | None = None,
    callback_query_id: str | None = None,
    replace_callback_query_message: bool | None = None,
) -> EphemeralMessageParameters:
    """Bot API 10.x: a message only ``receiver_user_id`` ever sees."""
    return EphemeralMessageParameters(
        receiver_user_id=receiver_user_id,
        callback_query_id=callback_query_id,
        replace_callback_query_message=replace_callback_query_message,
    )


def validate_html(html: str, *, max_chars: int = MAX_CHARS) -> str:
    """Clamp ``html`` to Telegram's limit without breaking the markup.

    The tail is cut, then any tag that was left open is closed again, so the
    result always parses. A short notice is appended.
    """
    def closers(fragment: str) -> str:
        return "".join(f"</{tag}>" for tag in reversed(_open_tags(fragment)))

    if len(html) <= max_chars:
        # Nothing to cut, but never ship a message with a dangling tag: Telegram
        # rejects the whole thing, and it is a one-line fix here.
        return html + closers(html)

    budget = max_chars - len(_TRUNCATION_NOTE) - 64
    head = html[:budget].rstrip()

    # Never end inside a tag: drop a trailing "<...".
    open_bracket = head.rfind("<")
    if open_bracket > head.rfind(">"):
        head = head[:open_bracket].rstrip()

    while len(head) + len(closers(head)) + 64 > max_chars:
        head = head[:-128].rstrip()
        open_bracket = head.rfind("<")
        if open_bracket > head.rfind(">"):
            head = head[:open_bracket].rstrip()

    return head + closers(head) + _TRUNCATION_NOTE


def _plain_fallback(html: str) -> str:
    """Readable plain text for the fallback path (never longer than 4096)."""
    text = rich_to_plain(html)
    if len(text) > 4096:
        text = text[:4090] + "…"
    return text or "…"


async def rich_send(
    bot: Bot,
    chat_id: int | str,
    html: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    message_thread_id: int | None = None,
    business_connection_id: str | None = None,
    direct_messages_topic_id: int | None = None,
    reply_parameters: ReplyParameters | None = None,
    disable_notification: bool | None = None,
    protect_content: bool | None = None,
    allow_paid_broadcast: bool | None = None,
    message_effect_id: str | None = None,
    is_rtl: bool | None = None,
    skip_entity_detection: bool | None = None,
    ephemeral: EphemeralMessageParameters | None = None,
    fallback: bool = True,
) -> Message | None:
    """Send a rich message, falling back to plain text if Telegram refuses it.

    ``ephemeral`` posts it so that only one person can see it - the buttons of a
    group's music panel belong to whoever pressed them, not to the whole chat.
    """
    payload = validate_html(html)
    try:
        return await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=input_rich(
                payload, is_rtl=is_rtl, skip_entity_detection=skip_entity_detection
            ),
            reply_markup=reply_markup,
            message_thread_id=message_thread_id,
            business_connection_id=business_connection_id,
            direct_messages_topic_id=direct_messages_topic_id,
            reply_parameters=reply_parameters,
            disable_notification=disable_notification,
            protect_content=protect_content,
            allow_paid_broadcast=allow_paid_broadcast,
            message_effect_id=message_effect_id,
            ephemeral_message_parameters=ephemeral,
        )
    except TelegramBadRequest as exc:
        if not fallback:
            raise
        log.warning("rich_send rejected (%s); falling back to plain text", exc.message[:160])
    except TelegramAPIError as exc:  # rate limits, network, …
        log.warning("rich_send failed (%s)", exc)
        if not fallback:
            raise
        return None

    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=_plain_fallback(html),
            reply_markup=reply_markup,
            message_thread_id=message_thread_id,
            business_connection_id=business_connection_id,
            reply_parameters=reply_parameters,
            disable_notification=disable_notification,
            ephemeral_message_parameters=ephemeral,
        )
    except TelegramAPIError as exc:
        log.error("plain fallback also failed: %s", exc)
        return None


async def rich_send_blocks(
    bot: Bot,
    chat_id: int | str,
    blocks: Sequence[Any],
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    message_thread_id: int | None = None,
    **kwargs: Any,
) -> Message | None:
    """Send typed ``InputRichBlock*`` entities instead of an HTML string."""
    try:
        return await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=InputRichMessage(blocks=list(blocks)),
            reply_markup=reply_markup,
            message_thread_id=message_thread_id,
            **kwargs,
        )
    except TelegramAPIError as exc:
        log.error("rich_send_blocks failed: %s", exc)
        return None


async def rich_edit(
    bot: Bot,
    *,
    chat_id: int | str | None = None,
    message_id: int | None = None,
    inline_message_id: str | None = None,
    html: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    business_connection_id: str | None = None,
    fallback: bool = True,
) -> Message | bool | None:
    """Edit an existing message into rich content.

    Returns the edited :class:`Message`, or ``None`` on failure. A
    "message is not modified" reply is treated as success and returns ``None``
    (there is nothing new to show).
    """
    payload = validate_html(html)
    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            inline_message_id=inline_message_id,
            rich_message=input_rich(payload),
            reply_markup=reply_markup,
            business_connection_id=business_connection_id,
        )
    except TelegramBadRequest as exc:
        message = (exc.message or "").lower()
        if "not modified" in message:
            return None
        if not fallback:
            raise
        log.warning("rich_edit rejected (%s); falling back to plain text", exc.message[:160])
    except TelegramAPIError as exc:
        log.warning("rich_edit failed (%s)", exc)
        if not fallback:
            raise
        return None

    try:
        return await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            inline_message_id=inline_message_id,
            text=_plain_fallback(html),
            reply_markup=reply_markup,
        )
    except TelegramBadRequest as exc:
        if "not modified" in (exc.message or "").lower():
            return None
        log.error("plain edit fallback failed: %s", exc)
        return None
    except TelegramAPIError as exc:
        log.error("plain edit fallback failed: %s", exc)
        return None


async def rich_reply(message: Message, html: str, **kwargs: Any) -> Message | None:
    """Reply to ``message`` with rich content."""
    bot = message.bot
    if bot is None:  # pragma: no cover - defensive
        return None
    return await rich_send(
        bot,
        message.chat.id,
        html,
        reply_parameters=ReplyParameters(message_id=message.message_id),
        **kwargs,
    )


async def rich_answer(callback_query: Any, html: str, **kwargs: Any) -> Message | bool | None:
    """Replace the message a callback button belongs to with rich content."""
    bot = callback_query.bot if hasattr(callback_query, "bot") else None
    if bot is None:  # pragma: no cover - defensive
        return None
    message = getattr(callback_query, "message", None)
    if message is None:
        return None
    if isinstance(message, Message):
        return await rich_edit(
            bot, chat_id=message.chat.id, message_id=message.message_id, html=html, **kwargs
        )
    # Inline-mode callback: only the inline message id is available.
    inline_id = getattr(message, "inline_message_id", None) or getattr(
        callback_query, "inline_message_id", None
    )
    if inline_id:
        return await rich_edit(bot, inline_message_id=inline_id, html=html, **kwargs)
    return None


# ---------------------------------------------------------------------------
#  tiny keyboard helper (Bot API 9.4+ supports coloured buttons)
# ---------------------------------------------------------------------------


def keyboard(
    rows: Sequence[Sequence[dict[str, Any]]] | None = None,
    **kwargs: Any,
) -> InlineKeyboardMarkup | None:
    """Build an inline keyboard from dicts.

    >>> keyboard([[{"text": "Yes", "callback_data": "y", "style": "success"}]])
    """
    if not rows:
        return None
    from aiogram.types import InlineKeyboardButton

    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(**cell) for cell in row] for row in rows],
        **kwargs,
    )
    return markup


async def safe_chat(bot: Bot, chat_id: int | str) -> Chat | None:
    """``get_chat`` that returns ``None`` instead of raising."""
    try:
        return await bot.get_chat(chat_id)
    except TelegramAPIError as exc:
        log.debug("get_chat(%s) failed: %s", chat_id, exc)
        return None


# ---------------------------------------------------------------------------
#  ephemeral messages (Bot API 9.5+/10.x): one person, one message
# ---------------------------------------------------------------------------


async def ephemeral_send(
    bot: Bot,
    chat_id: int | str,
    html: str,
    *,
    receiver_user_id: int | None = None,
    callback_query_id: str | None = None,
    replace_callback_query: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Send rich content only ``receiver_user_id`` can see.

    With ``replace_callback_query`` the message that carried the button is
    swapped for this one - that is how a control panel stays in place without
    ever touching the chat's history.
    """
    payload = validate_html(html)
    params = ephemeral_parameters(
        receiver_user_id=receiver_user_id,
        callback_query_id=callback_query_id,
        replace_callback_query_message=replace_callback_query if callback_query_id else None,
    )
    try:
        return await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=input_rich(payload),
            reply_markup=reply_markup,
            ephemeral_message_parameters=params,
        )
    except TelegramAPIError as exc:
        log.debug("ephemeral rich send rejected (%s); falling back to sent message", exc)
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=_plain_fallback(html),
            reply_markup=reply_markup,
            ephemeral_message_parameters=params,
        )
    except TelegramAPIError as exc:
        log.warning("ephemeral send failed: %s", exc)
        return None


async def ephemeral_edit(
    bot: Bot,
    chat_id: int | str,
    html: str,
    *,
    receiver_user_id: int,
    ephemeral_message_id: int,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Edit an ephemeral message in place (``edit_ephemeral_message_text``)."""
    try:
        return bool(
            await bot.edit_ephemeral_message_text(
                chat_id=chat_id,
                receiver_user_id=int(receiver_user_id),
                ephemeral_message_id=int(ephemeral_message_id),
                rich_message=input_rich(validate_html(html)),
                reply_markup=reply_markup,
            )
        )
    except TelegramBadRequest as exc:
        if "not modified" in (exc.message or "").lower():
            return True
        log.debug("ephemeral edit rejected: %s", exc.message[:160])
        return False
    except TelegramAPIError as exc:
        log.debug("ephemeral edit failed: %s", exc)
        return False


__all__ += ["MessageEntity", "ephemeral_edit", "ephemeral_parameters", "ephemeral_send", "safe_chat"]
