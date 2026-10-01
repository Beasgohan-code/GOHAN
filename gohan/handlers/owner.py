"""Owner-only commands and the owner **control panel**.

Everything here is gated by :class:`gohan.filters.IsOwner`, which reads
``OWNER_USER_IDS``. The panel is the single place an owner needs:

``/owner``    home screen with live counters and colour-coded buttons
``own:``      every button in the panel (stats, groups, users, filters, logs,
              watchdog, backups, maintenance, broadcast, restart, shutdown)

Restart and shutdown are always behind a green/red confirmation, and while the
bot is off the owner still learns about it - :mod:`gohan.__main__` sends the
"stopped" notice before exiting.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, FSInputFile, Message

from .. import __version__
from ..config import Settings, detect_host
from ..filters import IsOwner
from ..logging_setup import get_logger
from ..rich import keys
from ..rich import ui
from ..rich.sender import rich_edit, rich_send
from ..storage import Database

log = get_logger("handlers.owner")

router = Router(name="owner")
router.message.filter(IsOwner())
router.callback_query.filter(IsOwner())

__all__ = ["OWNER_PAGES", "SERVICES", "home_body", "home_keyboard", "router"]

#: filled by :func:`gohan.dispatcher.build_runtime`
SERVICES: dict[str, Any] = {}

#: pages the panel can render: name -> (title, icon)
OWNER_PAGES: dict[str, tuple[str, str]] = {
    "home": ("owner panel", "crown"),
    "stats": ("statistics", "chart"),
    "groups": ("groups", "users"),
    "users": ("recent users", "user"),
    "filters": ("top filters", "magnet"),
    "logs": ("event log", "memo"),
    "system": ("system", "satellite"),
    "watchdog": ("watchdog", "broom"),
    "ai": ("assistant", "robot"),
}


# ---------------------------------------------------------------------------
#  data for the screens
# ---------------------------------------------------------------------------


async def _system_rows(db: Database, settings: Settings) -> list[tuple[str, str]]:
    mtproto = SERVICES.get("mtproto")
    watchdog = SERVICES.get("watchdog")
    keep_alive = SERVICES.get("keep_alive")
    ai = SERVICES.get("ai")
    started = float(SERVICES.get("started_at") or time.time())
    pid = os.getpid()

    rows: list[tuple[str, str]] = [
        ("🤖 ᴠᴇʀꜱɪᴏɴ", f"<code>{__version__}</code>"),
        ("🧬 ᴘɪᴅ", f"<code>{pid}</code>"),
        ("⏱ ᴜᴘᴛɪᴍᴇ", ui.uptime(time.time() - started)),
        ("🖥 ʜᴏꜱᴛ", ui.esc(detect_host())),
        ("🌐 ᴡᴇʙ", f"port <code>{settings.port}</code>" if settings.keep_alive else "off"),
        ("📡 ᴍᴛᴘʀᴏᴛᴏ", ui.esc(mtproto.status.describe() if mtproto else "off")),
        ("🧠 ʟʟᴍ", ui.esc(getattr(getattr(ai, "llm", None), "model", "off"))),
        ("🧹 ᴡᴀᴛᴄʜᴅᴏɢ", f"{watchdog.stats.sweeps} sweeps" if watchdog else "off"),
        ("📨 ʟᴏɢ", f"<code>{settings.log_channel_id or 'owner dm'}</code>"),
    ]
    if keep_alive is not None:
        rows.append(("🏓 ꜱᴇʟꜰ-ᴘɪɴɢ", getattr(keep_alive, "_pings", 0).__str__()))
    try:
        free = shutil.disk_usage(".").free / 1_073_741_824
        rows.append(("💽 ꜰʀᴇᴇ ᴅɪꜱᴋ", f"<code>{free:.1f} GB</code>"))
    except OSError:
        pass
    return rows


async def home_body(db: Database, settings: Settings) -> str:
    """The panel's home screen."""
    stats = await db.stats()
    system = await _system_rows(db, settings)
    maintenance = bool(await db.get_kv("maintenance", False))

    return ui.screen(
        settings.bot_name,
        icon="crown",
        subtitle="control panel · every action is one tap away",
        blocks_=[
            ui.kv_panel(
                [
                    ("🟢 ꜱᴛᴀᴛᴇ", "maintenance" if maintenance else "running"),
                    ("👥 ᴜꜱᴇʀꜱ", f"<code>{stats['users']}</code> (+{stats['users_new']} 24h)"),
                    ("💬 ɢʀᴏᴜᴘꜱ", f"<code>{stats['chats']}</code>"),
                    ("🎮 ɢᴀᴍᴇꜱ 24ʜ", f"<code>{stats['games']}</code>"),
                    ("🚫 ʙᴀɴɴᴇᴅ", f"<code>{stats['banned']}</code>"),
                ],
                icon="chart",
            ),
            ui.kv_panel(system, icon="satellite"),
            ui.italic("ᴛᴀᴘ ᴀ ᴄᴏʟᴏᴜʀ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ · ᴛʜᴇ ᴘᴀɴᴇʟ ᴜᴘᴅᴀᴛᴇꜱ ɪɴ ᴘʟᴀᴄᴇ"),
        ],
    )


