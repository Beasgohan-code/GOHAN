"""Composer control-panel callbacks + generation-stop handling."""

from __future__ import annotations

import logging
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageGenerationStopped,
)

from ..auth import owner, private
from ..mtproto import MtpEngine
from ..publisher import publish
from ..rich import MODES, build_rich, draft_keyboard, escape, resolve_mode
from ..storage import Draft, Storage
from ..stream import start_stream, stop_stream
from .compose import send_preview

log = logging.getLogger("gohan.actions")

router = Router(name="actions")


# ------------------------------------------------------------------ helpers

def _tg_link(username: str | None, chat_id: int, message_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    return f"https://t.me/c/{str(chat_id).removeprefix('-100')}/{message_id}"


async def _load(storage: Storage, user_id: int) -> Draft:
    return await storage.get_draft(user_id) or Draft()


async def _refresh_panel(bot, draft: Draft, chat_id: int) -> None:
    if draft.prev_msg_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=draft.prev_msg_id,
                reply_markup=draft_keyboard(draft),
            )
        except TelegramAPIError:
            log.debug("panel refresh failed", exc_info=True)


def _channels_kb(channels: list[dict], purpose: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for ch in channels:
        label = ch["title"][:32]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📺 {label}", callback_data=f"g:{purpose}:{ch['chat_id']}"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🔄 Refresh", callback_data=f"g:pick:{purpose}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _open_picker(bot, chat_id: int, storage: Storage, purpose: str, prompt: str):
    channels = await storage.list_channels()
    if not channels:
        return await bot.send_message(
            chat_id,
            "No channels registered yet.\n"
            "Add one first: <code>/addchannel @yourchannel</code> "
            "(bot must be an admin with post rights).",
            parse_mode="HTML",
        )
    return await bot.send_message(
        chat_id, prompt, reply_markup=_channels_kb(channels, purpose)
    )


# ----------------------------------------------------------- option toggles

async def _toggle(call: CallbackQuery, storage: Storage, key: str, label: str) -> None:
    draft = await _load(storage, call.from_user.id)
    draft.options[key] = not draft.options.get(key)
    await storage.save_draft(call.from_user.id, draft)
    await _refresh_panel(call.bot, draft, call.message.chat.id)
    await call.answer(f"{label} → {'on' if draft.options[key] else 'off'}")


@router.callback_query(F.data == "g:lp", private, owner)
async def cb_link(call: CallbackQuery, storage: Storage) -> None:
    await _toggle(call, storage, "link_preview", "Link preview (plain fallback)")


@router.callback_query(F.data == "g:prot", private, owner)
async def cb_protect(call: CallbackQuery, storage: Storage) -> None:
    await _toggle(call, storage, "protect", "Protect content")


@router.callback_query(F.data == "g:sil", private, owner)
async def cb_silent(call: CallbackQuery, storage: Storage) -> None:
    await _toggle(call, storage, "silent", "Silent send")


@router.callback_query(F.data == "g:paid", private, owner)
async def cb_paid(call: CallbackQuery, storage: Storage) -> None:
    await _toggle(call, storage, "paid", "Paid broadcast (0.1★/msg)")


@router.callback_query(F.data == "g:fx", private, owner)
async def cb_effect(call: CallbackQuery, storage: Storage) -> None:
    draft = await _load(storage, call.from_user.id)
    turning_on = not draft.options.get("effect")
    if turning_on and not (draft.options.get("effect_id")):
        await call.answer(
            "No effect id set — use /effect <id> first (then toggle here).",
            show_alert=True,
        )
        return
    draft.options["effect"] = turning_on
    await storage.save_draft(call.from_user.id, draft)
    await _refresh_panel(call.bot, draft, call.message.chat.id)
    await call.answer(f"Effect → {'on' if turning_on else 'off'}")


# ------------------------------------------------------------ mode / preview

@router.callback_query(F.data == "g:mode", private, owner)
async def cb_mode(call: CallbackQuery, bot, storage: Storage) -> None:
    draft = await _load(storage, call.from_user.id)
    idx = MODES.index(draft.mode) if draft.mode in MODES else 0
    draft.mode = MODES[(idx + 1) % len(MODES)]
    await storage.save_draft(call.from_user.id, draft)
    if draft.text and draft.prev_msg_id:
        try:
            # showcase: editMessageText(rich_message=…) re-renders in place
            await bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=draft.prev_msg_id,
                rich_message=build_rich(draft.text, draft.mode),
                reply_markup=draft_keyboard(draft),
            )
        except (TelegramBadRequest, TelegramAPIError):
            try:
                await bot.delete_message(call.message.chat.id, draft.prev_msg_id)
            except TelegramAPIError:
                pass
            await send_preview(bot, call.message, draft)
            await storage.save_draft(call.from_user.id, draft)
    resolved = resolve_mode(draft.text, draft.mode) if draft.text else draft.mode
    await call.answer(f"Mode → {draft.mode}" + (f" (renders as {resolved})" if draft.mode == "auto" else ""))


