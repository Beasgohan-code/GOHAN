"""Owner log channel.

Everything the owner wants to know about their bot lands in one place, tagged
``#Event`` so a Telegram search inside the channel works like a filter:

* ``#BotStarted`` / ``#BotStopped`` - with host, version, uptime and totals
* ``#NewUser`` / ``#Start``     - who started the bot (rate-limited per user)
* ``#Ban`` / ``#Warn`` / ``#Raid`` - moderation actions
* ``#LowDisk`` / ``#AutoRestart`` / ``#Watchdog`` - infrastructure
* ``#DailyReport``              - last 24 h + totals, once a day

Destination rules (same as Videl1):

* with ``LOG_CHANNEL_ID`` set → the channel gets everything;
* events listed in ``OWNER_DM_EVENTS`` additionally go to every owner's DM;
* with no channel configured, *everything* falls back to the owners' DMs.

Messages are sent through the rich-message sender when possible so the log looks
like the rest of the bot (small-caps headings, blockquote panels, tables).
"""

from __future__ import annotations

import asyncio
import html
import platform
import time
from datetime import datetime, timedelta
from typing import Any, Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup

from .. import __version__
from ..config import Settings, detect_host
from ..rich import ui
from ..rich.sender import rich_send
from ..storage import Database

__all__ = ["LogChannel", "esc"]

_REASON_FILE_MAX = 300


def esc(value: Any) -> str:
    """HTML-escape untrusted values before they go into a log message."""
    return html.escape(str(value if value is not None else ""), quote=False)


def stamp(when: datetime | None = None, tz: Any = None) -> str:
    moment = when or datetime.now(tz)
    return moment.strftime("%d %b %Y · %I:%M:%S %p").lstrip("0")