def home_keyboard(*, maintenance: bool = False) -> Any:
    """Colour-coded navigation for the whole panel."""
    return keys.keyboard(
        [
            keys.button("Stats", callback="own:stats", style="primary", icon="chart"),
            keys.button("Groups", callback="own:groups", style="primary", icon="users"),
            keys.button("Users", callback="own:users", style="primary", icon="user"),
        ],
        [
            keys.button("Watchdog", callback="own:watchdog", style="primary", icon="broom"),
            keys.button("Filters", callback="own:filters", style="primary", icon="magnet"),
            keys.button("Logs", callback="own:logs", style="primary", icon="memo"),
        ],
        [
            keys.button("System", callback="own:system", style="primary", icon="satellite"),
            keys.button("Assistant", callback="own:ai", style="primary", icon="robot"),
            keys.button("Refresh", callback="own:home", style="primary", icon="refresh"),
        ],
        [
            keys.button("Broadcast", callback="own:cast:ask", style="success", icon="megaphone"),
            keys.button("Backup", callback="own:backup", style="success", icon="save"),
            keys.button("Maintenance", callback="own:maint:off" if maintenance else "own:maint:on", style="danger", icon="tools"),
        ],
        [
            keys.button("Restart bot", callback="own:restart:ask", style="danger", icon="refresh"),
            keys.button("Shut down", callback="own:stop:ask", style="danger", icon="stop2"),
            keys.button("Close", callback="own:close", style="link", icon="cross"),
        ],
    )


