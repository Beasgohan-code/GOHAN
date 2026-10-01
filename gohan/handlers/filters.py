"""``/filters`` - trigger -> reply auto-answers, the classic group utility.

* ``/filter <trigger> <reply>``   add or replace (reply to media to save it)
* ``/stop <trigger>``             delete one
* ``/stopall``                    delete everything
* ``/filters``                    the panel: list, search, delete - all button driven

Management is **group-admin by default**, but the bot owner can switch a chat to
owner-only with ``/filtermode`` (or the toggle in the owner panel).
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from ..config import Settings
from ..filters import IsAdmin, IsGroup, is_admin, is_owner
from ..logging_setup import get_logger
from ..rich import keys
from ..rich import ui
from ..rich.sender import rich_edit, rich_reply, rich_send
from ..storage import Database

log = get_logger("handlers.filters")

router = Router(name="handlers.filters")
router.message.filter(IsGroup())

__all__ = ["router", "OWNER_ONLY_KEY"]

#: chat setting: when true, only the bot owner may manage filters in this chat
OWNER_ONLY_KEY = "filters_owner_only"

MAX_TRIGGER = 32
MAX_REPLY = 1200


async def _may_manage(message: Message, bot: Bot, db: Database, settings: Settings) -> bool:
    """Admins manage by default; the owner may restrict a chat to themselves."""
    user = message.from_user
    if user is None:
        return False
    if is_owner(user, settings):
        return True
    chat_settings = await db.get_chat_settings(message.chat.id)
    if chat_settings.get(OWNER_ONLY_KEY):
        return False
    return await is_admin(bot, message.chat.id, user.id, settings)


async def _gate(message: Message, bot: Bot, db: Database, settings: Settings) -> bool:
    if not await _may_manage(message, bot, db, settings):
        chat_settings = await db.get_chat_settings(message.chat.id)
        why = (
            "ᴛʜɪꜱ ᴄʜᴀᴛ ɪꜱ ꜱᴇᴛ ᴛᴏ ᴏᴡɴᴇʀ-ᴏɴʟʏ"
            if chat_settings.get(OWNER_ONLY_KEY)
            else "ᴀᴅᴍɪɴꜱ ᴏɴʟʏ"
        )
        await rich_reply(message, ui.panel(ui.no(why), icon="lock"))
        return False
    return True


@router.message(Command("filter"))
async def cmd_filter(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    """``/filter <trigger> <reply>`` (or reply to a message)."""
    if not await _gate(message, bot, db, settings):
        return
    args = (command.args or "").strip()
    if not args:
        await rich_reply(
            message,
            ui.panel(
                "ᴜꜱᴀɢᴇ: <code>/filter hello hi there!</code>",
                icon="magnet",
            ),
        )
        return

    trigger, _, inline_reply = args.partition(" ")
    trigger = trigger.lstrip("/#")[:MAX_TRIGGER].lower()
    if not trigger:
        return

    reply_text = inline_reply.strip()
    kind, file_id = "text", None
    replied = message.reply_to_message
    if replied is not None:
        reply_text = reply_text or (replied.text or replied.caption or "").strip()
        if replied.photo:
            kind, file_id = "photo", replied.photo[-1].file_id
        elif replied.video:
            kind, file_id = "video", replied.video.file_id
        elif replied.animation:
            kind, file_id = "animation", replied.animation.file_id
        elif replied.sticker:
            kind, file_id = "sticker", replied.sticker.file_id
        elif replied.voice:
            kind, file_id = "voice", replied.voice.file_id

    if not reply_text and not file_id:
        await rich_reply(message, ui.panel(ui.no("nothing to save"), icon="question"))
        return
    reply_text = reply_text[:MAX_REPLY]

    existed = await db.get_filter(message.chat.id, trigger) is not None
    await db.save_filter(
        message.chat.id,
        trigger,
        reply_text,
        kind=kind,
        file_id=file_id,
        created_by=message.from_user.id if message.from_user else None,
    )
    total = await db.count_filters(message.chat.id)
    await rich_reply(
        message,
        ui.panel(
            ui.ok(
                f"{'updated' if existed else 'saved'} filter <b>#{ui.esc(trigger)}</b>"
                f" · <code>{total}</code> in this chat"
            ),
            icon="magnet",
        ),
        reply_markup=keys.keyboard(
            [
                keys.button("Open filters", callback="flt:list", style="primary", icon="list"),
                keys.button("Delete it", callback=f"flt:del:{trigger}", style="danger", icon="trash"),
            ]
        ),
    )


@router.message(Command(commands=["stop", "unfilter"]))
async def cmd_stop(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    """``/stop <trigger>`` - delete one filter."""
    if not await _gate(message, bot, db, settings):
        return
    trigger = (command.args or "").strip().lstrip("/#").lower()
    if not trigger:
        await rich_reply(message, ui.panel("ᴜꜱᴀɢᴇ: <code>/stop hello</code>", icon="question"))
        return
    removed = await db.delete_filter(message.chat.id, trigger)
    await rich_reply(
        message,
        ui.panel(
            ui.ok(f"deleted <b>#{ui.esc(trigger)}</b>") if removed else ui.no("no such filter"),
            icon="trash",
        ),
    )


@router.message(Command(commands=["stopall", "clearfilters"]))
async def cmd_stopall(
    message: Message, bot: Bot, db: Database, settings: Settings
) -> None:
    """``/stopall`` - delete every filter in this chat (asks first)."""
    if not await _gate(message, bot, db, settings):
        return
    count = await db.count_filters(message.chat.id)
    if not count:
        await rich_reply(message, ui.panel(ui.italic("no filters to delete"), icon="broom"))
        return
    await rich_reply(
        message,
        ui.screen(
            "delete all filters",
            icon="warning",
            blocks_=[ui.panel(f"ᴛʜɪꜱ ᴡɪʟʟ ʀᴇᴍᴏᴠᴇ <b>{count}</b> ꜰɪʟᴛᴇʀꜱ. ᴛʜᴇʀᴇ ɪꜱ ɴᴏ ᴜɴᴅᴏ.")],
        ),
        reply_markup=keys.confirm("flt:clear:yes", yes_label=f"Yes, delete {count}", no_label="Keep them"),
    )


@router.message(Command("filtermode"))
async def cmd_filtermode(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    """``/filtermode admin|owner`` - who may manage filters here (bot owner only)."""
    if message.from_user is None or not is_owner(message.from_user, settings):
        await rich_reply(message, ui.panel(ui.no("bot owner only"), icon="crown"))
        return

    value = (command.args or "").strip().lower()

    chat_settings = await db.get_chat_settings(message.chat.id)
    if value not in ("admin", "owner"):
        current = "owner" if chat_settings.get(OWNER_ONLY_KEY) else "admin"
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(ui.kv("🧲 ꜰɪʟᴛᴇʀ ᴍᴏᴅᴇ", f"<b>{current}</b>"), icon="crown")
            + "\n\n"
            + ui.action_bar(
                [
                    ("Admins", "flt:mode:admin", "success"),
                    ("Owner only", "flt:mode:owner", "danger"),
                ],
                per_row=2,
            ),
        )
        return

    chat_settings[OWNER_ONLY_KEY] = value == "owner"
    await db.update_chat_settings(message.chat.id, chat_settings)
    await rich_send(
        bot,
        message.chat.id,
        ui.panel(
            ui.ok(f"filters are now <b>{value}</b>-managed here"), icon="crown"
        ),
    )


@router.message(Command("filters"))
async def cmd_filters(message: Message, bot: Bot, db: Database) -> None:
    """``/filters [search]`` - the management panel."""
    query = (message.text or "").partition(" ")[2].strip().lower()
    rows = await db.list_filters(message.chat.id)
    if query:
        rows = [row for row in rows if query in row["trigger"]]
    await rich_send(bot, message.chat.id, _panel(message.chat.title or "", rows, query=query), reply_markup=_panel_keys(rows))


def _panel(chat_title: str, rows: list[Any], *, query: str = "") -> str:
    if not rows:
        return ui.screen(
            "filters",
            icon="magnet",
            subtitle=chat_title,
            blocks_=[
                ui.panel(
                    ui.italic(ui.caps("no filters yet"))
                    if not query
                    else ui.no(f"nothing matching <b>{ui.esc(query)}</b>")
                ),
                ui.details(
                    "how to add one",
                    ui.list_ordered(
                        [
                            "<code>/filter hello hi there!</code>",
                            "reply to a message with <code>/filter hello</code> to save media",
                            "<code>/stop hello</code> to delete it",
                        ]
                    ),
                    icon="book",
                ),
            ],
        )
    table = ui.table(
        ["ᴛʀɪɢɢᴇʀ", "ᴛʏᴘᴇ", "ᴜꜱᴇꜱ", "ʀᴇᴘʟʏ"],
        [
            (
                f"#{ui.esc(row['trigger'])}",
                row["kind"],
                str(row["uses"]),
                ui.esc((row["reply"] or "")[:42] or "(media)"),
            )
            for row in rows[:20]
        ],
    )
    return ui.screen(
        "filters",
        icon="magnet",
        subtitle=f"{chat_title} · {len(rows)} total",
        blocks_=[
            table,
            ui.italic("ᴛᴀᴘ ᴀ ʙᴜᴛᴛᴏɴ ᴛᴏ ᴅᴇʟᴇᴛᴇ · <code>/filters word</code> to search"),
        ],
    )


def _panel_keys(rows: list[Any]) -> Any:
    if not rows:
        return keys.keyboard([keys.button("How to add", callback="flt:help", style="primary", icon="book")])
    buttons = [
        keys.button(f"✕ {row['trigger']}", callback=f"flt:del:{row['trigger']}", style="danger", icon="trash")
        for row in rows[:20]
    ]
    grid = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    grid.append(
        [
            keys.button("Refresh", callback="flt:list", style="primary", icon="refresh"),
            keys.button("Delete all", callback="flt:clear:ask", style="danger", icon="trash"),
        ]
    )
    return keys.keyboard(*grid)


@router.callback_query(F.data.startswith("flt:"))
async def on_filter_callback(callback: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    """Panel buttons: delete, clear, refresh, mode."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    chat_id = callback.message.chat.id
    parts = (callback.data or "").split(":", 2)
    action = parts[1] if len(parts) > 1 else ""

    is_owner_user = is_owner(callback.from_user, settings)
    chat_settings = await db.get_chat_settings(chat_id)
    owner_only = bool(chat_settings.get(OWNER_ONLY_KEY))
    allowed = is_owner_user or (
        not owner_only and await is_admin(bot, chat_id, callback.from_user.id, settings)
    )
    if not allowed:
        await callback.answer("admins only", show_alert=True)
        return

    if action == "help":
        await callback.answer("/filter hello hi there!", show_alert=True)
        return

    if action == "list":
        rows = await db.list_filters(chat_id)
        await rich_edit(
            bot,
            chat_id=chat_id,
            message_id=callback.message.message_id,
            html=_panel(getattr(callback.message.chat, "title", "") or "", rows),
        )
        await callback.answer(f"{len(rows)} filters")
        return

    if action == "del" and len(parts) > 2:
        trigger = parts[2].lstrip("/#").lower()
        removed = await db.delete_filter(chat_id, trigger)
        rows = await db.list_filters(chat_id)
        await rich_edit(
            bot,
            chat_id=chat_id,
            message_id=callback.message.message_id,
            html=_panel(getattr(callback.message.chat, "title", "") or "", rows),
        )
        await callback.answer(f"deleted #{trigger}" if removed else "already gone")
        return

    if action == "clear" and len(parts) > 2 and parts[2] == "yes":
        count = await db.clear_filters(chat_id)
        rows = await db.list_filters(chat_id)
        await rich_edit(
            bot,
            chat_id=chat_id,
            message_id=callback.message.message_id,
            html=_panel(getattr(callback.message.chat, "title", "") or "", rows),
        )
        await callback.answer(f"deleted {count} filters")
        return

    if action == "clear" and len(parts) > 2 and parts[2] == "ask":
        count = await db.count_filters(chat_id)
        await rich_send(
            bot,
            chat_id,
            ui.panel(ui.no(f"delete all <b>{count}</b> filters? this cannot be undone"), icon="warning"),
            reply_markup=keys.confirm("flt:clear:yes", yes_label="Delete all", no_label="Keep"),
        )
        await callback.answer()
        return

    if action == "mode" and len(parts) > 2 and is_owner_user:
        chat_settings[OWNER_ONLY_KEY] = parts[2] == "owner"
        await db.update_chat_settings(chat_id, chat_settings)
        await callback.answer(f"filters: {parts[2]}")
        return

    await callback.answer()
