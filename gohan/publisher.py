"""Publish pipeline: sendRichMessage first, sensible fallbacks after.

Ladder:
    1. aiogram  ``sendRichMessage``            (Bot API 10.1 rich)
    2. aiogram  ``sendMessage`` with parse mode (html / classic markdown)
    3. aiogram  ``sendMessage`` raw text        (never-fail content delivery)
    4. Kurigram ``send_rich_message``           (MTProto, different rights)
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import LinkPreviewOptions, Message, ReplyParameters

from .mtproto import MtpEngine
from .rich import build_rich, resolve_mode, strip_tags, url_keyboard

log = logging.getLogger("gohan.publish")


def _flags(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "disable_notification": True if options.get("silent") else None,
        "protect_content": True if options.get("protect") else None,
        "allow_paid_broadcast": True if options.get("paid") else None,
        "message_effect_id": (
            options.get("effect_id") if options.get("effect") else None
        ),
    }


async def _send_rich(
    bot: Bot,
    chat_id: int | str,
    text: str,
    mode: str,
    options: dict[str, Any],
    kb,
    rp: ReplyParameters | None,
) -> Message:
    return await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=build_rich(text, mode),
        reply_markup=kb,
        reply_parameters=rp,
        **_flags(options),
    )


async def publish(
    bot: Bot,
    mtp: MtpEngine | None,
    chat_id: int | str,
    text: str,
    mode: str,
    buttons: list[dict[str, str]],
    options: dict[str, Any],
    reply_to: int | None = None,
) -> tuple[int, str]:
    """Publish a post. Returns ``(message_id, engine_used)``. Raises last error."""
    mode = resolve_mode(text, mode)
    kb = url_keyboard(buttons)
    rp = (
        ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
        if reply_to
        else None
    )
    opts = dict(options)
    last: Exception | None = None

    # 1) rich --------------------------------------------------------------
    try:
        msg = await _send_rich(bot, chat_id, text, mode, opts, kb, rp)
        return msg.message_id, "bot-api/sendRichMessage"
    except TelegramBadRequest as exc:
        last = exc
        # a stale/unknown effect id must not cost us the rich rendering
        if opts.get("effect") and "effect" in str(exc).lower():
            opts = {**opts, "effect": False}
            try:
                msg = await _send_rich(bot, chat_id, text, mode, opts, kb, rp)
                return msg.message_id, "bot-api/sendRichMessage"
            except TelegramBadRequest as exc2:
                last = exc2
    except TelegramAPIError as exc:
        last = exc

    # 2) classic parse-mode fallback --------------------------------------
    if mode == "html":
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(
                    is_disabled=not bool(opts.get("link_preview"))
                ),
                reply_markup=kb,
                reply_parameters=rp,
                **{k: v for k, v in _flags(opts).items() if k != "message_effect_id"},
            )
            return msg.message_id, "bot-api/html"
        except TelegramAPIError as exc:
            last = exc
    else:
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                link_preview_options=LinkPreviewOptions(
                    is_disabled=not bool(opts.get("link_preview"))
                ),
                reply_markup=kb,
                reply_parameters=rp,
                **{k: v for k, v in _flags(opts).items() if k != "message_effect_id"},
            )
            return msg.message_id, "bot-api/markdown"
        except TelegramAPIError as exc:
            last = exc

    # 3) raw text ----------------------------------------------------------
    raw = strip_tags(text) if mode == "html" else text
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=raw,
            link_preview_options=LinkPreviewOptions(
                is_disabled=not bool(opts.get("link_preview"))
            ),
            reply_markup=kb,
            reply_parameters=rp,
            **{k: v for k, v in _flags(opts).items() if k != "message_effect_id"},
        )
        return msg.message_id, "bot-api/text"
    except TelegramAPIError as exc:
        last = exc

    # 4) Kurigram MTProto --------------------------------------------------
    if mtp is not None and mtp.enabled:
        try:
            mtp_id = await mtp.send_rich(chat_id, mode, text, opts, buttons)
            if mtp_id:
                return mtp_id, "kurigram/send_rich_message"
        except Exception as exc:  # noqa: BLE001 — last leg, keep the best error
            last = exc

    log.warning("publish failed for chat %s: %r", chat_id, last)
    raise last if last else RuntimeError("publish failed")
