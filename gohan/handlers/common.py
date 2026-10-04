"""Static cards & meta commands: /start /help /richdemo /status."""

from __future__ import annotations

import aiogram
import pyrogram
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InputRichMessage, Message

from .. import __version__
from ..auth import owner, private
from ..config import cfg
from ..mtproto import MtpEngine
from ..rich import (
    HELP_HTML,
    WELCOME_HTML,
    demo_document,
    demo_keyboard,
    escape,
    help_keyboard,
    welcome_keyboard,
)
from ..storage import Storage

router = Router(name="common")


async def send_help(bot, chat_id: int) -> None:
    await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=InputRichMessage(html=HELP_HTML),
        reply_markup=help_keyboard(),
    )


async def send_demo(bot, chat_id: int) -> None:
    await bot.send_rich_message(
        chat_id=chat_id,
        rich_message=demo_document(),
        reply_markup=demo_keyboard(),
    )


@router.message(Command("start"), private, owner)
async def cmd_start(message: Message, bot) -> None:
    name = escape(message.from_user.full_name if message.from_user else "friend")
    text = WELCOME_HTML.format(
        name=name,
        aiogram_ver=aiogram.__version__,
        kurigram_ver=pyrogram.__version__,
    )
    await bot.send_rich_message(
        chat_id=message.chat.id,
        rich_message=InputRichMessage(html=text),
        reply_markup=welcome_keyboard(),
    )


@router.message(Command("help"), private, owner)
async def cmd_help(message: Message, bot) -> None:
    await send_help(bot, message.chat.id)


@router.message(Command("richdemo"), private, owner)
async def cmd_demo(message: Message, bot) -> None:
    await bot.send_rich_message(
        chat_id=message.chat.id,
        rich_message=demo_document(),
        reply_markup=demo_keyboard(),
    )


@router.message(Command("status"), private, owner)
async def cmd_status(message: Message, bot, storage: Storage, mtp: MtpEngine) -> None:
    me = await bot.get_me()
    stats = await storage.stats()
    mtp_line = (
        f"🟢 {escape(mtp.who or 'ready')}"
        if mtp.enabled
        else f"⚪️ {escape(mtp.disabled_reason)}"
    )
    text = (
        f"🍚 <b>GOHAN status</b> v{__version__}\n\n"
        f"<b>Bot API engine</b>: aiogram {aiogram.__version__} → "
        f"@{escape(me.username)} (id {me.id})\n"
        f"<b>MTProto engine</b>: Kurigram {pyrogram.__version__} → {mtp_line}\n"
        f"<b>Database</b>: {cfg.database} · {stats['channels']} channels · "
        f"{stats['drafts']} drafts\n"
        f"<b>Queue</b>: {stats['pending']} pending · {stats['published']} published\n"
        f"<b>Access</b>: "
        + (
            f"owner-only ({', '.join(str(i) for i in sorted(cfg.owner_ids))})"
            if cfg.owner_ids
            else "open (set OWNER_IDS to lock it down)"
        )
    )
    await bot.send_rich_message(
        chat_id=message.chat.id, rich_message=InputRichMessage(html=text)
    )


# ------------------------------------------------------------- card callbacks

@router.callback_query(F.data == "g:hlp", private, owner)
async def cb_help(call: CallbackQuery, bot) -> None:
    await send_help(bot, call.message.chat.id)
    await call.answer()


@router.callback_query(F.data == "g:demo", private, owner)
async def cb_demo(call: CallbackQuery, bot) -> None:
    await send_demo(bot, call.message.chat.id)
    await call.answer()


@router.callback_query(F.data == "g:new", private, owner)
async def cb_new(call: CallbackQuery) -> None:
    await call.answer(
        "Send me the text for your new post (HTML or Markdown) 👇",
        show_alert=True,
    )


@router.callback_query(F.data == "g:noop", private, owner)
async def cb_noop(call: CallbackQuery) -> None:
    await call.answer("👌 rich button works")
