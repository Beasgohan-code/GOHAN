"""Composer: draft intake, preview, schedule, stream, effect, cancel."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..auth import owner, private
from ..config import cfg
from ..rich import build_rich, draft_keyboard, escape, resolve_mode
from ..storage import Draft, Storage
from ..stream import start_stream

log = logging.getLogger("gohan.compose")

router = Router(name="compose")          # slash commands
intake_router = Router(name="compose-intake")  # plain text — MUST be included last

MAX_BYTES = 32768  # Bot API rich message hard limit (UTF-8)
URL_RE = __import__("re").compile(r"^https?://", __import__("re").IGNORECASE)


def _now_local() -> datetime:
    try:
        return datetime.now(ZoneInfo(cfg.tz))
    except Exception:  # unknown tz -> UTC, never crash
        return datetime.now(timezone.utc)


def _fmt_run_at(run_at: float) -> str:
    try:
        return datetime.fromtimestamp(run_at, ZoneInfo(cfg.tz)).strftime("%H:%M %d %b")
    except Exception:
        return datetime.fromtimestamp(run_at, timezone.utc).strftime("%H:%M %d %b (UTC)")


async def _load_draft(storage: Storage, user_id: int) -> Draft:
    return await storage.get_draft(user_id) or Draft()


async def send_preview(bot, message: Message, draft: Draft) -> None:
    """Render the draft *as the actual post* with the control panel attached."""
    if not draft.text.strip():
        await message.reply("Nothing to preview yet — send me your post text first.")
        return
    sent = await bot.send_rich_message(
        chat_id=message.chat.id,
        rich_message=build_rich(draft.text, draft.mode),
        reply_markup=draft_keyboard(draft),
    )
    draft.prev_msg_id = sent.message_id


# ------------------------------------------------------------------ commands

@router.message(Command("new"), private, owner)
async def cmd_new(message: Message, storage: Storage) -> None:
    await storage.clear_draft(message.from_user.id)
    await message.reply(
        "🆕 Fresh draft.\n\nSend me your post — HTML or Markdown both work "
        "(auto-detected). Tables, headings, code, checklists and collapsible "
        "blocks are all supported."
    )


@router.message(Command("cancel"), private, owner)
async def cmd_cancel(message: Message, bot, storage: Storage) -> None:
    draft = await storage.get_draft(message.from_user.id)
    await storage.clear_draft(message.from_user.id)
    if draft and draft.prev_msg_id:
        try:
            await bot.delete_message(message.chat.id, draft.prev_msg_id)
        except Exception:
            pass
    await message.reply("🗑 Draft discarded." if draft else "No active draft.")


@router.message(Command("preview"), private, owner)
async def cmd_preview(message: Message, bot, storage: Storage) -> None:
    draft = await _load_draft(storage, message.from_user.id)
    await send_preview(bot, message, draft)
    await storage.save_draft(message.from_user.id, draft)


@router.message(Command("stream"), private, owner)
async def cmd_stream(message: Message, bot, storage: Storage) -> None:
    draft = await _load_draft(storage, message.from_user.id)
    if not draft.text.strip():
        await message.reply("No draft yet — type your post first, then /stream.")
        return
    await message.reply(
        "🧪 Streaming via sendRichMessageDraft (can_stop + keep_on_stop). "
        "A stop button will appear in the client."
    )
    start_stream(bot, message.chat.id, draft.text, draft.mode)


@router.message(Command("schedule"), private, owner)
async def cmd_schedule(
    message: Message, command: CommandObject, bot, storage: Storage
) -> None:
    draft = await _load_draft(storage, message.from_user.id)
    if not draft.text.strip():
        await message.reply("Create a draft first (type your post), then /schedule.")
        return
    args = (command.args or "").split()
    if not args or not args[0].isdigit():
        await message.reply(
            "Usage: <code>/schedule 90</code> — publish in 90 minutes.\n"
            "Or use the 📅 button on your preview for quick options.",
            parse_mode="HTML",
        )
        return
    minutes = int(args[0])
    channels = await storage.list_channels()
    if not channels:
        await message.reply("No channels yet — /addchannel @yourchannel first.")
        return
    target = channels[0]
    run_at = time.time() + minutes * 60
    post_id = await storage.add_post(
        message.from_user.id, target["chat_id"], draft, run_at
    )
    await message.reply(
        f"📅 Post #{post_id} scheduled for <b>{_fmt_run_at(run_at)}</b> "
        f"({cfg.tz}) → {escape(target['title'])}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🗑 Cancel it",
                        callback_data=f"g:pcancel:{post_id}",
                    )
                ]
            ]
        ),
    )


@router.message(Command("queue"), private, owner)
async def cmd_queue(message: Message, storage: Storage) -> None:
    rows = await storage.pending_posts(message.from_user.id)
    if not rows:
        await message.reply("📭 Queue is empty.")
        return
    channels = {c["chat_id"]: c for c in await storage.list_channels()}
    lines, kb_rows = [], []
    for row in rows:
        title = channels.get(row["chat_id"], {}).get("title", str(row["chat_id"]))
        lines.append(
            f"• <b>#{row['id']}</b> → {escape(title)} "
            f"@ <code>{_fmt_run_at(row['run_at'])}</code>"
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 Cancel #{row['id']}",
                    callback_data=f"g:pcancel:{row['id']}",
                )
            ]
        )
    await message.reply(
        "📭 <b>Pending posts</b>\n\n" + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


@router.message(Command("effect"), private, owner)
async def cmd_effect(message: Message, command: CommandObject, storage: Storage) -> None:
    user_id = message.from_user.id
    draft = await _load_draft(storage, user_id)
    args = (command.args or "").strip()
    if not args:
        await message.reply(
            "Usage: <code>/effect 5101838440137014545</code> or "
            "<code>/effect off</code>\n"
            "Effect ids come from Telegram clients (✨ on a sent message).",
            parse_mode="HTML",
        )
        return
    if args.lower() in {"off", "none", "0"}:
        draft.options["effect"] = False
        draft.options["effect_id"] = None
        await message.reply("✨ Effect disabled.")
    else:
        if not args.isdigit():
            await message.reply("Effect id must be numeric.")
            return
        draft.options["effect_id"] = args
        draft.options["effect"] = True
        await message.reply(f"✨ Effect armed: <code>{args}</code>", parse_mode="HTML")
    await storage.save_draft(user_id, draft)


# --------------------------------------------------------------- text intake

@intake_router.message(F.content_type == "text", private, owner)
async def on_text(message: Message, bot, storage: Storage) -> None:
    text = message.text or ""
    if text.startswith("/"):
        return  # unknown command — ignore quietly
    user_id = message.from_user.id
    draft = await _load_draft(storage, user_id)

    # -- "Label | url" while adding an inline button ------------------------
    if draft.awaiting == "button":
        if "|" not in text:
            await message.reply('Send it as: <code>Read more | https://example.com</code>',
                                parse_mode="HTML")
            return
        label, _, url = text.partition("|")
        label, url = label.strip(), url.strip()
        if not label or not URL_RE.match(url):
            await message.reply("URL must start with http:// or https://")
            return
        if len(draft.buttons) >= 8:
            await message.reply("Maximum 8 buttons per post.")
            return
        draft.buttons.append({"label": label[:64], "url": url})
        draft.awaiting = None
        await storage.save_draft(user_id, draft)
        await message.reply(f"🔘 Added button “{label}”.")
        await send_preview(bot, message, draft)
        await storage.save_draft(user_id, draft)
        return

    # -- brand new content ---------------------------------------------------
    if len(text.encode("utf-8")) > MAX_BYTES:
        await message.reply(
            f"That's {len(text.encode('utf-8'))} bytes — rich messages cap at "
            f"{MAX_BYTES} UTF-8 bytes. Trim it a little."
        )
        return

    draft.text = text
    draft.mode = "auto"
    draft.awaiting = None
    resolved = resolve_mode(text, draft.mode)
    await storage.save_draft(user_id, draft)
    await send_preview(bot, message, draft)
    await storage.save_draft(user_id, draft)
    await message.reply(
        f"📝 Draft captured ({len(text.encode('utf-8'))} bytes, "
        f"<b>{resolved}</b> auto-detected). Use the panel above to send, "
        f"schedule or stream it.",
        parse_mode="HTML",
    )
