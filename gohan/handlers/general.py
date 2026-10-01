"""``/start``, ``/help``, ``/status``, ``/id``, ``/notes`` and the menu callbacks."""

from __future__ import annotations

import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, ReplyParameters

from .. import __version__
from ..config import Settings, detect_host
from ..content import HELP_SECTIONS, help_screen
from ..filters import IsGroup, is_owner
from ..logging_setup import get_logger
from ..rich import ui
from ..rich.sender import rich_edit, rich_reply, rich_send
from ..storage import Database

log = get_logger("handlers.general")

router = Router(name="general")

__all__ = ["router"]

BOOT_TIME = time.time()

#: The bot's own description - the owner's words, verbatim.
WELCOME_INTRO = (
    "✨ ɪ'ᴍ <b>ɢᴏʜᴀɴ</b> 🧚‍♂️\n\n"
    "ɪ ʜᴀᴠᴇ ʟᴏᴛꜱ ᴏꜰ ꜰᴇᴀᴛᴜʀᴇꜱ ʟɪᴋᴇ ᴀɪ ᴄʜᴀᴛʙᴏᴛ, ᴀɴɪᴍᴇ, ᴍᴜꜱɪᴄ, ɴᴏᴛᴇꜱ, ꜰɪʟᴛᴇʀꜱ, ꜰᴜɴ "
    "ᴀɴᴅ ᴍᴀɴʏ ᴏᴛʜᴇʀ ᴜꜱᴇꜰᴜʟ ᴄᴏᴍᴍᴀɴᴅꜱ!\n\n"
    "ᴛʜɪꜱ ɪꜱ ᴍʏ ᴘʟᴀɴ ᴏꜰ ʙᴏᴛ 😺"
)


def _menu_buttons(is_owner_user: bool, *, bot_username: str | None = None) -> str:
    rows = [
        [
            ui.button("Guardian", callback="help:guardian", icon="shield"),
            ui.button("Games", callback="help:games", icon="game"),
        ],
        [
            ui.button("Fun", callback="help:fun", icon="sparkles"),
            ui.button("Assistant", callback="help:ai", icon="robot"),
        ],
        [
            ui.button("Status", callback="menu:status", icon="chart"),
            ui.button(
                "Add to group",
                url=(
                    f"https://t.me/{bot_username}?startgroup=true"
                    if bot_username
                    else "https://t.me/BotFather"
                ),
                style="link",
                icon="plus",
            ),
        ],
    ]
    if is_owner_user:
        rows.append([ui.button("Owner tools", callback="help:owner", icon="crown")])
    return ui.actions(*rows)