class LogChannel:
    """Fan-out for bot events."""

    def __init__(self, settings: Settings, db: Database, bot: Bot | None = None) -> None:
        self.settings = settings
        self.db = db
        self.bot = bot
        self._tasks: set[asyncio.Task[Any]] = set()
        self._start_seen: dict[int, float] = {}
        self._last_sent: dict[str, str] = {}  # de-dupe identical consecutive events
        self.blocking = False  # tests set True so sends complete before asserting

    def bind(self, bot: Bot) -> None:
        self.bot = bot

    # -- destinations --------------------------------------------------------

    def targets(self, tag: str) -> list[int]:
        channel = self.settings.log_channel_id
        out: list[int] = []
        if channel:
            out.append(channel)
        # Owner DMs: for "important" tags, or always when there is no channel.
        if tag in self.settings.owner_dm_event_tags or not channel:
            out.extend(o for o in sorted(self.settings.owner_ids) if o not in out)
        return out

    # -- sending -------------------------------------------------------------

    async def _deliver(self, chat_id: int, html_text: str, reply_markup: InlineKeyboardMarkup | None) -> Any:
        if self.bot is None:
            return None
        for attempt in range(3):
            try:
                message = await rich_send(
                    self.bot, chat_id, html_text, reply_markup=reply_markup, fallback=True
                )
                return message
            except TelegramRetryAfter as exc:
                if exc.retry_after > 120:
                    break
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramAPIError as exc:
                if reply_markup is not None and attempt == 0:
                    reply_markup = None  # markup can be rejected; retry plain once
                    continue
                log_line = f"log to {chat_id} failed: {exc}"
                if self.bot is not None:
                    pass
                break
        return None

    async def event(
        self,
        tag: str,
        body: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        dm: bool | None = None,
        dedupe: bool = False,
        silent_duplicate: bool = True,
    ) -> None:
        """Publish ``#tag`` + ``body``. Never raises."""
        tag = tag.lstrip("#")
        text = f"<b>#{esc(tag)}</b>\n\n{body}\n\n<i>🕒 {stamp()}</i>"

        if dedupe and self._last_sent.get(tag) == text:
            if silent_duplicate:
                return
        self._last_sent[tag] = text

        try:
            await self.db.log_event(tag)  # the channel is a window; the db is history
        except Exception:
            pass

        if self.bot is None:
            return

        targets = self.targets(tag)
        if dm is True:
            targets = list({*targets, *sorted(self.settings.owner_ids)})
        elif dm is False:
            targets = [t for t in targets if t not in self.settings.owner_ids] or targets[:1]

        async def send_all() -> None:
            for chat_id in targets:
                await self._deliver(chat_id, text, reply_markup)

        if self.blocking:
            await send_all()
            return
        task = asyncio.get_running_loop().create_task(send_all())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def raw(self, text: str, *, tag: str = "Log") -> None:
        """Send an already-formatted message (adds the tag/timestamp if missing)."""
        body = text if text.lstrip().startswith("<b>#") else f"<b>#{esc(tag)}</b>\n\n{text}"
        payload = f"{body}\n\n<i>🕒 {stamp()}</i>"
        if self.bot is None:
            return
        for chat_id in self.targets(tag):
            await self._deliver(chat_id, payload, None)

    async def flush(self, timeout: float = 10.0) -> None:
        """Wait for queued sends (used on shutdown)."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    # -- helpers -------------------------------------------------------------

    def should_log_start(self, user_id: int) -> bool:
        """Rate-limit ``#Start`` per user (``START_LOG_COOLDOWN_MIN``)."""
        if not self.settings.log_start_events:
            return False
        cooldown = self.settings.start_log_cooldown_min * 60
        last = self._start_seen.get(user_id, 0.0)
        if cooldown and time.time() - last < cooldown:
            return False
        self._start_seen[user_id] = time.time()
        if len(self._start_seen) > 50_000:
            self._start_seen.clear()
        return True

    def user_block(self, user: Any, extra: str = "") -> str:
        """Standard 'who did it' panel for a Telegram user."""
        if user is None:
            return ui.kv_panel([("👤 ᴜꜱᴇʀ", "unknown")])
        name = esc(" ".join(x for x in (user.first_name, user.last_name) if x))
        rows: list[tuple[str, str]] = [
            ("👤 ᴜꜱᴇʀ", ui.mention(f"<b>{name or user.id}</b>", user.id)),
            ("🆔 ɪᴅ", f"<code>{user.id}</code>"),
            ("🔗 ᴜꜱᴇʀ", f"@{esc(user.username)}" if getattr(user, "username", None) else "—"),
        ]
        if getattr(user, "language_code", None):
            rows.append(("🌐 ʟᴀɴɢ", esc(user.language_code)))
        if getattr(user, "is_premium", False):
            rows.append(("⭐ ᴘʀᴇᴍɪᴜᴍ", "yes"))
        if extra:
            rows.append(("•", extra))
        return ui.kv_panel(rows)

    # -- restart reason ------------------------------------------------------

    def set_restart_reason(self, text: str) -> None:
        try:
            path = self.settings.restart_reason_file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(text)[:_REASON_FILE_MAX], encoding="utf-8")
        except OSError:
            pass

    def pop_restart_reason(self) -> str:
        path = self.settings.restart_reason_file
        try:
            text = path.read_text(encoding="utf-8").strip()
            path.unlink(missing_ok=True)
            return text
        except OSError:
            return ""

    # -- reports -------------------------------------------------------------

    async def collect_totals(self) -> dict[str, Any]:
        stats = await self.db.stats(since=time.time() - 86400)
        return {
            "users": stats["users"],
            "new": stats["users_new"],
            "active": stats["users_active"],
            "banned": stats["banned"],
            "chats": stats["chats"],
            "games": stats["games"],
        }

    async def boot_report(
        self,
        me: Any,
        *,
        handlers: int,
        boot_seconds: float,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """The "I'm online" message, including why the last run ended."""
        totals = await self.collect_totals()
        reason = self.pop_restart_reason()
        rows: list[tuple[str, str]] = [
            ("🤖 ʙᴏᴛ", f"@{esc(getattr(me, 'username', '?'))} (<code>{getattr(me, 'id', '?')}</code>)"),
            ("🖥 ʜᴏꜱᴛ", esc(detect_host())),
            ("🐍 ᴘʏᴛʜᴏɴ", platform.python_version()),
            ("🧩 ʜᴀɴᴅʟᴇʀꜱ", str(handlers)),
            ("⏱ ʙᴏᴏᴛ", f"{boot_seconds:.1f}s"),
        ]
        if extra:
            rows.extend((k, str(v)) for k, v in extra.items())
        body = ui.screen(
            "online",
            icon="rocket",
            subtitle=f"{self.settings.bot_name} v{__version__}",
            blocks_=[
                ui.kv_panel(rows, icon="robot"),
                ui.kv_panel(
                    [
                        ("👥 ᴜꜱᴇʀꜱ", f"<code>{totals['users']}</code> (🚫 {totals['banned']} banned)"),
                        ("✨ ɴᴇᴡ (24ʜ)", f"<code>{totals['new']}</code>"),
                        ("💬 ᴄʜᴀᴛꜱ", f"<code>{totals['chats']}</code>"),
                        ("🎮 ɢᴀᴍᴇꜱ (24ʜ)", f"<code>{totals['games']}</code>"),
                    ],
                    icon="chart",
                ),
                ui.details("♻️ restart reason", esc(reason) or "—", icon="refresh")
                if reason
                else "",
            ],
        )
        await self.event("BotStarted", body, dm=False)

    async def daily_report(self) -> str:
        """Last 24 h + totals; returns the rendered body for reuse by ``/report``."""
        since = time.time() - 86400
        counts = await self.db.event_counts(since)
        totals = await self.collect_totals()
        top = counts.most_common(8) if hasattr(counts, "most_common") else sorted(
            counts.items(), key=lambda kv: kv[1], reverse=True
        )[:8]

        body = ui.screen(
            "daily report",
            icon="chart",
            subtitle=datetime.now(self.settings.tzinfo).strftime("%d %b %Y"),
            blocks_=[
                ui.h("last 24h", 2, icon="clock"),
                ui.kv_panel(
                    [
                        ("✨ ɴᴇᴡ ᴜꜱᴇʀꜱ", f"<code>{totals['new']}</code>"),
                        ("💬 ᴀᴄᴛɪᴠᴇ", f"<code>{totals['active']}</code>"),
                        ("🎮 ɢᴀᴍᴇꜱ", f"<code>{totals['games']}</code>"),
                    ],
                    icon="sparkles",
                ),
                ui.h("totals", 2, icon="chart"),
                ui.kv_panel(
                    [
                        ("👥 ᴜꜱᴇʀꜱ", f"<code>{totals['users']}</code>"),
                        ("🚫 ʙᴀɴɴᴇᴅ", f"<code>{totals['banned']}</code>"),
                        ("💬 ᴄʜᴀᴛꜱ", f"<code>{totals['chats']}</code>"),
                    ],
                    icon="inbox",
                ),
                ui.h("events", 2, icon="memo"),
                ui.table(["ᴇᴠᴇɴᴛ", "ᴄᴏᴜɴᴛ"], [[f"#{esc(tag)}", str(n)] for tag, n in top])
                if top
                else ui.italic("no events recorded"),
            ],
        )
        await self.event("DailyReport", body, dm=False)
        return body

    async def daily_report_loop(self) -> None:
        """Publish :meth:`daily_report` every day at ``DAILY_REPORT_HOUR`` local time."""
        if not self.settings.daily_report:
            return
        tz = self.settings.tzinfo
        while True:
            now = datetime.now(tz)
            target = now.replace(
                hour=self.settings.daily_report_hour % 24, minute=0, second=0, microsecond=0
            )
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                await self.daily_report()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.event("Error", f"daily report failed: <code>{esc(exc)[:200]}</code>")
            await asyncio.sleep(60)

    # -- convenience event helpers ------------------------------------------

    async def moderation(self, tag: str, *, chat: Any, actor: Any, target: Any, extra: str = "") -> None:
        """A moderation action: who did what, to whom, where."""
        body = ui.screen(
            tag,
            icon="shield",
            subtitle=getattr(chat, "title", None) or str(getattr(chat, "id", "?")),
            blocks_=[
                ui.kv_panel(
                    [("🛡 ᴀᴄᴛᴏʀ", f"<code>{getattr(actor, 'id', actor)}</code>")],
                    icon="crown",
                ),
                self.user_block(target, extra),
            ],
        )
        await self.event(tag, body)

    async def error(self, where: str, exc: BaseException, extra: str = "") -> None:
        body = ui.screen(
            "error",
            icon="warning",
            subtitle=where,
            blocks_=[
                ui.panel(f"<code>{esc(type(exc).__name__)}: {esc(str(exc))[:300]}</code>"),
                ui.italic(esc(extra)) if extra else "",
            ],
        )
        await self.event("Error", body, dedupe=True)


__all__ += ["Iterable"]
