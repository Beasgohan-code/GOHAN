"""Assemble the bot: services, middlewares, routers, dispatcher.

Router order matters - it *is* the priority list:

1. ``owner``            - owner commands are never shadowed
2. ``general``          - ``/start``, ``/help``, ``/id``, notes
3. ``guardian``         - moderation commands, the settings panel, captcha
4. ``filters``          - trigger -> reply, before the guard
5. ``fun``              - the 19 action commands and ``/actions``
6. ``games``            - quiz, word chain, dice, slots
7. ``anime``/``music``  - lookups and downloads
8. ``ai``               - ``/ask`` and friends
9. ``guardian.events``  - the live join/message guard, **last**

aiogram stops at the **first matching handler**, and a handler that returns
nothing still counts as handled. Anything with a catch-all filter therefore has
to sit behind a strict filter (or after everything else), otherwise it starves
every router behind it - see ``WantsAnswer`` in :mod:`gohan.handlers.ai` and
``GameRunning`` in :mod:`gohan.games`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from . import __version__
from .ai.chat import AIConversation, ConversationStore
from .ai.llm import build_llm
from .config import Settings, detect_host
from .filters import is_owner_id
from .fun import AnimationProvider
from .games import GameManager
from .guardian.antiraid import RaidDetector
from .guardian.captcha import CaptchaManager
from .logging_setup import get_logger
from .mtproto.bridge import build_bridge
from .rich import ui
from .rich.sender import rich_send
from .services.keep_alive import KeepAlive
from .services.log_channel import LogChannel
from .services.watchdog import Watchdog
from .storage import Database
from .voice import AfkWatcher, build_player

log = get_logger("dispatcher")

__all__ = ["GohanBot", "build_bot", "build_dispatcher"]


def build_bot(settings: Settings) -> Bot:
    """A Bot with the defaults GOHAN wants everywhere."""
    token = settings.bot_token.get_secret_value() if settings.bot_token else ""
    if not token:
        raise RuntimeError("BOT_TOKEN is not set - copy .env.example to .env and fill it in")
    return Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=False),
    )


@dataclass
class GohanBot:
    """Everything the running bot owns, in one object."""

    settings: Settings
    bot: Bot
    dispatcher: Dispatcher
    db: Database
    animations: AnimationProvider
    captcha: CaptchaManager
    games: GameManager
    conversations: ConversationStore
    ai: AIConversation
    raid: RaidDetector
    watchdog: Watchdog | None = None
    log_channel: LogChannel | None = None
    keep_alive: KeepAlive | None = None
    mtproto: Any = None
    player: Any = None
    started_at: float = field(default_factory=time.time)
    routers: list[str] = field(default_factory=list)
    _voice_task: Any = None

    # -- lifecycle -----------------------------------------------------------

    async def start_services(self) -> None:
        """Bring up the optional subsystems. Each failure is contained."""
        self.settings.ensure_dirs()

        if self.log_channel is not None:
            self.log_channel.bind(self.bot)
            reason = self.log_channel.pop_restart_reason()
            if reason:
                log.info("restart reason from last run: %s", reason)

        if self.mtproto is not None:
            self.mtproto.start_background()

        if self.watchdog is not None:
            self.watchdog.start()

        if self.keep_alive is not None:
            await self.keep_alive.start()

        if self.log_channel is not None and self.settings.daily_report:
            import asyncio

            asyncio.get_running_loop().create_task(self.log_channel.daily_report_loop())

        if self.player is not None:
            import asyncio

            self._voice_task = asyncio.get_running_loop().create_task(self._voice_beat())

    async def _voice_beat(self) -> None:
        """Advance finished tracks and let idle rooms go (see Player.tick)."""
        import asyncio

        interval = max(1.0, float(getattr(self.settings, "voice_tick_sec", 5.0)))
        while True:
            try:
                await self.player.tick()
            except Exception as exc:  # pragma: no cover - the beat must never die
                log.debug("voice tick failed: %s", exc)
            await asyncio.sleep(interval)

    async def stop_services(self) -> None:
        if self._voice_task is not None:
            self._voice_task.cancel()
            self._voice_task = None
        if self.player is not None:
            try:
                await self.player.close()
            except Exception:
                pass
        if self.keep_alive is not None:
            await self.keep_alive.stop()
        if self.watchdog is not None:
            await self.watchdog.stop()
        if self.mtproto is not None:
            await self.mtproto.stop()
        if self.log_channel is not None:
            await self.log_channel.flush()
        try:
            await self.db.close()
        except Exception:
            pass

    # -- messages ------------------------------------------------------------

    async def announce_boot(self) -> None:
        """Tell the log channel (and owners) that a new process is alive."""
        if self.log_channel is None or not self.settings.log_start_events:
            return
        me = await self.bot.get_me()
        stats = await self.db.stats()
        body = ui.screen(
            "bot started",
            icon="rocket",
            subtitle=f"@{me.username}" if me.username else "",
            blocks_=[
                ui.kv_panel(
                    [
                        ("🤖 ᴠᴇʀꜱɪᴏɴ", f"<code>{__version__}</code>"),
                        ("🌍 ʜᴏꜱᴛ", ui.esc(detect_host())),
                        ("🧠 ʟʟᴍ", ui.esc(getattr(self.ai.llm, "model", "off"))),
                        (
                            "📡 ᴍᴛᴘʀᴏᴛᴏ",
                            ui.esc(self.mtproto.status.describe() if self.mtproto else "off"),
                        ),
                        ("🌐 ᴡᴇʙ", f"port <code>{self.settings.port}</code>"),
                        ("📊 ʀᴏᴜᴛᴇʀꜱ", f"<code>{len(self.routers)}</code>"),
                    ],
                    icon="chart",
                ),
                ui.table(
                    ["ᴍᴇᴛʀɪᴄ", "ᴠᴀʟᴜᴇ"],
                    [
                        ("users", str(stats["users"])),
                        ("active (24h)", str(stats["users_active"])),
                        ("chats", str(stats["chats"])),
                        ("games (24h)", str(stats["games"])),
                    ],
                ),
            ],
        )
        await self.log_channel.event("BotStarted", body, dm=False, dedupe=True)

    async def notify(self, tag: str, html: str, *, dm: bool = True) -> None:
        """Rich note to the log channel / owners. Never raises."""
        try:
            if self.log_channel is not None:
                await self.log_channel.event(tag, html, dm=dm)
                return
            for owner_id in self.settings.owner_ids:
                await rich_send(self.bot, owner_id, html, fallback=True)
        except Exception as exc:
            log.debug("notify(%s) failed: %s", tag, exc)


def build_services(settings: Settings, db: Database, bot: Bot) -> dict[str, Any]:
    """Create the subsystems that handlers ask for by name."""
    llm = build_llm(settings)
    conversations = ConversationStore()
    ai = AIConversation(bot=bot, llm=llm, store=conversations, db=db, settings=settings)
    animations = AnimationProvider(db, enabled=settings.rich_default)
    captcha = CaptchaManager()
    games = GameManager()
    raid = RaidDetector()
    log_channel = LogChannel(settings, db, bot)
    mtproto = build_bridge(settings)

    from .voice.handlers import make_panel_hook

    player = build_player(settings, db=db, mtproto=mtproto, hook=make_panel_hook(bot))
    afk = AfkWatcher(db, bot)

    return {
        "ai": ai,
        "player": player,
        "afk": afk,
        "llm": llm,
        "store": conversations,
        "animations": animations,
        "captcha": captcha,
        "games": games,
        "raid": raid,
        "log_channel": log_channel,
        "mtproto": mtproto,
        "gui": None,
    }


def build_watchdog_for(bot: Bot, db: Database, services: dict[str, Any], notify: Any) -> Watchdog:
    """Wire the watchdog to the live services so it can heal them."""
    settings = services["settings"]

    async def _get_me() -> Any:
        return await bot.get_me()

    def _on_restart() -> None:
        log_channel = services.get("log_channel")
        if log_channel is not None:
            log_channel.set_restart_reason("watchdog: Telegram connection lost")

    states = [
        ("captcha", lambda: services["captcha"].pending, services["captcha"].drop),
    ]
    caches = []
    animations = services.get("animations")
    if animations is not None:
        caches.append(lambda: len(getattr(animations, "_cache", {})))

    return Watchdog(
        settings,
        get_me=_get_me,
        states=states,
        notify=notify,
        caches=caches,
        on_restart=_on_restart,
    )


def build_dispatcher(
    settings: Settings, services: dict[str, Any] | None = None, **extra: Any
) -> tuple[Dispatcher, list[str]]:
    """Create the Dispatcher and attach every router.

    ``services`` are injected into handler ``data``, so a handler only has to
    name what it needs (``db: Database, ai: AIConversation, log_channel: …``).
    """
    services = {**(services or {}), **extra}
    from . import anime as anime_module
    from . import fun as fun_module
    from . import games as games_module
    from . import music as music_module
    from .guardian import events as guardian_events
    from .guardian import moderation
    from .guardian import panel as guardian_panel
    from .handlers import ai as ai_handlers
    from .handlers import filters as filter_handlers
    from .handlers import general, owner
    from .voice import handlers as voice_handlers

    # Module-level routers can only be attached to one Dispatcher, ever. Rather
    # than leak that constraint to callers (--check and the test-suite both build
    # a dispatcher), reload the router modules so each build gets fresh objects.
    import importlib

    for module in (
        owner,
        general,
        guardian_panel,
        moderation,
        filter_handlers,
        fun_module,
        games_module,
        anime_module,
        music_module,
        voice_handlers,
        ai_handlers,
        guardian_events,
    ):
        importlib.reload(module)

    dp = Dispatcher(storage=MemoryStorage())

    # services are injected before every update; DatabaseMiddleware adds the db
    # and UsageMiddleware adds chat_settings on top.
    @dp.update.outer_middleware
    async def _inject(handler: Any, event: Any, data: dict[str, Any]) -> Any:  # type: ignore[no-untyped-def]
        for key, value in services.items():
            data.setdefault(key, value)
        data.setdefault("settings", settings)
        return await handler(event, data)

    order: list[str] = []
    for router in (
        owner.router,             # owners first: never shadowed
        general.router,           # /start, /help, /id, /notes, /ping
        guardian_panel.router,    # /guardian, /settings, guard:* buttons
        moderation.router,        # /warn, /mute, /ban, /purge, …
        filter_handlers.router,   # /filter, /stop, /filters
        fun_module.router,        # /hug … /actions, /setgif
        games_module.router,      # /quiz, /guess, /chain, /dice, /slot, /top
        anime_module.router,      # /anime, /trending, /character
        music_module.router,      # /music, /download (files, no voice chat)
        voice_handlers.router,    # /play, /queue, /loop, playlists, v:* buttons
        ai_handlers.router,       # /ask, /ai, /translate, /summary
        guardian_events.trigger_router,  # filters - before the guard, by design
        guardian_events.router,   # live guard - always last
    ):
        dp.include_router(router)
        order.append(getattr(router, "name", router.__class__.__name__))

    from .middleware import (
        DatabaseMiddleware,
        ErrorsMiddleware,
        MaintenanceMiddleware,
        ThrottleMiddleware,
        UsageMiddleware,
    )

    dp.update.outer_middleware(ErrorsMiddleware(services.get("log_channel")))
    dp.update.outer_middleware(MaintenanceMiddleware())
    dp.update.outer_middleware(DatabaseMiddleware(services["db"]))
    dp.update.outer_middleware(UsageMiddleware(services["db"], rich_default=settings.rich_default))
    dp.message.middleware(ThrottleMiddleware(settings.throttle_delay))
    dp.callback_query.middleware(ThrottleMiddleware(0.3))

    # Away mode runs *before* routing but hands the update on, so a member who
    # comes back is greeted and still gets an answer to what they just said.
    afk = services.get("afk")
    if afk is not None:
        dp.message.outer_middleware(afk)

    return dp, order


def _make_lifecycle(bot: Bot, db: Database, settings: Settings, log_channel: Any) -> Any:
    """Let the web panel restart or stop the bot process.

    The web layer must never touch the process itself - it just calls this hook,
    which reuses the exact same code path as the Telegram ``/restart`` command.
    """

    async def lifecycle(action: str) -> None:
        from .handlers.owner import _restart, _shutdown

        log.warning("lifecycle action from the web panel: %s", action)
        if action == "restart":
            await _restart(bot, db, settings, log_channel)
        elif action in ("shutdown", "stop"):
            await _shutdown(bot, db, settings, log_channel, reason="web control panel")

    return lifecycle


async def build_runtime(settings: Settings) -> GohanBot:
    """Everything :mod:`gohan.__main__` needs, in order."""
    runtime_started = time.time()
    settings.ensure_dirs()
    bot = build_bot(settings)
    db = Database(settings.database_path)
    await db.connect()

    services = build_services(settings, db, bot)
    services["settings"] = settings
    services["db"] = db
    services["bot"] = bot

    watchdog = build_watchdog_for(bot, db, services, notify=_make_notifier(bot, db, services, settings))
    services["watchdog"] = watchdog

    dp, order = build_dispatcher(settings, services)

    keep_alive = None
    if settings.keep_alive:
        from .web.auth import Authenticator
        from .web.state import build_state

        services["auth"] = Authenticator(settings)
        services["auth_mode"] = services["auth"].mode
        services["lifecycle"] = _make_lifecycle(bot, db, settings, services.get("log_channel"))
        web_state = build_state(settings, db, services, started_at=runtime_started)
        keep_alive = KeepAlive(
            settings,
            stats=_make_stats(db, services, settings),
            state=web_state,
        )
        keep_alive.auth = services["auth"]

    runtime = GohanBot(
        settings=settings,
        bot=bot,
        player=services["player"],
        dispatcher=dp,
        db=db,
        animations=services["animations"],
        captcha=services["captcha"],
        games=services["games"],
        conversations=services["store"],
        ai=services["ai"],
        raid=services["raid"],
        watchdog=watchdog,
        log_channel=services["log_channel"],
        keep_alive=keep_alive,
        mtproto=services["mtproto"],
        routers=order,
        started_at=runtime_started,
    )
    runtime.routers = order

    # owner commands reach the live services through this shim
    from .handlers import owner as owner_handlers

    owner_handlers.SERVICES.update(
        {
            "watchdog": watchdog,
            "mtproto": services["mtproto"],
            "keep_alive": keep_alive,
            "ai": services["ai"],
            "log_channel": services["log_channel"],
            "started_at": runtime.started_at,
            "settings": settings,
            "db": db,
            "bot": bot,
        }
    )
    from .guardian import moderation as moderation_module

    setattr(moderation_module, "log_channel", services["log_channel"])
    return runtime


def _make_notifier(bot: Bot, db: Database, services: dict[str, Any], settings: Settings) -> Any:
    async def notify(tag: str, html: str) -> None:
        log_channel = services.get("log_channel")
        if log_channel is not None:
            await log_channel.event(tag, html, dm=False, dedupe=True)
            return
        for owner_id in settings.owner_ids:
            await rich_send(bot, owner_id, html, fallback=True)

    return notify


def _make_stats(db: Database, services: dict[str, Any], settings: Settings) -> Any:
    async def stats() -> dict[str, Any]:
        counts = await db.stats()
        payload: dict[str, Any] = {
            "version": __version__,
            "users": counts["users"],
            "chats": counts["chats"],
            "games_24h": counts["games"],
            "rich": True,
            "watchdog": services["watchdog"].stats.sweeps,
        }
        mtproto = services.get("mtproto")
        if mtproto is not None:
            payload["mtproto"] = mtproto.status.mode.value
        return payload

    return stats
