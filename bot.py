#!/usr/bin/env python3
"""GOHAN — rich channel publisher bot.

Usage:
    python bot.py            # long-polling run (needs BOT_TOKEN)
    python bot.py --selftest # offline smoke test, no token required
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("gohan")

UPDATE_TYPES = ["message", "callback_query", "stopped_message_generation"]


# ------------------------------------------------------------------ selftest

def selftest() -> int:
    """Offline smoke test: imports, rich payload builders, storage roundtrip."""
    failures: list[str] = []

    def check(name: str, fn) -> None:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failures.append(name)
            print(f"  FAIL {name}: {exc!r}")

    import aiogram
    import pyrogram
    from aiogram.types import InputRichMessage

    from gohan import __version__
    from gohan.config import cfg
    from gohan.mtproto import MtpEngine
    from gohan.publisher import _flags
    from gohan.rich import (
        build_rich,
        demo_document,
        demo_keyboard,
        draft_keyboard,
        help_keyboard,
        resolve_mode,
        url_keyboard,
        welcome_keyboard,
    )
    from gohan.stream import _paragraphs, stop_stream
    from gohan.storage import Draft, Storage, default_options

    print(f"GOHAN {__version__} · aiogram {aiogram.__version__} · "
          f"Kurigram(pyrogram) {pyrogram.__version__}")

    def t_modes() -> None:
        assert resolve_mode("<b>hi</b>", "auto") == "html"
        assert resolve_mode("**hi**", "auto") == "markdown"
        assert resolve_mode("plain text", "html") == "html"
        assert build_rich("<b>x</b>", "auto").model_dump(exclude_none=True) == {"html": "<b>x</b>"}
        assert build_rich("# md", "markdown").model_dump(exclude_none=True)["markdown"] == "# md"

    def t_demo_doc() -> None:
        doc = demo_document()
        data = doc.model_dump(exclude_none=True)
        assert data.get("blocks"), "demo blocks missing"
        assert len(data["blocks"]) >= 10, f"expected >=10 blocks, got {len(data['blocks'])}"

    def t_cards() -> None:
        from gohan.rich import HELP_HTML, WELCOME_HTML
        InputRichMessage(html=HELP_HTML).model_dump(exclude_none=True)
        InputRichMessage(html=WELCOME_HTML.format(
            name="X", aiogram_ver=aiogram.__version__, kurigram_ver=pyrogram.__version__
        )).model_dump(exclude_none=True)

    def t_keyboards() -> None:
        d = Draft(text="hello **world**", buttons=[{"label": "Docs", "url": "https://x.dev"}])
        kb = draft_keyboard(d)
        demo_keyboard(); help_keyboard(); welcome_keyboard(); url_keyboard(d.buttons)
        for row in kb.inline_keyboard:
            for btn in row:
                if btn.callback_data:
                    assert len(btn.callback_data.encode()) <= 64, btn.callback_data

    def t_stream_bits() -> None:
        parts = _paragraphs("a\n\nb\n\nc")
        assert parts == ["a", "b", "c"], parts
        assert stop_stream(1, 999) is False

    def t_flags() -> None:
        flags = _flags({"silent": True, "protect": True, "paid": False, "effect": False})
        assert flags["disable_notification"] is True
        assert flags["allow_paid_broadcast"] is None
        assert flags["message_effect_id"] is None

    def t_mtp_disabled() -> None:
        engine = MtpEngine(cfg)
        assert not engine.enabled and engine.disabled_reason

    async def t_storage() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st = Storage(str(Path(tmp) / "t.db"))
            await st.connect()
            d = Draft(text="post", mode="html", buttons=[{"label": "a", "url": "https://a.b"}],
                      options=default_options())
            await st.save_draft(7, d)
            got = await st.get_draft(7)
            assert got and got.text == "post" and got.buttons[0]["label"] == "a"
            await st.upsert_channel(-100123, "Chan", "chan", "channel")
            chans = await st.list_channels()
            assert chans and chans[0]["chat_id"] == -100123
            pid = await st.add_post(7, -100123, d, 1.0)
            due = await st.due_posts(2.0)
            assert due and due[0]["id"] == pid
            await st.finish_post(pid, "done", message_id=5)
            assert not await st.due_posts(2.0)
            stats = await st.stats()
            assert stats["published"] == 1
            await st.clear_draft(7)
            await st.close()

    print("running checks…")
    check("modes & rich builder", t_modes)
    check("10.3 showcase document", t_demo_doc)
    check("canned cards render", t_cards)
    check("keyboards / callback limits", t_keyboards)
    check("stream paragraph split", t_stream_bits)
    check("publish flags", t_flags)
    check("mtproto disabled state", t_mtp_disabled)
    check("storage roundtrip", lambda: asyncio.run(t_storage()))

    # publisher/handlers import graph
    def t_handlers() -> None:
        from gohan.handlers import build_router
        build_router()
        from gohan.handlers.actions import on_generation_stopped  # noqa: F401
        from gohan.scheduler import Scheduler  # noqa: F401

    check("handlers & scheduler import", t_handlers)

    if failures:
        print(f"\n❌ {len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("\n✅ all selftests passed")
    return 0


# --------------------------------------------------------------------- main

async def amain() -> None:
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties

    from gohan.config import cfg
    from gohan.handlers import build_router
    from gohan.handlers.actions import on_generation_stopped
    from gohan.mtproto import MtpEngine
    from gohan.scheduler import Scheduler
    from gohan.storage import Storage

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    if not cfg.bot_token:
        print(
            "BOT_TOKEN is missing.\n"
            "  1. talk to @BotFather → /newbot → copy the token\n"
            "  2. cp .env.example .env  and paste it into BOT_TOKEN=…\n"
            "  3. python bot.py",
            file=sys.stderr,
        )
        raise SystemExit(2)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(link_preview_is_disabled=True))
    dp = Dispatcher()
    dp.include_router(build_router())
    # Bot API 10.3 stop button — typed observer, NOT dp.update
    dp.stopped_message_generation.register(on_generation_stopped)

    storage = Storage(cfg.database)
    await storage.connect()
    mtp = MtpEngine(cfg)
    await mtp.start()
    scheduler = Scheduler(bot, storage, mtp)
    scheduler.start()

    me = await bot.get_me()
    log.info("GOHAN online as @%s (id %s)", me.username, me.id)
    log.info("OWNER_IDS: %s", sorted(cfg.owner_ids) if cfg.owner_ids else "OPEN BOT")
    try:
        await dp.start_polling(
            bot, allowed_updates=UPDATE_TYPES, storage=storage, mtp=mtp
        )
    finally:
        await scheduler.stop()
        await mtp.stop()
        await storage.close()
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="GOHAN rich channel publisher bot")
    parser.add_argument("--selftest", action="store_true",
                        help="run offline checks and exit (no token needed)")
    args = parser.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    asyncio.run(amain())


if __name__ == "__main__":
    main()