@router.callback_query(F.data == "g:pv", private, owner)
async def cb_preview(call: CallbackQuery, bot, storage: Storage) -> None:
    draft = await _load(storage, call.from_user.id)
    if not draft.text:
        await call.answer("No draft — type your post first.", show_alert=True)
        return
    await send_preview(bot, call.message, draft)
    await storage.save_draft(call.from_user.id, draft)
    await call.answer("👁 Rendered")


@router.callback_query(F.data == "g:rep", private, owner)
async def cb_replace(call: CallbackQuery) -> None:
    await call.answer("Send the replacement text now — I'll swap the draft.", show_alert=True)


@router.callback_query(F.data == "g:st", private, owner)
async def cb_snapshot(call: CallbackQuery, storage: Storage) -> None:
    draft = await _load(storage, call.from_user.id)
    o = draft.options
    flags = ",".join(
        k for k in ("link_preview", "protect", "silent", "paid", "effect") if o.get(k)
    ) or "none"
    await call.answer(
        f"{len(draft.text.encode())}B · mode {resolve_mode(draft.text, draft.mode) if draft.text else draft.mode} · "
        f"{len(draft.buttons)} buttons · flags: {flags}",
        show_alert=True,
    )


@router.callback_query(F.data == "g:del", private, owner)
async def cb_discard(call: CallbackQuery, bot, storage: Storage) -> None:
    draft = await _load(storage, call.from_user.id)
    await storage.clear_draft(call.from_user.id)
    if draft.prev_msg_id:
        try:
            await bot.delete_message(call.message.chat.id, draft.prev_msg_id)
        except TelegramAPIError:
            pass
    await call.answer("🗑 Draft discarded")


@router.callback_query(F.data == "g:stream", private, owner)
async def cb_stream(call: CallbackQuery, bot, storage: Storage) -> None:
    draft = await _load(storage, call.from_user.id)
    if not draft.text:
        await call.answer("No draft yet — type your post first.", show_alert=True)
        return
    await call.answer("🧪 Streaming… (stop button in your client)")
    start_stream(bot, call.message.chat.id, draft.text, draft.mode)


# -------------------------------------------------------------- send / plan

@router.callback_query(F.data.in_({"g:send", "g:chref"}), private, owner)
async def cb_pick_send(call: CallbackQuery, bot, storage: Storage) -> None:
    await call.answer()
    await _open_picker(
        bot, call.message.chat.id, storage, "to", "📤 Where should I publish?"
    )


@router.callback_query(F.data.in_({"g:sch", "g:picksch"}), private, owner)
async def cb_pick_sch(call: CallbackQuery, bot, storage: Storage) -> None:
    draft = await _load(storage, call.from_user.id)
    if not draft.text:
        await call.answer("No draft yet — type your post first.", show_alert=True)
        return
    await call.answer()
    await _open_picker(
        bot,
        call.message.chat.id,
        storage,
        "sc",
        "📅 Which channel, and when?",
    )