async def _render(db: Database, settings: Settings, page: str) -> str:
    """Build one panel page."""
    if page == "stats":
        tables = await db.table_sizes()
        events = await db.event_counts(time.time() - 7 * 86400)
        games = await db.game_stats()
        return ui.screen(
            "statistics",
            icon="chart",
            blocks_=[
                ui.table(["ᴛᴀʙʟᴇ", "ʀᴏᴡꜱ"], [[name, str(size)] for name, size in tables.items()]),
                ui.table(["ɢᴀᴍᴇ", "ᴘʟᴀʏꜱ"], [[name, str(count)] for name, count in games[:8]])
                if games
                else ui.italic("no games played yet"),
                ui.table(["ᴇᴠᴇɴᴛ (7ᴅ)", "ᴄᴏᴜɴᴛ"], [[f"#{tag}", str(count)] for tag, count in list(events.items())[:10]])
                if events
                else ui.italic("no events yet"),
            ],
        )

    if page == "groups":
        chats = await db.recent_chats(limit=12, active_only=False)
        if not chats:
            return ui.screen("groups", icon="users", blocks_=[ui.italic("not in any group yet")])
        rows = [
            (
                ui.esc((row["title"] or str(row["chat_id"]))[:34]),
                f"<code>{row['chat_id']}</code>",
                "yes" if row["is_active"] else "no",
            )
            for row in chats
        ]
        return ui.screen(
            "groups",
            icon="users",
            subtitle=f"{await db.count_chats()} active",
            blocks_=[
                ui.table(["ᴛɪᴛʟᴇ", "ᴄʜᴀᴛ ɪᴅ", "ᴀᴄᴛɪᴠᴇ"], rows, raw=True),
                ui.italic("ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴏʀ ᴛᴏɢɢʟᴇ ᴀ ᴄʜᴀᴛ"),
            ],
        )

    if page == "users":
        users = await db.recent_users(limit=12)
        rows = [
            (
                ui.esc((row["first_name"] or "?")[:24]),
                f"@{row['username']}" if row["username"] else "—",
                f"<code>{row['user_id']}</code>",
                "🚫" if row["is_banned"] else "✓",
            )
            for row in users
        ]
        return ui.screen(
            "recent users",
            icon="user",
            blocks_=[ui.table(["ɴᴀᴍᴇ", "ᴜꜱᴇʀɴᴀᴍᴇ", "ɪᴅ", "ꜱᴛᴀᴛᴜꜱ"], rows, raw=True)],
        )

    if page == "filters":
        rows = await db.top_filters(limit=12)
        return ui.screen(
            "filters",
            icon="magnet",
            blocks_=[
                ui.table(
                    ["ᴛʀɪɢɢᴇʀ", "ᴄʜᴀᴛꜱ"],
                    [[f"#{ui.esc(row['trigger'])}", str(row["triggers"])] for row in rows],
                    raw=True,
                )
                if rows
                else ui.italic("no filters anywhere yet"),
                ui.italic("ɢʀᴏᴜᴘ ᴀᴅᴍɪɴꜱ ᴍᴀɴᴀɢᴇ ᴛʜᴇɪʀ ᴏᴡɴ ᴡɪᴛʜ <code>/filters</code>"),
            ],
        )

    if page == "logs":
        rows = await db.events_since(time.time() - 3 * 86400)
        table = (
            ui.table(
                ["ᴇᴠᴇɴᴛ", "ᴄʜᴀᴛ", "ᴡʜᴇɴ"],
                [
                    [
                        f"#{ui.esc(row['tag'])}",
                        f"<code>{row['chat_id'] or '-'}</code>",
                        datetime.fromtimestamp(row["ts"]).strftime("%d %b %H:%M"),
                    ]
                    for row in rows[:20]
                ],
                raw=True,
            )
            if rows
            else ui.italic("no events recorded")
        )
        return ui.screen("event log", icon="memo", subtitle="last 3 days", blocks_=[table])

    if page == "system":
        return ui.screen("system", icon="satellite", blocks_=[ui.kv_panel(await _system_rows(db, settings), icon="eye")])

    if page == "watchdog":
        watchdog = SERVICES.get("watchdog")
        if watchdog is None:
            return ui.screen("watchdog", icon="broom", blocks_=[ui.no("disabled (WATCHDOG=false)")])
        summary = watchdog.stats.summary()
        last = summary.get("last") or {}
        return ui.screen(
            "watchdog",
            icon="broom",
            subtitle=f"sweep every {settings.watchdog_interval}s · cleanup after {settings.cleanup_after_hours}h",
            blocks_=[
                ui.kv_panel(
                    [
                        ("🧹 ꜱᴡᴇᴇᴘꜱ", f"<code>{summary['sweeps']}</code>"),
                        ("🗑 ꜰɪʟᴇꜱ ʀᴇᴍᴏᴠᴇᴅ", f"<code>{summary['files_removed']}</code>"),
                        ("💾 ꜰʀᴇᴇᴅ", f"<code>{summary['freed_mb']} MB</code>"),
                        ("💽 ꜰʀᴇᴇ ᴅɪꜱᴋ", f"<code>{last.get('free_disk_gb', '?')} GB</code>"),
                        ("🧠 ʀᴀᴍ", f"<code>{last.get('ram_mb', '?')} MB</code>"),
                        ("📅 ʟᴀꜱᴛ", str(last.get("at", "never"))),
                    ],
                    icon="eye",
                ),
            ],
        )

    if page == "ai":
        ai = SERVICES.get("ai")
        llm = getattr(ai, "llm", None)
        live = bool(getattr(llm, "api_key", None))
        return ui.screen(
            "assistant",
            icon="robot",
            blocks_=[
                ui.kv_panel(
                    [
                        ("🧠 ᴍᴏᴅᴇʟ", ui.esc(getattr(llm, "model", "demo"))),
                        ("🔌 ᴘʀᴏᴠɪᴅᴇʀ", ui.esc(settings.llm_provider)),
                        ("🔑 ᴀᴘɪ ᴋᴇʏ", ui.badge(live, yes="configured", no_="demo mode")),
                        ("🌊 ꜱᴛʀᴇᴀᴍɪɴɢ", "private chats"),
                    ],
                    icon="eye",
                ),
                ui.panel(
                    ui.italic(
                        "ᴛʜᴇ ʙᴏᴛ ʀᴜɴꜱ ꜰɪɴᴇ ᴡɪᴛʜᴏᴜᴛ ᴀ ᴋᴇʏ - "
                        "/ᴀꜱᴋ ᴀɴꜱᴡᴇʀꜱ ᴡɪᴛʜ ᴀ ʟᴏᴄᴀʟ ᴅᴇᴍᴏ ɢᴇɴᴇʀᴀᴛᴏʀ"
                    )
                ),
                ui.details(
                    "how to switch it on",
                    ui.list_ordered(
                        [
                            "<code>LLM_PROVIDER=openai_compatible</code>",
                            "<code>LLM_API_KEY=sk-…</code>",
                            "<code>LLM_BASE_URL=https://api.openai.com/v1</code> (or any compatible)",
                            "<code>LLM_MODEL=gpt-4o-mini</code>",
                        ]
                    ),
                    icon="book",
                    open=True,
                ),
            ],
        )

    return await home_body(db, settings)