@router.message(Command("start"))
async def cmd_start(
    message: Message,
    bot: Bot,
    db: Database,
    settings: Settings,
    log_channel: Any = None,
) -> None:
    """The front door: who GOHAN is and what he can do."""
    user = message.from_user
    if user is not None:
        try:
            await db.upsert_user(
                user.id,
                username=user.username,
                first_name=user.first_name,
                language_code=user.language_code,
                is_premium=bool(user.is_premium),
                counted_start=True,
            )
            if log_channel is not None and log_channel.should_log_start(user.id):
                await log_channel.event(
                    "NewUser",
                    ui.screen("new user", icon="wave", blocks_=[log_channel.user_block(user)]),
                    dm=False,
                )
        except Exception as exc:
            log.debug("start bookkeeping failed: %s", exc)

    owner = is_owner(user, settings)
    bot_username = getattr(bot, "username", None)
    if not bot_username:
        try:
            bot_username = (await bot.get_me()).username
        except Exception:
            bot_username = None
    body = ui.screen(
        settings.bot_name,
        icon="robot",
        subtitle="ᴀɪ · ᴀɴɪᴍᴇ · ᴍᴜꜱɪᴄ · ɴᴏᴛᴇꜱ · ꜰɪʟᴛᴇʀꜱ · ꜰᴜɴ · ɢᴀᴍᴇꜱ",
        blocks_=[
            ui.panel(WELCOME_INTRO, expandable=False),
            ui.panel(
                ui.kv("🛡 ɢᴜᴀʀᴅɪᴀɴ", "ꜱᴘᴀᴍ · ʀᴀɪᴅꜱ · ᴄᴀᴘᴛᴄʜᴀ · ᴡᴀʀɴꜱ"),
                ui.kv("🔮 ᴀɪ ᴄʜᴀᴛ", "/ᴀꜱᴋ · ꜱᴛʀᴇᴀᴍᴇᴅ ᴀɴꜱᴡᴇʀꜱ"),
                ui.kv("🎬 ᴀɴɪᴍᴇ", "/ᴀɴɪᴍᴇ · /ᴛʀᴇɴᴅɪɴɢ · /ᴄʜᴀʀᴀᴄᴛᴇʀ"),
                ui.kv("🎧 ᴍᴜꜱɪᴄ", "/ᴍᴜꜱɪᴄ · /ᴅᴏᴡɴʟᴏᴀᴅ"),
                ui.kv("📝 ɴᴏᴛᴇꜱ", "/ꜱᴀᴠᴇ · /ɴᴏᴛᴇꜱ · #ɴᴏᴛᴇ"),
                ui.kv("🧲 ꜰɪʟᴛᴇʀꜱ", "/ꜰɪʟᴛᴇʀ · /ꜰɪʟᴛᴇʀꜱ"),
                ui.kv("🎮 ɢᴀᴍᴇꜱ", "ǫᴜɪᴢ · ᴡᴏʀᴅ ᴄʜᴀɪɴ · ᴅɪᴄᴇ · ꜱʟᴏᴛꜱ"),
                ui.kv("✨ ꜰᴜɴ", "19 ᴀᴄᴛɪᴏɴꜱ · /ʜᴜɢ /ᴋɪꜱꜱ /ꜱʟᴀᴘ"),
                icon="sparkles",
            ),
            ui.kv_panel(
                [
                    ("ᴠᴇʀꜱɪᴏɴ", f"<code>{__version__}</code>"),
                    ("ᴜᴘᴛɪᴍᴇ", ui.uptime(time.time() - BOOT_TIME)),
                    ("ʜᴏꜱᴛ", ui.esc(detect_host())),
                ],
                icon="chart",
            ),
            ui.details(
                "add me to a group",
                ui.list_ordered(
                    [
                        "ᴛᴀᴘ <b>ᴀᴅᴅ ᴛᴏ ɢʀᴏᴜᴘ</b> ʙᴇʟᴏᴡ",
                        "ɢʀᴀɴᴛ ᴍᴇ <b>ᴅᴇʟᴇᴛᴇ ᴍᴇꜱꜱᴀɢᴇꜱ</b> ᴀɴᴅ <b>ʀᴇꜱᴛʀɪᴄᴛ ᴍᴇᴍʙᴇʀꜱ</b>",
                        "ᴛʜᴇ ɢᴜᴀʀᴅɪᴀɴ ꜱᴛᴀʀᴛꜱ ᴡᴏʀᴋɪɴɢ ᴀᴜᴛᴏᴍᴀᴛɪᴄᴀʟʟʏ",
                        "ᴛʜᴇ ᴏᴡɴᴇʀ ɢᴇᴛꜱ ᴀ ᴍᴇꜱꜱᴀɢᴇ ᴡʜᴇɴ ʏᴏᴜ ᴀᴅᴅ ᴍᴇ",
                    ]
                ),
                icon="plus",
            ),
        ],
    )
    await rich_send(
        bot, message.chat.id, body + "\n\n" + _menu_buttons(owner, bot_username=bot_username)
    )


@router.message(Command("help"))
async def cmd_help(
    message: Message,
    command: CommandObject,
    bot: Bot,
    settings: Settings,
) -> None:
    """``/help [section]`` - the command index."""
    tag = (command.args or "").strip().lower()
    owner = is_owner(message.from_user, settings)
    body = help_screen(tag or None, is_owner=owner)
    await rich_send(bot, message.chat.id, body, fallback=True)


@router.callback_query(F.data.startswith("help:"))
async def on_help_button(callback: CallbackQuery, bot: Bot, settings: Settings) -> None:
    """Section buttons from ``/help``."""
    tag = (callback.data or "").split(":", 1)[-1]
    owner = is_owner(callback.from_user, settings)
    if tag == "owner" and not owner:
        await callback.answer("owner only", show_alert=True)
        return
    if callback.message is None:
        await callback.answer()
        return
    body = help_screen(tag if tag in HELP_SECTIONS else None, is_owner=owner)
    if tag in HELP_SECTIONS:
        body += "\n\n" + ui.actions([ui.button("All sections", callback="help:index", style="primary", icon="book")])
    await rich_edit(
        bot,
        chat_id=callback.message.chat.id,  # type: ignore[union-attr]
        message_id=callback.message.message_id,  # type: ignore[union-attr]
        html=body,
    )
    await callback.answer()