@router.callback_query(F.data.startswith("g:pick:"), private, owner)
async def cb_pick_refresh(call: CallbackQuery, bot, storage: Storage) -> None:
    purpose = call.data.split(":", 2)[2]
    await call.answer()
    prompt = (
        "📤 Where should I publish?"
        if purpose == "to"
        else "📅 Which channel, and when?"
    )
    await _open_picker(bot, call.message.chat.id, storage, purpose, prompt)


@router.callback_query(F.data.startswith("g:to:"), private, owner)
async def cb_send_now(call: CallbackQuery, bot, storage: Storage, mtp: MtpEngine) -> None:
    chat_id = int(call.data.split(":", 2)[2])
    draft = await _load(storage, call.from_user.id)
    if not draft.text:
        await call.answer("No draft — nothing to send.", show_alert=True)
        return
    await call.answer("📤 Publishing…")
    try:
        message_id, engine = await publish(
            bot,
            mtp,
            chat_id,
            draft.text,
            draft.mode,
            draft.buttons,
            draft.options,
        )
    except Exception as exc:  # noqa: BLE001
        await bot.send_message(
            call.message.chat.id, f"❌ Publish failed: <code>{escape(str(exc)[:400])}</code>",
            parse_mode="HTML",
        )
        return
    channels = {c["chat_id"]: c for c in await storage.list_channels()}
    ch = channels.get(chat_id, {})
    link = _tg_link(ch.get("username"), chat_id, message_id)
    await bot.send_message(
        call.message.chat.id,
        f"✅ Published to <b>{escape(ch.get('title', str(chat_id)))}</b> "
        f"via <code>{engine}</code> (msg {message_id})",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🌍 View post", url=link),
                    InlineKeyboardButton(text="📤 Send elsewhere", callback_data="g:send"),
                ],
                [InlineKeyboardButton(text="🧪 Stream it", callback_data="g:stream")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("g:sc:"), private, owner)
async def cb_schedule_pick(call: CallbackQuery, storage: Storage) -> None:
    chat_id = int(call.data.split(":", 2)[2])
    await call.answer()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label, callback_data=f"g:schq:{mins}:{chat_id}"
                )
                for label, mins in options
            ]
            for options in (
                [("⏱ +5 min", 5), ("⏱ +30 min", 30)],
                [("⏱ +2 h", 120), ("⏱ +1 d", 1440)],
            )
        ]
    )
    await call.message.answer("⏰ When do you want it published?", reply_markup=kb)


@router.callback_query(F.data.startswith("g:schq:"), private, owner)
async def cb_schedule_quick(call: CallbackQuery, storage: Storage) -> None:
    _, _, rest = call.data.partition(":")  # "mins:chat_id"
    mins_raw, _, chat_id_raw = rest.partition(":")
    minutes, chat_id = int(mins_raw), int(chat_id_raw)
    draft = await _load(storage, call.from_user.id)
    if not draft.text:
        await call.answer("No draft — nothing to schedule.", show_alert=True)
        return
    run_at = time.time() + minutes * 60
    post_id = await storage.add_post(call.from_user.id, chat_id, draft, run_at)
    await call.answer(f"📅 queued for +{minutes} min")
    try:
        await call.message.edit_text(
            f"📅 <b>Post #{post_id}</b> queued — publishes in {minutes} min.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🗑 Cancel it", callback_data=f"g:pcancel:{post_id}"
                        )
                    ],
                    [InlineKeyboardButton(text="📭 Queue", callback_data="g:q")],
                ]
            ),
        )
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("g:pcancel:"), private, owner)
async def cb_post_cancel(call: CallbackQuery, storage: Storage) -> None:
    post_id = int(call.data.split(":", 2)[2])
    ok = await storage.cancel_post(post_id, call.from_user.id)
    await call.answer("🗑 cancelled" if ok else "nothing to cancel")
    try:
        await call.message.edit_text(
            f"🚫 Scheduled post #{post_id} cancelled."
            if ok
            else f"Post #{post_id} is no longer pending.",
            reply_markup=None,
        )
    except TelegramAPIError:
        pass


