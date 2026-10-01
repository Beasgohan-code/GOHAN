"""``/guardian`` - the per-chat control panel.

A single rich screen with colour-coded toggle buttons. Buttons carry the current
state so an admin can see at a glance what is on:

* green  = enabled
* red    = disabled
* blue   = informational

Every toggle writes to the chat's settings blob and re-renders the panel in
place, so flipping several settings is one continuous conversation.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..filters import IsAdmin, IsGroup
from ..logging_setup import get_logger
from ..rich import ui
from ..rich.sender import rich_edit, rich_reply, rich_send
from ..storage import Database
from .events import DEFAULTS

log = get_logger("guardian.panel")

router = Router(name="guardian.panel")
router.message.filter(IsGroup())

__all__ = ["router", "panel_body", "panel_keyboard"]

#: toggle key -> (label, emoji name, help text)
TOGGLES: dict[str, tuple[str, str, str]] = {
    "guardian": ("Guardian", "shield", "master switch for every automatic rule"),
    "captcha": ("Captcha", "pointer", "new members must tap a button before talking"),
    "antiraid": ("Anti-raid", "siren", "auto-mute and decline when a raid is detected"),
    "antispam": ("Anti-spam", "ban", "delete scam patterns, floods and duplicates"),
    "welcome": ("Welcome", "handshake", "greet every new member"),
    "goodbye": ("Goodbye", "moon", "say farewell when someone leaves"),
    "delete_links": ("No links", "link", "delete any message containing a link"),
    "delete_forwards": ("No forwards", "outbox", "delete forwarded messages"),
}


def _state_icon(enabled: bool) -> str:
    return "check" if enabled else "cross"


def panel_body(chat_title: str, current: dict[str, Any], *, stats: dict[str, Any] | None = None) -> str:
    """Render the settings screen."""
    rows = []
    for key, (label, _icon, help_text) in TOGGLES.items():
        enabled = bool(current.get(key))
        rows.append((f"{ui.e(_state_icon(enabled))} <b>{ui.caps(label)}</b>", ui.esc(help_text)))

    blocks: list[Any] = [
        ui.table(["ᴛᴏɢɢʟᴇ", "ᴡʜᴀᴛ ɪᴛ ᴅᴏᴇꜱ"], rows, raw=True),
    ]
    if stats:
        blocks.append(
            ui.kv_panel(
                [
                    ("📡 ꜱᴛᴀᴛᴇ", str(stats.get("state", "normal"))),
                    ("📊 ᴊᴏɪɴꜱ/ᴍɪɴ", str(stats.get("joins_window", 0))),
                    ("🎯 ꜱᴜꜱᴘɪᴄɪᴏɴ", f"{float(stats.get('suspicion', 0)):.0%}"),
                ],
                icon="chart",
            )
        )
    blocks.append(
        ui.details(
            "commands",
            ui.list_unordered(
                [
                    "<code>/warn /mute /kick /ban</code> - moderation",
                    "<code>/purge /lockdown</code> - bulk cleanup",
                    "<code>/welcome /rules</code> - group copy",
                ]
            ),
            icon="book",
        )
    )
    return ui.screen(
        "guardian",
        icon="shield",
        subtitle=f"{chat_title} · tap a toggle to flip it",
        blocks_=blocks,
    )


def panel_keyboard(current: dict[str, Any]) -> str:
    """Colour-coded buttons: green = on, red = off."""
    buttons = [
        ui.button(
            label,
            callback=f"guard:toggle:{key}",
            style="success" if current.get(key) else "danger",
            icon=_state_icon(bool(current.get(key))),
        )
        for key, (label, _icon, _help) in TOGGLES.items()
    ]
    rows: list[list[str]] = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append(
        [
            ui.button("Refresh", callback="guard:refresh", style="primary", icon="refresh"),
            ui.button("Lockdown", callback="guard:lockdown", style="danger", icon="lock"),
            ui.button("Close", callback="guard:close", style="link", icon="cross"),
        ]
    )
    rows.append(
        [
            ui.button("Assistant", callback="guard:ai", style="primary", icon="robot"),
            ui.button("Filters", callback="flt:list", style="primary", icon="magnet"),
            ui.button("Commands", callback="guard:commands", style="primary", icon="book"),
        ]
    )
    return ui.actions(*rows)


def chat_commands_block() -> str:
    """The group command index shown by the Commands button."""
    return ui.details(
        "group commands",
        ui.table(
            ["ᴀʀᴇᴀ", "ᴄᴏᴍᴍᴀɴᴅꜱ"],
            [
                ("guardian", "/guardian · /settings · /lockdown"),
                ("moderation", "/warn /mute /kick /ban /unban /purge"),
                ("filters", "/filter /stop /stopall /filters"),
                ("notes", "/save /notes"),
                ("assistant", "/ai on · /ai off · /ai prompt · /ask"),
                ("music", "/music /download"),
                ("anime", "/anime /trending /character"),
                ("games", "/quiz /guess /chain /dice /slot /top"),
                ("fun", "/hug /kiss /slap … see /actions"),
            ],
            raw=True,
        ),
        icon="book",
        open=True,
    )


async def _current(db: Database, chat_id: int) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update(await db.get_chat_settings(chat_id))
    return merged


def _chat_title(message_or_query: Any) -> str:
    chat = getattr(message_or_query, "chat", None)
    return str(getattr(chat, "title", "") or "this chat")


@router.message(Command("guardian"))
async def cmd_guardian(
    message: Message,
    bot: Bot,
    db: Database,
    settings: Any,
    raid: Any = None,
) -> None:
    """Show the panel (admins only)."""
    if not await IsAdmin()(message, bot=bot, settings=settings):
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(ui.no("only admins can change these settings"), icon="lock"),
        )
        return
    current = await _current(db, message.chat.id)
    stats = raid.summary(message.chat.id) if raid is not None else None
    await rich_send(
        bot,
        message.chat.id,
        panel_body(_chat_title(message), current, stats=stats) + "\n\n" + panel_keyboard(current),
    )


@router.callback_query(F.data.startswith("guard:"))
async def on_panel_callback(
    callback: CallbackQuery, bot: Bot, db: Database, settings: Any, raid: Any = None
) -> None:
    """Handle every panel button."""
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else "refresh"
    chat = getattr(callback.message, "chat", None)
    if chat is None:
        await callback.answer("chat unavailable", show_alert=True)
        return
    if not await IsAdmin()(callback, bot=bot, settings=settings):
        await callback.answer("admins only", show_alert=True)
        return

    current = await _current(db, chat.id)

    if action == "close":
        await callback.answer("closed")
        try:
            await bot.delete_message(chat.id, callback.message.message_id)  # type: ignore[union-attr]
        except Exception:
            pass
        return

    if action == "lockdown":
        from .captcha import MUTED

        try:
            if current.get("lockdown"):
                from .moderation import _DEFAULT_GROUP_PERMISSIONS

                await bot.restrict_chat_member(chat.id, chat.id, permissions=_DEFAULT_GROUP_PERMISSIONS)
                current["lockdown"] = False
                await db.set_chat_setting(chat.id, "lockdown", False)
                if raid is not None:
                    raid.release(chat.id)
                await callback.answer("lockdown lifted")
            else:
                await bot.restrict_chat_member(chat.id, chat.id, permissions=MUTED)
                current["lockdown"] = True
                await db.set_chat_setting(chat.id, "lockdown", True)
                if raid is not None:
                    raid.lock(chat.id)
                await callback.answer("chat frozen")
        except Exception as exc:
            await callback.answer(f"could not change lockdown: {exc}", show_alert=True)
            return

    elif action == "toggle":
        key = parts[2] if len(parts) > 2 else ""
        if key not in TOGGLES:
            await callback.answer("unknown toggle", show_alert=True)
            return
        current[key] = not bool(current.get(key))
        await db.set_chat_setting(chat.id, key, current[key])
        label = TOGGLES[key][0]
        await callback.answer(f"{label}: {'on' if current[key] else 'off'}")
        try:
            await db.log_event(
                "GuardianSetting",
                chat_id=chat.id,
                user_id=callback.from_user.id,
                data={"key": key, "value": current[key]},
            )
        except Exception:
            pass
    elif action == "ai":
        from ..handlers.ai import toggle_chat_ai

        enabled = await toggle_chat_ai(db, chat.id)
        await callback.answer(f"assistant {'on' if enabled else 'off'}")
    elif action == "commands":
        await callback.answer("group commands")
        await rich_send(bot, chat.id, chat_commands_block())
        return
    else:
        await callback.answer("refreshed")

    stats = raid.summary(chat.id) if raid is not None else None
    body = panel_body(str(chat.title or ""), current, stats=stats) + "\n\n" + panel_keyboard(current)
    await rich_edit(
        bot,
        chat_id=chat.id,
        message_id=callback.message.message_id,  # type: ignore[union-attr]
        html=body,
    )


@router.message(Command("settings"))
async def cmd_settings_alias(message: Message, bot: Bot, db: Database, settings: Any, raid: Any = None) -> None:
    """``/settings`` - alias for ``/guardian``."""
    if not await IsAdmin()(message, bot=bot, settings=settings):
        await rich_reply(message, ui.panel(ui.no("admins only"), icon="lock"))
        return
    current = await _current(db, message.chat.id)
    stats = raid.summary(message.chat.id) if raid is not None else None
    await rich_reply(
        message,
        panel_body(_chat_title(message), current, stats=stats) + "\n\n" + panel_keyboard(current),
    )
