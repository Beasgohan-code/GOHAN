"""Channel management + MTProto-powered inspection: /addchannel /channels /chinfo /latest."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    Message,
)

from ..auth import owner, private
from ..mtproto import MtpEngine
from ..rich import escape
from ..storage import Storage

log = logging.getLogger("gohan.channels")

router = Router(name="channels")

POSTABLE_TYPES = ("channel", "supergroup", "group")


async def _default_target(command: CommandObject | None, storage: Storage) -> str | None:
    if command and (command.args or "").strip():
        return command.args.strip()
    channels = await storage.list_channels()
    return str(channels[0]["chat_id"]) if channels else None


def _as_chat_id(raw: str) -> int | str:
    raw = raw.strip()
    return int(raw) if raw.lstrip("-").isdigit() else raw


# ------------------------------------------------------------------ listing

def _list_text(channels: list[dict]) -> str:
    if not channels:
        return (
            "📡 <b>No channels yet.</b>\n\n"
            "Add one with <code>/addchannel @yourchannel</code> — the bot must "
            "be an <b>administrator with post rights</b> in the channel."
        )
    lines = [
        f"• <b>{escape(c['title'])}</b> "
        f"(<code>{c['chat_id']}</code>)"
        + (f" @{escape(c['username'])}" if c["username"] else "")
        for c in channels
    ]
    return "📡 <b>Registered channels</b>\n\n" + "\n".join(lines)


def _list_kb(channels: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for c in channels:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📤 {c['title'][:24]}", callback_data=f"g:to:{c['chat_id']}"
                ),
                InlineKeyboardButton(
                    text="🗑", callback_data=f"g:rmch:{c['chat_id']}"
                ),
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🔄 Refresh", callback_data="g:chlist")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("channels"), private, owner)
async def cmd_channels(message: Message, storage: Storage) -> None:
    channels = await storage.list_channels()
    await message.reply(
        _list_text(channels),
        parse_mode="HTML",
        reply_markup=_list_kb(channels),
    )


@router.callback_query(lambda c: c.data == "g:chlist", private, owner)
async def cb_channels_list(call: CallbackQuery, storage: Storage) -> None:
    channels = await storage.list_channels()
    await call.answer()
    try:
        await call.message.edit_text(
            _list_text(channels),
            parse_mode="HTML",
            reply_markup=_list_kb(channels),
        )
    except Exception:
        log.debug("channel list edit failed", exc_info=True)


# --------------------------------------------------------------- add/remove

@router.message(Command("addchannel"), private, owner)
async def cmd_add_channel(
    message: Message, command: CommandObject, bot, storage: Storage
) -> None:
    target = (command.args or "").strip()
    if not target:
        await message.reply("Usage: <code>/addchannel @mychannel</code>", parse_mode="HTML")
        return
    try:
        chat = await bot.get_chat(_as_chat_id(target))
    except Exception as exc:  # noqa: BLE001
        await message.reply(f"⚠️ Can't read that chat: <code>{escape(str(exc)[:200])}</code>",
                            parse_mode="HTML")
        return
    if chat.type not in POSTABLE_TYPES:
        await message.reply(f"Unsupported chat type: <code>{escape(str(chat.type))}</code>",
                            parse_mode="HTML")
        return

    if chat.type == "channel":
        try:
            me = await bot.get_me()
            member = await bot.get_chat_member(chat.id, me.id)
        except Exception as exc:  # noqa: BLE001
            await message.reply(f"⚠️ Membership check failed: {escape(str(exc)[:200])}",
                                parse_mode="HTML")
            return
        status = getattr(member.status, "value", member.status)
        if status != "administrator" or getattr(member, "can_post_messages", None) is False:
            await message.reply(
                "🚫 I need to be an <b>administrator with "
                "<i>Post messages</i></b> rights in this channel.\n"
                "Channel → Administrators → Add admin → me → enable <b>Post messages</b>.",
                parse_mode="HTML",
            )
            return

    title = chat.title or str(chat.id)
    username = getattr(chat, "username", None)
    await storage.upsert_channel(chat.id, title, username, str(chat.type))
    count = None
    try:
        count = await bot.get_chat_member_count(chat.id)
    except Exception:
        pass
    await message.reply(
        f"✅ <b>{escape(title)}</b> registered"
        + (f" · <code>{count:,}</code> subscribers" if count else "")
        + ".\nUse 📤 Send now / 📅 Schedule or <code>/channels</code>.",
        parse_mode="HTML",
    )


@router.callback_query(lambda c: c.data and c.data.startswith("g:rmch:"), private, owner)
async def cb_remove_channel(call: CallbackQuery, storage: Storage) -> None:
    chat_id = int(call.data.split(":", 2)[2])
    await storage.delete_channel(chat_id)
    channels = await storage.list_channels()
    await call.answer("Channel removed")
    try:
        await call.message.edit_text(
            _list_text(channels),
            parse_mode="HTML",
            reply_markup=_list_kb(channels),
        )
    except Exception:
        log.debug("edit after remove failed", exc_info=True)


# -------------------------------------------------------------- inspection

@router.message(Command("chinfo"), private, owner)
async def cmd_chat_info(
    message: Message, command: CommandObject, bot, storage: Storage, mtp: MtpEngine
) -> None:
    target = await _default_target(command, storage)
    if not target:
        await message.reply("Usage: <code>/chinfo @mychannel</code>", parse_mode="HTML")
        return
    chat_id = _as_chat_id(target)
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as exc:  # noqa: BLE001
        await message.reply(f"⚠️ {escape(str(exc)[:200])}", parse_mode="HTML")
        return

    count = None
    try:
        count = await bot.get_chat_member_count(chat.id)
    except Exception:
        pass

    engine_bits = ["Bot API · aiogram get_chat"]
    members = count
    if mtp.enabled:
        try:
            info = await mtp.chat_info(chat.id)
            if info and info.get("members"):
                members = info["members"]
                engine_bits.append("Kurigram get_chat_members_count")
        except Exception:
            log.debug("mtp chat_info failed", exc_info=True)

    description = (getattr(chat, "description", None) or "").strip()
    member_line = f"• members: <code>{members:,}</code>" if members is not None else "• members: —"
    text = (
        f"📇 <b>{escape(chat.title or 'Chat')}</b>\n\n"
        f"• id: <code>{chat.id}</code>\n"
        f"• type: <code>{escape(str(getattr(chat.type, 'value', chat.type)))}</code>\n"
        f"• username: "
        + (f"@{escape(chat.username)}" if getattr(chat, "username", None) else "—")
        + f"\n{member_line}"
    )
    if description:
        text += f"\n\n<blockquote>{escape(description[:800])}</blockquote>"
    text += f"\n\n<i>Sources: {' + '.join(engine_bits)}</i>"

    kb = None
    if getattr(chat, "username", None):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🌍 Open chat",
                        url=f"https://t.me/{chat.username}",
                    )
                ]
            ]
        )
    await bot.send_rich_message(
        chat_id=message.chat.id,
        rich_message=InputRichMessage(html=text),
        reply_markup=kb,
    )


@router.message(Command("latest"), private, owner)
async def cmd_latest(
    message: Message, command: CommandObject, bot, storage: Storage, mtp: MtpEngine
) -> None:
    target = await _default_target(command, storage)
    if not target:
        await message.reply("Usage: <code>/latest @mychannel</code>", parse_mode="HTML")
        return
    if not mtp.enabled:
        await message.reply(
            "🛰 <b>MTProto engine is off.</b>\n\n"
            "Reading channel history isn't possible through the Bot API — "
            "set <code>API_ID</code> / <code>API_HASH</code> in <code>.env</code> "
            "(and optionally <code>SESSION_STRING</code>) to enable "
            "Kurigram's <code>/latest</code>.",
            parse_mode="HTML",
        )
        return

    chat_id = _as_chat_id(target)
    post = await mtp.latest_post(chat_id)
    if post is None:
        await message.reply(
            "🛰 No messages found — make sure the bot/user is a member of the chat."
        )
        return

    body = (getattr(post, "text", None) or getattr(post, "caption", None) or "").strip()
    if not body:
        body = "🖼 (media post without text)"
    body = body[:3000]
    date = getattr(post, "date", None)
    date_line = f" · {date.strftime('%Y-%m-%d %H:%M')}" if date else ""
    text = (
        f"📜 <b>Latest post</b>{date_line}\n"
        f"<i>via Kurigram get_chat_history (msg {post.id})</i>\n\n"
        f"<blockquote>{escape(body)}</blockquote>"
    )
    kb = None
    if getattr(post, "link", None):
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🌍 Open", url=post.link)]
            ]
        )
    await bot.send_rich_message(
        chat_id=message.chat.id,
        rich_message=InputRichMessage(html=text),
        reply_markup=kb,
    )