@router.callback_query(F.data == "g:q", private, owner)
async def cb_queue_hint(call: CallbackQuery) -> None:
    await call.answer("Run /queue to see pending posts.", show_alert=True)


# ------------------------------------------------------------- button editor

def _buttons_text(draft: Draft) -> str:
    if not draft.buttons:
        return (
            "🔘 <b>URL buttons</b>\n\n"
            "No buttons yet. Add one as <code>Label | https://example.com</code> — "
            "they're attached as a classic inline keyboard under the rich post."
        )
    lines = [f"{i + 1}. <b>{escape(b['label'])}</b> → {escape(b['url'])}"
             for i, b in enumerate(draft.buttons)]
    return "🔘 <b>URL buttons</b>\n\n" + "\n".join(lines)


def _buttons_kb(draft: Draft) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"❌ {b['label'][:24]}", callback_data=f"g:btnrm:{i}"
            )
        ]
        for i, b in enumerate(draft.buttons)
    ]
    rows.append([InlineKeyboardButton(text="➕ Add button", callback_data="g:btnadd")])
    rows.append([InlineKeyboardButton(text="✅ Done", callback_data="g:done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "g:btns", private, owner)
async def cb_buttons_editor(call: CallbackQuery, bot, storage: Storage) -> None:
    draft = await _load(storage, call.from_user.id)
    await call.answer()
    await bot.send_message(
        call.message.chat.id,
        _buttons_text(draft),
        parse_mode="HTML",
        reply_markup=_buttons_kb(draft),
    )


@router.callback_query(F.data == "g:btnadd", private, owner)
async def cb_button_add(call: CallbackQuery, storage: Storage) -> None:
    draft = await _load(storage, call.from_user.id)
    draft.awaiting = "button"
    await storage.save_draft(call.from_user.id, draft)
    await call.answer(
        "Send: Label | https://example.com  (one message)", show_alert=True
    )


@router.callback_query(F.data.startswith("g:btnrm:"), private, owner)
async def cb_button_rm(call: CallbackQuery, storage: Storage) -> None:
    idx = int(call.data.split(":", 2)[2])
    draft = await _load(storage, call.from_user.id)
    if 0 <= idx < len(draft.buttons):
        removed = draft.buttons.pop(idx)
        await storage.save_draft(call.from_user.id, draft)
        await call.answer(f"Removed “{removed['label']}”")
    else:
        await call.answer("Already gone")
    try:
        await call.message.edit_text(
            _buttons_text(draft), parse_mode="HTML", reply_markup=_buttons_kb(draft)
        )
    except TelegramAPIError:
        pass


@router.callback_query(F.data == "g:done", private, owner)
async def cb_buttons_done(call: CallbackQuery, storage: Storage) -> None:
    draft = await _load(storage, call.from_user.id)
    await storage.save_draft(call.from_user.id, draft)
    await call.answer(f"{len(draft.buttons)} button(s) ready ✔")
    try:
        await call.message.edit_text(
            _buttons_text(draft), parse_mode="HTML", reply_markup=None
        )
    except TelegramAPIError:
        pass


# -------------------------------------------------- stopped_message_generation

async def on_generation_stopped(stopped: MessageGenerationStopped, bot) -> None:
    """Bot API 10.3: user pressed the draft's stop button.

    Registered on ``dp.stopped_message_generation`` (typed observer); the
    event object IS the ``MessageGenerationStopped`` update payload.
    """
    if stop_stream(stopped.chat.id, stopped.draft_id):
        try:
            await bot.send_message(stopped.chat.id, "⏹ Stream stopped.")
        except TelegramAPIError:
            pass