@router.callback_query(F.data == "help:index")
async def on_help_index(callback: CallbackQuery, bot: Bot, settings: Settings) -> None:
    if callback.message is None:
        await callback.answer()
        return
    await rich_edit(
        bot,
        chat_id=callback.message.chat.id,  # type: ignore[union-attr]
        message_id=callback.message.message_id,  # type: ignore[union-attr]
        html=help_screen(None, is_owner=is_owner(callback.from_user, settings)),
    )
    await callback.answer("index")


@router.message(Command(commands=["id", "whoami"]))
async def cmd_id(message: Message, bot: Bot) -> None:
    """Show ids - handy for configuring owners and log channels."""
    rows: list[tuple[str, str]] = [
        ("👤 ʏᴏᴜʀ ɪᴅ", f"<code>{message.from_user.id}</code>"),
        ("💬 ᴄʜᴀᴛ ɪᴅ", f"<code>{message.chat.id}</code>"),
    ]
    if message.reply_to_message and message.reply_to_message.from_user:
        other = message.reply_to_message.from_user
        rows.append(("↩️ ʀᴇᴘʟɪᴇᴅ ᴛᴏ", f"<code>{other.id}</code>"))
    await rich_reply(
        message,
        ui.screen("ids", icon="info", blocks_=[ui.kv_panel(rows)]),
    )


@router.message(Command("notes"), IsGroup())
async def cmd_notes(message: Message, bot: Bot, db: Database) -> None:
    """``/notes`` - saved notes for this chat (add with ``/save``)."""
    names = await db.list_notes(message.chat.id)
    body = ui.screen(
        "notes",
        icon="memo",
        subtitle=message.chat.title or "",
        blocks_=[
            ui.panel(" · ".join(f"#{ui.esc(n)}" for n in names))
            if names
            else ui.italic("no notes yet - an admin can add one with /save <name> <text>"),
            ui.italic("read one by typing its <code>#name</code> in the chat"),
        ],
    )
    await rich_reply(message, body)