def _page_keyboard(page: str) -> Any:
    """Per-page footer, always with Back / Refresh / Close."""
    extra: list[list[Any]] = []
    if page == "watchdog":
        extra.append(
            [
                keys.button("Sweep now", callback="wd:run", style="success", icon="broom"),
                keys.button("Aggressive", callback="wd:clean", style="danger", icon="fire"),
            ]
        )
    if page == "groups":
        extra.append(
            [
                keys.button("Broadcast", callback="own:cast:ask", style="success", icon="megaphone"),
                keys.button("Add to a group", url="https://t.me/", style="link", icon="plus"),
            ]
        )
    body = extra or [[keys.button("Refresh", callback=f"own:{page}", style="primary", icon="refresh")]]
    return keys.with_back(body, back="own:home", close="own:close")


# ---------------------------------------------------------------------------
#  commands
# ---------------------------------------------------------------------------


@router.message(Command(commands=["owner", "panel", "admin"]))
async def cmd_owner(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    """The control panel."""
    maintenance = bool(await db.get_kv("maintenance", False))
    await rich_send(
        bot,
        message.chat.id,
        await home_body(db, settings),
        reply_markup=home_keyboard(maintenance=maintenance),
    )


@router.callback_query(F.data.startswith("own:"))
async def on_owner_callback(callback: CallbackQuery, bot: Bot, db: Database, settings: Settings, log_channel: Any = None) -> None:
    """Every button in the panel."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    page = parts[1] if len(parts) > 1 else "home"
    extra = parts[2] if len(parts) > 2 else ""
    chat_id = callback.message.chat.id
    message_id = callback.message.message_id

    async def render(target: str) -> None:
        maintenance = bool(await db.get_kv("maintenance", False))
        await rich_edit(
            bot,
            chat_id=chat_id,
            message_id=message_id,
            html=await _render(db, settings, target),
            reply_markup=home_keyboard(maintenance=maintenance) if target == "home" else _page_keyboard(target),
        )

    # -- navigation ------------------------------------------------------
    if page in OWNER_PAGES:
        await render(page)
        await callback.answer(OWNER_PAGES[page][0])
        return

    if page == "close":
        await callback.answer("closed")
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
        return

    # -- maintenance -----------------------------------------------------
    if page == "maint" and extra in ("on", "off"):
        await db.set_kv("maintenance", extra == "on")
        await render("home")
        await callback.answer(f"maintenance {extra}")
        if log_channel is not None:
            await log_channel.event(
                "Maintenance",
                ui.panel(ui.kv("🛠 ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ", extra), icon="tools"),
                dm=False,
            )
        return

    # -- broadcast -------------------------------------------------------
    if page == "cast":
        if extra == "ask":
            await callback.answer()
            await rich_send(
                bot,
                chat_id,
                ui.panel(
                    "ꜱᴇɴᴅ <code>/broadcast your message</code> ᴛᴏ ʀᴇᴀᴄʜ ᴇᴠᴇʀʏᴏɴᴇ",
                    icon="megaphone",
                ),
            )
            return
        if extra == "send":
            count = await _broadcast(bot, db, parts[3] if len(parts) > 3 else "")
            await callback.answer(f"sent to {count} users")
            return

    # -- backup ----------------------------------------------------------
    if page == "backup":
        await callback.answer("preparing…")
        path = await _backup_file(settings)
        if path is None:
            await callback.answer("no database yet", show_alert=True)
            return
        try:
            await bot.send_document(
                chat_id,
                FSInputFile(path, filename=path.name),
                caption=ui.caps(f"{settings.bot_name} backup · {path.stat().st_size // 1024} kb"),
            )
        finally:
            path.unlink(missing_ok=True)
        return

    # -- restart / shutdown (always confirmed) ---------------------------
    if page == "restart":
        if extra == "ask":
            await callback.answer()
            await rich_send(
                bot,
                chat_id,
                ui.screen(
                    "restart",
                    icon="refresh",
                    blocks_=[
                        ui.panel(
                            "ᴛʜᴇ ʙᴏᴛ ᴡɪʟʟ ʀᴇꜱᴛᴀʀᴛ ɴᴏᴡ. ᴇxɪᴛ ᴄᴏᴅᴇ <code>42</code> ɪꜱ ᴜꜱᴇᴅ ꜱᴏ "
                            "ꜱʏꜱᴛᴇᴍᴅ/ᴅᴏᴄᴋᴇʀ ᴡɪʟʟ ʙʀɪɴɢ ɪᴛ ꜱᴛʀᴀɪɢʜᴛ ʙᴀᴄᴋ."
                        )
                    ],
                ),
                reply_markup=keys.confirm("own:restart:yes", yes_label="Restart now", no_label="Not now", yes_icon="refresh"),
            )
            return
        if extra == "yes":
            await callback.answer("restarting…")
            await _restart(bot, db, settings, log_channel)
            return

    if page == "stop":
        if extra == "ask":
            await callback.answer()
            await rich_send(
                bot,
                chat_id,
                ui.screen(
                    "shut down",
                    icon="stop2",
                    blocks_=[
                        ui.panel(
                            "ᴛʜᴇ ʙᴏᴛ ᴡɪʟʟ ꜱᴛᴏᴘ ᴄᴏᴍᴘʟᴇᴛᴇʟʏ. ᴏɴ ᴀ ʜᴏꜱᴛ ᴡɪᴛʜ ᴀ "
                            "ʀᴇꜱᴛᴀʀᴛ ᴘᴏʟɪᴄʏ ɪᴛ ᴡɪʟʟ ᴄᴏᴍᴇ ʙᴀᴄᴋ ʙʏ ɪᴛꜱᴇʟꜰ."
                        )
                    ],
                ),
                reply_markup=keys.confirm("own:stop:yes", yes_label="Stop the bot", no_label="Keep running", yes_icon="stop2"),
            )
            return
        if extra == "yes":
            await callback.answer("stopping…")
            await _shutdown(bot, db, settings, log_channel, reason="owner shutdown")
            return

    if page == "users" and extra == "":
        await render("users")
        return

    await callback.answer()


# ---------------------------------------------------------------------------
#  commands that are quicker as text
# ---------------------------------------------------------------------------


@router.message(Command(commands=["report", "daily"]))
async def cmd_report(message: Message, bot: Bot, log_channel: Any = None) -> None:
    """Send the daily report right now."""
    if log_channel is None:
        await rich_send(bot, message.chat.id, ui.panel(ui.no("log channel unavailable"), icon="warning"))
        return
    await rich_send(bot, message.chat.id, await log_channel.daily_report())


@router.message(Command("status"))
async def cmd_status(message: Message, bot: Bot, db: Database, settings: Settings, ai: Any = None) -> None:
    """A compact one-screen status (no buttons - ``/owner`` has those)."""
    await rich_send(
        bot,
        message.chat.id,
        await home_body(db, settings),
        reply_markup=home_keyboard(maintenance=bool(await db.get_kv("maintenance", False))),
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    await rich_send(bot, message.chat.id, await _render(db, settings, "stats"), reply_markup=_page_keyboard("stats"))


@router.message(Command("logs"))
async def cmd_logs(message: Message, bot: Bot, db: Database, settings: Settings) -> None:
    await rich_send(bot, message.chat.id, await _render(db, settings, "logs"), reply_markup=_page_keyboard("logs"))


@router.message(Command("watchdog"))
async def cmd_watchdog(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings, log_channel: Any = None) -> None:
    """``/watchdog [run|clean]`` - report or force a sweep."""
    watchdog = SERVICES.get("watchdog")
    arg = (command.args or "").strip().lower()
    if watchdog is None:
        await rich_send(bot, message.chat.id, ui.panel(ui.no("watchdog is disabled (WATCHDOG=false)"), icon="broom"))
        return
    if arg in ("run", "now", "sweep", "clean"):
        report = await watchdog.sweep(aggressive=arg == "clean")
        if log_channel is not None:
            await log_channel.event(
                "WatchdogRun",
                ui.panel(
                    ui.kv("🗑 ꜰɪʟᴇꜱ", str(report["files"])),
                    ui.kv("💽 ꜰʀᴇᴇ", f"{report['free_disk_gb']} GB"),
                    icon="broom",
                ),
                dm=False,
                dedupe=True,
            )
    await rich_send(bot, message.chat.id, await _render(db, settings, "watchdog"), reply_markup=_page_keyboard("watchdog"))


@router.message(Command("groups"))
async def cmd_groups(message: Message, bot: Bot, db: Database, settings: Settings, log_channel: Any = None) -> None:
    """``/groups`` - list every chat the bot is in, with quick actions."""
    chats = await db.recent_chats(limit=12, active_only=False)
    if not chats:
        await rich_send(bot, message.chat.id, ui.panel(ui.italic("not in any group yet"), icon="users"))
        return
    await rich_send(
        bot,
        message.chat.id,
        await _render(db, settings, "groups"),
        reply_markup=_page_keyboard("groups"),
    )
    if log_channel is not None:
        await log_channel.event("GroupList", ui.panel(ui.kv("💬 ᴄʜᴀᴛꜱ", str(len(chats))), icon="users"), dm=False)


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, command: CommandObject, bot: Bot, db: Database, log_channel: Any = None) -> None:
    """``/broadcast <text>`` - send to every known user."""
    text = (command.args or "").strip()
    if not text:
        await rich_send(bot, message.chat.id, ui.panel("ᴜꜱᴀɢᴇ: <code>/broadcast hello everyone</code>", icon="megaphone"))
        return
    sent = await _broadcast(bot, db, text)
    await rich_send(bot, message.chat.id, ui.panel(ui.ok(f"delivered to <b>{sent}</b> users"), icon="check"))
    if log_channel is not None:
        await log_channel.event("Broadcast", ui.panel(ui.esc(text[:400]), icon="megaphone"), dm=False)


@router.message(Command("maintenance"))
async def cmd_maintenance(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """``/maintenance on|off``."""
    arg = (command.args or "").strip().lower()
    current = bool(await db.get_kv("maintenance", False))
    if arg not in ("on", "off"):
        await rich_send(bot, message.chat.id, ui.panel(ui.kv("🛠 ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ", ui.badge(current)), icon="tools"))
        return
    await db.set_kv("maintenance", arg == "on")
    await rich_send(
        bot,
        message.chat.id,
        ui.panel(
            ui.ok("maintenance <b>on</b>") if arg == "on" else ui.no("maintenance <b>off</b>"),
            icon="tools",
        ),
    )


@router.message(Command("restart"))
async def cmd_restart(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings, log_channel: Any = None) -> None:
    """``/restart [confirm]`` - restart the process."""
    if (command.args or "").strip().lower() not in ("confirm", "yes", "now"):
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(
                ui.kv("♻️ ʀᴇꜱᴛᴀʀᴛ", "ᴛʜɪꜱ ᴡɪʟʟ ʀᴇꜱᴛᴀʀᴛ ᴛʜᴇ ʙᴏᴛ ᴘʀᴏᴄᴇꜱꜱ"),
                icon="refresh",
            ),
            reply_markup=keys.confirm("own:restart:yes", yes_label="Restart now", no_label="Cancel", yes_icon="refresh"),
        )
        return
    await _restart(bot, db, settings, log_channel)


@router.message(Command("shutdown"))
async def cmd_shutdown(message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings, log_channel: Any = None) -> None:
    """``/shutdown confirm`` - stop the bot."""
    if (command.args or "").strip().lower() not in ("confirm", "yes", "now"):
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(ui.kv("🛑 ꜱʜᴜᴛ ᴅᴏᴡɴ", "ᴛʜᴇ ʙᴏᴛ ᴡɪʟʟ ꜱᴛᴏᴘ"), icon="stop2"),
            reply_markup=keys.confirm("own:stop:yes", yes_label="Stop", no_label="Cancel", yes_icon="stop2"),
        )
        return
    await _shutdown(bot, db, settings, log_channel, reason="owner shutdown")


@router.message(Command("backup"))
async def cmd_backup(message: Message, bot: Bot, settings: Settings) -> None:
    """Send the SQLite file as a document."""
    path = await _backup_file(settings)
    if path is None:
        await rich_send(bot, message.chat.id, ui.panel(ui.no("no database to back up"), icon="cross"))
        return
    try:
        await bot.send_document(
            message.chat.id,
            FSInputFile(path, filename=path.name),
            caption=ui.caps(f"{settings.bot_name} backup · {path.stat().st_size // 1024} kb"),
        )
    finally:
        path.unlink(missing_ok=True)


@router.message(Command("emojis"))
async def cmd_emojis(message: Message, command: CommandObject, bot: Bot) -> None:
    """``/emojis [query]`` - browse the 600-id custom emoji registry."""
    from ..rich import emoji as registry

    query = (command.args or "").strip()
    hits = registry.search(query, limit=30)
    if not hits:
        await rich_send(bot, message.chat.id, ui.panel(ui.no("nothing found"), icon="question"))
        return
    rows = [[entry.char, entry.name, entry.pack or "—"] for entry in hits[:24]]
    await rich_send(
        bot,
        message.chat.id,
        ui.screen(
            "emoji registry",
            icon="sparkles",
            subtitle=f"{registry.total_custom_ids()} custom ids · {len(registry.names())} names",
            blocks_=[ui.table(["ᴇᴍᴏᴊɪ", "ɴᴀᴍᴇ", "ᴘᴀᴄᴋ"], rows)],
        ),
    )


# ---------------------------------------------------------------------------
#  actions
# ---------------------------------------------------------------------------


async def _broadcast(bot: Bot, db: Database, text: str) -> int:
    """Send ``text`` to every known user, politely rate-limited."""
    if not text.strip():
        return 0
    rows = await db.fetch_all("SELECT user_id FROM users WHERE is_banned = 0 AND is_bot = 0")
    body = ui.panel(ui.esc(text), icon="megaphone") + "\n\n" + ui.footer_text("broadcast", caps_text=False)
    sent = 0
    for index, row in enumerate(rows, start=1):
        try:
            await rich_send(bot, row["user_id"], body, fallback=True)
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(min(exc.retry_after, 60) + 1)
        except TelegramAPIError:
            continue
        except Exception:
            continue
        if index % 25 == 0:
            await asyncio.sleep(1.5)
    await db.log_event("Broadcast", data={"sent": sent, "total": len(rows)})
    return sent


async def _backup_file(settings: Settings) -> Path | None:
    source = Path(settings.database_path)
    if not source.exists():
        return None
    target = source.with_suffix(f".{datetime.now():%Y%m%d-%H%M}.sqlite3")
    await asyncio.to_thread(shutil.copy2, source, target)
    return target


async def _restart(bot: Bot, db: Database, settings: Settings, log_channel: Any = None) -> None:
    """Log, tell the owner, then re-exec the process (exit code 42 is 'restart me')."""
    log.info("owner requested a restart")
    if log_channel is not None:
        log_channel.set_restart_reason("owner restart")
        await log_channel.event("OwnerRestart", "♻️ <b>restart requested from the panel</b>", dm=True)
    try:
        await db.log_event("OwnerRestart")
    except Exception:
        pass
    if log_channel is not None:
        await log_channel.flush(timeout=6)
    await asyncio.sleep(0.6)

    watchdog = SERVICES.get("watchdog")
    on_restart = getattr(watchdog, "on_restart", None)
    if callable(on_restart):
        try:
            result = on_restart()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            log.debug("restart hook failed: %s", exc)

    # Re-exec: identical to the watchdog's heal path, and works on every host.
    try:
        os.execl(sys.executable, sys.executable, "-m", "gohan")
    except Exception as exc:  # pragma: no cover - platform dependent
        log.error("exec failed (%s); exiting with code 42 instead", exc)
        os._exit(42)


async def _shutdown(bot: Bot, db: Database, settings: Settings, log_channel: Any = None, *, reason: str = "") -> None:
    """Say goodbye, then stop the process."""
    log.info("owner requested a shutdown (%s)", reason)
    _write_restart_reason(settings, f"stopped by owner ({reason})")
    if log_channel is not None:
        await log_channel.event(
            "BotStopped",
            ui.screen("bot stopping", icon="stop2", blocks_=[ui.panel(ui.esc(reason or "owner shutdown"))]),
            dm=True,
        )
        await log_channel.flush(timeout=6)
    else:
        for owner_id in settings.owner_ids:
            try:
                await rich_send(
                    bot,
                    owner_id,
                    ui.panel(ui.no("bot is stopping") + f"\n\n{ui.esc(reason)}", icon="stop2"),
                    fallback=True,
                )
            except Exception:
                pass
    await asyncio.sleep(0.6)
    os._exit(0)


def _write_restart_reason(settings: Settings, text: str) -> None:
    try:
        path = settings.restart_reason_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text[:300], encoding="utf-8")
    except OSError:
        pass
