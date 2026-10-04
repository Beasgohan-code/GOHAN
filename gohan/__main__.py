"""GOHAN entry point.

::

    python -m gohan              # run the bot
    python -m gohan --check      # validate the configuration and exit
    python -m gohan --login      # create the MTProto user session (SMS code)
    python -m gohan --login --bot  # create an MTProto *bot* session
    python -m gohan --version
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from . import __version__


def _say(text: str = "") -> None:
    print(text, flush=True)


def _banner(settings: Any) -> None:
    _say()
    _say(f"  ██████╗  ██████╗ ██╗  ██╗ █████╗ ███╗   ██╗")
    _say(f"  ██╔══██╗██╔═══██╗██║  ██║██╔══██╗████╗  ██║")
    _say(f"  ██║  ██║██║   ██║███████║███████║██╔██╗ ██║")
    _say(f"  ██║  ██║██║   ██║██╔══██║██╔══██║██║╚██╗██║")
    _say(f"  ██████╔╝╚██████╔╝██║  ██║██║  ██║██║ ╚████║")
    _say(f"  ╚═════╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝   v{__version__}")
    _say()
    _say(f"  {settings.bot_name} · guardian, group tools, games and an assistant")
    _say()


async def _run(settings: Any) -> int:
    from .logging_setup import get_logger, setup_logging

    setup_logging(settings)
    log = get_logger("main")

    from .dispatcher import build_runtime

    runtime = await build_runtime(settings)
    _banner(settings)
    _say(settings.describe())
    _say()

    problems = settings.problems()
    for issue in problems:
        _say(f"  ! {issue}")
    if problems:
        _say()

    me = None
    try:
        me = await runtime.bot.get_me()
    except Exception as exc:
        _say(f"  ✗ could not reach Telegram: {exc}")
        _say("    check BOT_TOKEN and the network, then try again")
        await runtime.stop_services()
        return 2

    _say(f"  ✓ connected as @{me.username} ({me.id})")
    _say(f"  ✓ {len(runtime.routers)} routers: {', '.join(runtime.routers)}")
    _say(f"  ✓ rich messages: {'on' if settings.rich_default else 'fallback'}")
    _say(f"  ✓ web: http://0.0.0.0:{settings.port}/health" if settings.keep_alive else "  · web disabled")
    _say()

    await runtime.start_services()
    await runtime.announce_boot()
    await _greet_owner(runtime)

    exit_code = 0
    log.info("polling starts (drop_pending_updates=%s)", settings.drop_pending_updates)
    try:
        await runtime.dispatcher.start_polling(
            runtime.bot,
            drop_pending_updates=settings.drop_pending_updates,
            allowed_updates=runtime.dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        )
    except (KeyboardInterrupt, SystemExit) as exc:
        # exit code 42 means "restart me" (same contract the watchdog uses)
        code = getattr(exc, "code", None)
        exit_code = code if isinstance(code, int) else 0
        log.info("stopping (exit code %s)", exit_code)
    except asyncio.CancelledError:
        pass

    log.info("shutting down")
    if runtime.log_channel is not None and exit_code != 42:
        await runtime.log_channel.event(
            "BotStopped", "🛑 <b>bot stopped</b> - graceful shutdown", dm=True, dedupe=False
        )
    await runtime.stop_services()
    try:
        await runtime.bot.session.close()
    except Exception:
        pass

    if exit_code == 42:
        _say("  ♻️ restarting…")
        os.execv(sys.executable, [sys.executable, "-m", "gohan"])
    return exit_code


async def _greet_owner(runtime: Any) -> None:
    """Tell the owner the bot is up - in a DM, every single start.

    Runs whether or not an LLM key is configured: the bot is designed to start
    in a degraded "demo" mode instead of refusing to run.
    """
    settings = runtime.settings
    if not settings.owner_ids:
        return

    from . import __version__
    from .rich import ui
    from .rich.sender import rich_send

    ai = getattr(runtime.ai, "llm", None)
    llm_ready = bool(getattr(ai, "api_key", None)) or getattr(ai, "name", "") == "demo"
    thinking = "demo mode (no api key)" if getattr(ai, "name", "") == "demo" else getattr(ai, "model", "off")
    mtproto = runtime.mtproto.status.describe() if runtime.mtproto else "off"

    body = ui.screen(
        f"{settings.bot_name} is awake",
        icon="rocket",
        subtitle="startup report",
        blocks_=[
            ui.panel(
                ui.kv("💚 ꜱᴛᴀᴛᴜꜱ", "<b>online</b>"),
                ui.kv("🤖 ᴠᴇʀꜱɪᴏɴ", f"<code>{__version__}</code>"),
                ui.kv("🧠 ᴀꜱꜱɪꜱᴛᴀɴᴛ", ui.esc(thinking)),
                ui.kv("📡 ᴍᴛᴘʀᴏᴛᴏ", ui.esc(mtproto)),
                ui.kv("🌐 ᴡᴇʙ", f"port <code>{settings.port}</code>" if settings.keep_alive else "off"),
                ui.kv("📊 ʀᴏᴜᴛᴇʀꜱ", f"<code>{len(runtime.routers)}</code>"),
                ui.kv("👑 ᴏᴡɴᴇʀꜱ", f"<code>{len(settings.owner_ids)}</code>"),
                icon="chart",
            ),
            ui.panel(
                ui.italic(
                    "ᴇᴠᴇʀʏᴛʜɪɴɢ ɪꜱ ʀᴇᴀᴅʏ. ᴛᴀᴘ ᴀ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ʀᴜɴ ᴛʜᴇ ꜱʜᴏᴡ."
                )
            ),
            ui.actions(
                [
                    ui.button("Owner panel", callback="own:home", style="primary", icon="crown"),
                    ui.button("Stats", callback="own:stats", style="primary", icon="chart"),
                ],
                [
                    ui.button("Watchdog", callback="own:watchdog", style="primary", icon="broom"),
                    ui.button("Groups", callback="own:groups", style="primary", icon="users"),
                ],
            ),
        ],
    )
    for owner_id in settings.owner_ids:
        try:
            await rich_send(runtime.bot, owner_id, body, fallback=True)
        except Exception as exc:
            log.debug("boot DM to %s failed: %s", owner_id, exc)

    if not llm_ready and getattr(ai, "name", "") != "demo":
        log.warning("no LLM api key - /ask will use the offline demo generator")
    else:
        log.info("assistant ready: %s", thinking)


async def _login(settings: Any, *, as_bot: bool = False, phone: str | None = None) -> int:
    """Create an MTProto session. For a user session this asks for the SMS code."""
    from .mtproto.bridge import LoginMode, MTProtoBridge

    if not settings.has_api_credentials:
        _say("  ✗ API_ID / API_HASH are missing - get them at https://my.telegram.org")
        return 2
    if phone:
        settings.phone = phone
    settings.ensure_dirs()

    mode = LoginMode.BOT if as_bot else LoginMode.USER
    bridge = MTProtoBridge(settings)
    if as_bot and settings.bot_token is None:
        _say("  ✗ BOT_TOKEN is required for a bot session")
        return 2
    if not as_bot and not settings.phone:
        _say("  ✗ PHONE is required for a user session (international format, e.g. +9198…)")
        return 2

    _say(f"  · creating a {mode.value} session in {settings.session_dir} …")
    if not as_bot:
        _say("  · Telegram will send a login code to your app - keep it handy")

    client = bridge.build_client(mode)
    try:
        # interactive: Kurigram prompts for the code (and 2FA password) itself
        await client.start()
        me = await client.get_me()
    except Exception as exc:
        _say(f"  ✗ login failed: {exc}")
        return 1
    finally:
        try:
            await client.stop()
        except Exception:
            pass

    session_file = settings.bot_session_file if as_bot else settings.user_session_file
    _say(f"  ✓ logged in as {getattr(me, 'first_name', '?')} (@{getattr(me, 'username', None)})")
    _say(f"  ✓ session saved to {session_file}")
    if not as_bot:
        _say("  · history, cross-chat reputation and 2 GB downloads are now available")
    return 0


def _check(settings: Any) -> int:
    """Validate everything that can be validated without the network."""
    _banner(settings)
    _say(settings.describe())
    _say()

    ok = True

    problems = settings.problems()
    if problems:
        for issue in problems:
            _say(f"  ! {issue}")
        # only the token is fatal for a normal run
        ok = not any("BOT_TOKEN" in issue for issue in problems)

    # dependencies
    try:
        import aiogram

        _say(f"  ✓ aiogram {aiogram.__version__}")
    except Exception as exc:
        _say(f"  ✗ aiogram missing: {exc}")
        ok = False

    try:
        import pyrogram

        _say(f"  ✓ kurigram {getattr(pyrogram, '__version__', '?')}")
    except Exception as exc:
        _say(f"  ! kurigram missing (MTProto features off): {exc}")

    try:
        import richgram

        _say(f"  ✓ richgram {getattr(richgram, '__version__', 'loaded')}")
    except Exception as exc:
        _say(f"  ! richgram missing (rich messages fall back to HTML): {exc}")

    # voice / music
    from .voice import capabilities as voice_capabilities

    caps = voice_capabilities(settings)
    _say(
        f"  {'✓' if caps.ready else '!'} voice: yt-dlp {'yes' if caps.yt_dlp else 'no'}"
        f" · py-tgcalls {'yes' if caps.pytgcalls else 'no'}"
        f" · ffmpeg {'yes' if caps.ffmpeg else 'no'}"
        f" · {caps.note}"
    )

    # rich layer
    from .rich import RICH_AVAILABLE
    from .rich import emoji as registry

    _say(f"  ✓ rich messages: {'richgram' if RICH_AVAILABLE else 'fallback'}")
    _say(f"  ✓ emoji registry: {registry.total_custom_ids()} custom ids, {len(registry.names())} names")

    # database
    import asyncio as _asyncio

    async def _db_check() -> tuple[bool, str]:
        from .storage import Database

        try:
            settings.ensure_dirs()
            db = Database(settings.database_path)
            await db.connect()
            sizes = await db.table_sizes()
            await db.close()
            return True, f"{len(sizes)} tables at {settings.database_path}"
        except Exception as exc:
            return False, str(exc)

    db_ok, db_msg = _asyncio.run(_db_check())
    _say(f"  {'✓' if db_ok else '✗'} database: {db_msg}")
    ok = ok and db_ok

    # routers import and count
    try:
        from .dispatcher import build_dispatcher
        from .storage import Database as _DB

        class _FakeBot:  # only used to satisfy type hints during import
            pass

        services = {
            "db": _DB(settings.database_path),
            "settings": settings,
            "bot": _FakeBot(),
            "log_channel": None,
            "ai": None,
            "games": None,
            "captcha": None,
            "raid": None,
            "animations": None,
            "store": None,
            "mtproto": None,
            "watchdog": None,
            "player": None,
            "afk": None,
        }
        _dp, order = build_dispatcher(settings, services)
        _say(f"  ✓ routers ({len(order)}): {', '.join(order)}")
    except Exception as exc:
        _say(f"  ✗ router wiring failed: {exc}")
        ok = False

    _say()
    _say("  ready to run:  python -m gohan" if ok else "  fix the ✗ / ! items above, then re-run --check")
    _say()
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gohan",
        description="GOHAN - a guardian, group-tools, games and AI Telegram bot",
    )
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    parser.add_argument("--login", action="store_true", help="create the MTProto user session")
    parser.add_argument("--bot", action="store_true", help="with --login: create a bot session instead")
    parser.add_argument("--phone", metavar="+9198…", help="with --login: phone number to log in with")
    parser.add_argument(
        "--version", action="version", version=f"GOHAN {__version__}", default=argparse.SUPPRESS
    )
    parser.add_argument("--env", metavar="FILE", help="read configuration from FILE instead of .env")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.env:
        import os

        os.environ["GOHAN_ENV_FILE"] = args.env

    from .config import get_settings

    settings = get_settings()

    if args.check:
        return _check(settings)
    if args.login:
        return asyncio.run(_login(settings, as_bot=args.bot, phone=args.phone))
    return asyncio.run(_run(settings))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