@router.message(Command("save"), IsGroup())
async def cmd_save(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    """``/save <name> <text>`` - store a note (admins only)."""
    from ..filters import is_admin

    if message.from_user is None or not await is_admin(
        bot, message.chat.id, message.from_user.id, settings
    ):
        await rich_reply(message, ui.panel(ui.no("admins only"), icon="lock"))
        return
    args = (command.args or "").strip()
    name, _, text = args.partition(" ")
    if not name or not text:
        await rich_reply(message, ui.panel("usage: <code>/save rules be nice</code>", icon="question"))
        return
    await db.save_note(message.chat.id, name, text, created_by=message.from_user.id)
    await rich_reply(message, ui.panel(ui.ok(f"saved note <b>#{ui.esc(name)}</b>"), icon="check"))


@router.message(F.text.regexp(r"^#(\w{1,32})$").as_("note"), IsGroup())
async def on_note_lookup(message: Message, bot: Bot, db: Database, note: Any = None) -> None:
    """Type ``#name`` to print a saved note."""
    name = note.group(1) if note else ""
    row = await db.get_note(message.chat.id, name)
    if row is None:
        return
    await rich_send(
        bot,
        message.chat.id,
        ui.screen(
            f"#{row['name']}",
            icon="memo",
            subtitle=message.chat.title or "",
            blocks_=[ui.panel(ui.esc(row["content"] or ""))],
        ),
    )


@router.message(Command("ping"))
async def cmd_ping(message: Message, bot: Bot) -> None:
    """Latency check."""
    started = time.perf_counter()
    sent = await bot.send_message(message.chat.id, "…")
    elapsed = (time.perf_counter() - started) * 1000
    try:
        await bot.delete_message(message.chat.id, sent.message_id)
    except Exception:
        pass
    await rich_send(
        bot,
        message.chat.id,
        ui.panel(
            ui.kv("🏓 ᴘᴏɴɢ", f"<b>{elapsed:.0f} ms</b>"),
            icon="satellite",
        ),
        reply_parameters=ReplyParameters(message_id=message.message_id),
    )


@router.callback_query(F.data.startswith("menu:"))
async def on_menu(callback: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    """Buttons on ``/start``."""
    action = (callback.data or "").split(":", 1)[-1]
    if callback.message is None:
        await callback.answer()
        return
    if action == "status":
        stats = await db.stats()
        body = ui.screen(
            "status",
            icon="heart_pulse",
            blocks_=[
                ui.kv_panel(
                    [
                        ("🟢 ꜱᴛᴀᴛᴇ", "online"),
                        ("⏱ ᴜᴘᴛɪᴍᴇ", ui.uptime(time.time() - BOOT_TIME)),
                        ("👥 ᴜꜱᴇʀꜱ", f"<code>{stats['users']}</code>"),
                        ("💬 ᴄʜᴀᴛꜱ", f"<code>{stats['chats']}</code>"),
                        ("🎮 ɢᴀᴍᴇꜱ 24ʜ", f"<code>{stats['games']}</code>"),
                    ],
                    icon="chart",
                ),
            ],
        )
        await rich_edit(
            bot,
            chat_id=callback.message.chat.id,  # type: ignore[union-attr]
            message_id=callback.message.message_id,  # type: ignore[union-attr]
            html=body,
        )
        await callback.answer("updated")
        return
    await callback.answer()


# ---------------------------------------------------------------------------
#  group membership: the owner always knows where their bot ended up
# ---------------------------------------------------------------------------


@router.my_chat_member()
async def on_bot_membership(
    event: Any,
    bot: Bot,
    db: Database,
    settings: Settings,
    log_channel: Any = None,
) -> None:
    """Fire when the bot is added to, promoted in, or removed from a chat.

    ``my_chat_member`` is the Bot API's own update type for this - no polling and
    no guessing, so the owner hears about every new group immediately.
    """
    from aiogram.enums import ChatMemberStatus as Status

    chat = event.chat
    new_status = event.new_chat_member.status
    was = event.old_chat_member.status if event.old_chat_member else None

    added = new_status in (Status.MEMBER, Status.ADMINISTRATOR) and was not in (
        Status.MEMBER,
        Status.ADMINISTRATOR,
    )
    removed = new_status in (Status.LEFT, Status.KICKED) and was in (
        Status.MEMBER,
        Status.ADMINISTRATOR,
    )
    promoted = new_status == Status.ADMINISTRATOR and was == Status.MEMBER
    if not (added or removed or promoted):
        return

    actor = getattr(event, "from_user", None)
    actor_text = "—"
    if actor is not None:
        actor_text = (
            f"@{actor.username}" if actor.username else ui.mention(actor.full_name, actor.id)
        )

    if added:
        await db.upsert_chat(
            chat.id,
            title=chat.title,
            type=chat.type,
            username=chat.username,
            added_by=actor.id if actor else None,
        )
    elif removed:
        await db.set_chat_active(chat.id, False)

    if log_channel is None:
        return

    what = "added to a group" if added else ("removed from a group" if removed else "promoted to admin")
    icon = "plus" if added else ("door" if removed else "crown")
    body = ui.screen(
        what,
        icon=icon,
        subtitle=chat.title or str(chat.id),
        blocks_=[
            ui.kv_panel(
                [
                    ("💬 ᴄʜᴀᴛ", ui.esc(chat.title or "?")),
                    ("🆔 ɪᴅ", f"<code>{chat.id}</code>"),
                    ("👤 ʙʏ", actor_text),
                    ("📊 ᴛʏᴘᴇ", str(chat.type)),
                    ("📈 ɢʀᴏᴜᴘꜱ", f"<code>{await db.count_chats()}</code>"),
                ],
                icon="users",
            ),
            ui.italic(
                "ᴛʜᴇ ɢᴜᴀʀᴅɪᴀɴ ɪꜱ ɴᴏᴡ ᴡᴀᴛᴄʜɪɴɢ ᴛʜɪꜱ ᴄʜᴀᴛ"
                if added
                else "ᴛʜɪꜱ ᴄʜᴀᴛ ɪꜱ ᴍᴀʀᴋᴇᴅ ɪɴᴀᴄᴛɪᴠᴇ"
            ),
        ],
    )
    # dm=True guarantees the owner sees it even when LOG_CHANNEL_ID is set
    await log_channel.event(
        "GroupAdded" if added else ("GroupRemoved" if removed else "GroupPromoted"),
        body,
        dm=True,
        dedupe=False,
    )
