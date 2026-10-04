"""End-to-end integration test against a LOCAL mock of the Telegram Bot API.

Runs the real Dispatcher + handlers + publisher + scheduler against
``http://127.0.0.1:<port>/`` and asserts the exact Bot API methods the bot
calls (sendRichMessage, sendRichMessageDraft, editMessageText, …).

    .venv/bin/python tests/integration.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402
from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.aiohttp import AiohttpSession  # noqa: E402
from aiogram.client.telegram import TelegramAPIServer  # noqa: E402
from aiogram.types import (  # noqa: E402
    CallbackQuery,
    Chat,
    Message,
    MessageGenerationStopped,
    Update,
    User,
)

CALLS: list[tuple[str, dict]] = []
MSG_SEQ = iter(range(10_000, 100_000))

OWNER = User(id=111, is_bot=False, first_name="Owner", username="owner")
BOT_USER = dict(id=777000, is_bot=True, first_name="GOHAN", username="gohan_pub_bot")

NOW = int(time.time())


def _chat(chat_id: int) -> dict:
    if chat_id < 0:
        return dict(id=chat_id, type="channel", title="Test Chan", username="testchan")
    return dict(id=chat_id, type="private", first_name="Owner")


def _message(chat_id: int, **extra) -> dict:
    return {
        "message_id": next(MSG_SEQ),
        "date": NOW,
        "chat": _chat(chat_id),
        "from": BOT_USER,
        **extra,
    }


async def _handle(request: web.Request) -> web.Response:
    method = request.match_info["method"]
    post = await request.post()
    body = {k: v for k, v in post.items()}
    CALLS.append((method, body))

    def ok(result) -> web.Response:
        return web.json_response({"ok": True, "result": result})

    chat_id_raw = body.get("chat_id", "111")
    chat_id = int(chat_id_raw) if str(chat_id_raw).lstrip("-").isdigit() else 111

    if method == "getMe":
        return ok({**BOT_USER, "can_join_groups": True,
                   "can_read_all_group_messages": False,
                   "supports_inline_queries": False,
                   "can_connect_to_business": False, "has_main_web_app": False})
    if method == "sendMessage":
        return ok(_message(chat_id, text=body.get("text", ""),
                           reply_markup=json.loads(body["reply_markup"])
                           if "reply_markup" in body else None))
    if method == "sendRichMessage":
        return ok(_message(chat_id))  # request payload is asserted, not echoed
    if method in ("sendRichMessageDraft", "send_message_draft"):
        return ok(True)
    if method == "editMessageText":
        return ok(_message(chat_id, text=body.get("text", "")))
    if method == "editMessageReplyMarkup":
        return ok(_message(chat_id))
    if method == "deleteMessage":
        return ok(True)
    if method == "getChat":
        raw = str(body.get("chat_id", ""))
        if raw in ("@testchan", "-1001"):
            base = dict(id=-1001, type="channel", title="Test Chan",
                        username="testchan", description="A test channel")
        else:
            base = dict(id=111, type="private", first_name="Owner")
        # ChatFullInfo strict requirements (aiogram 3.31)
        return ok({**base, "accent_color_id": 0, "max_reaction_count": 10,
                   "accepted_gift_types": {
                       "unlimited_gifts": True, "limited_gifts": True,
                       "unique_gifts": True, "premium_subscription": True,
                       "gifts_from_channels": True}})
    if method == "getChatMember":
        return ok(dict(user=BOT_USER, status="administrator", can_be_edited=False,
                       can_post_messages=True, can_change_info=True,
                       can_invite_users=True, can_pin_messages=True,
                       can_manage_video_chats=True, can_manage_topics=True,
                       can_delete_messages=True, can_manage_chat=True,
                       can_restrict_members=False, can_promote_members=True,
                       can_post_stories=True, can_edit_stories=True,
                       can_delete_stories=True, can_send_welcome_messages=True,
                       is_anonymous=False))
    if method == "getChatMemberCount":
        return ok(12345)
    if method == "getUpdates":
        return ok([])
    if method == "answerCallbackQuery":
        return ok(True)
    return web.json_response({"ok": False, "error_code": 404,
                              "description": f"unknown method {method}"})


def calls(method: str) -> list[dict]:
    return [b for m, b in CALLS if m == method]


def last_call(method: str) -> dict:
    found = calls(method)
    assert found, f"no {method} calls recorded (all: {[m for m, _ in CALLS]})"
    return found[-1]


def reset() -> None:
    CALLS.clear()


PASS = 0


def ok(label: str) -> None:
    global PASS
    PASS += 1
    print(f"  ok   {label}")


def private_msg(message_id: int, text: str) -> Update:
    return Update(
        update_id=message_id,
        message=Message(
            message_id=message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=111, type="private", first_name="Owner"),
            from_user=OWNER,
            text=text,
        ),
    )


def cb_update(update_id: int, data: str) -> Update:
    inner = Message(
        message_id=500,
        date=datetime.now(timezone.utc),
        chat=Chat(id=111, type="private", first_name="Owner"),
        from_user=BOT_USER and User(**BOT_USER),
        text="(preview)",
    )
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=OWNER,
            chat_instance="ci",
            data=data,
            message=inner,
        ),
    )


async def main() -> int:
    app = web.Application()
    app.router.add_post("/bot{token}/{method}", _handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    session = AiohttpSession(
        api=TelegramAPIServer.from_base(f"http://127.0.0.1:{port}/")
    )
    bot = Bot("12345:TEST-TOKEN", session=session)
    dp = Dispatcher()
    from gohan.handlers import build_router
    from gohan.handlers.actions import on_generation_stopped

    dp.include_router(build_router())
    dp.stopped_message_generation.register(on_generation_stopped)

    tmp = tempfile.mkdtemp(prefix="gohan-it-")
    from gohan.storage import Storage

    storage = Storage(f"{tmp}/it.db")
    await storage.connect()
    from gohan.mtproto import MtpEngine
    from gohan.config import cfg
    from gohan.scheduler import Scheduler

    mtp = MtpEngine(cfg)  # no API_ID in env -> disabled path
    scheduler = Scheduler(bot, storage, mtp)

    async def feed(upd: Update):
        await dp.feed_update(bot, upd, storage=storage, mtp=mtp)

    print("running integration scenarios…")
    try:
        await run_scenarios(feed, storage, scheduler, mtp)
    finally:
        await storage.close()
        await bot.session.close()
        await runner.cleanup()
    print(f"\n✅ {PASS} integration scenarios passed")
    return 0


async def run_scenarios(feed, storage, scheduler, mtp) -> None:

    # 1. /start -> welcome via sendRichMessage ------------------------------
    reset()
    await feed(private_msg(1, "/start"))
    body = last_call("sendRichMessage")
    rich = json.loads(body["rich_message"])
    assert "GOHAN" in rich.get("html", ""), rich
    ok("/start renders welcome via sendRichMessage")

    # 2. plain text draft -> markdown auto-detected rich preview + panel ----
    reset()
    await feed(private_msg(2, "Hello **world**\n\nSecond paragraph here."))
    rich = json.loads(last_call("sendRichMessage")["rich_message"])
    assert "markdown" in rich and "world" in rich["markdown"], rich
    assert "reply_markup" in last_call("sendRichMessage")
    assert "Draft captured" in last_call("sendMessage")["text"]
    draft = await storage.get_draft(111)
    assert draft and draft.mode == "auto" and draft.resolved_mode == "markdown"
    ok("draft intake -> markdown rich preview + control panel")

    # 3. mode toggle -> editMessageText(rich_message=…) in place -----------
    reset()
    await feed(cb_update(3, "g:mode"))
    body = last_call("editMessageText")
    assert "rich_message" in body and "html" in json.loads(body["rich_message"]), body
    ok("mode cycle re-renders via editMessageText(rich_message)")

    # 4. option toggle refreshes the panel ---------------------------------
    reset()
    await feed(cb_update(4, "g:prot"))
    body = last_call("editMessageReplyMarkup")
    assert "Protect: on" in json.dumps(json.loads(body["reply_markup"])), body
    ok("protect toggle refreshes panel keyboard")

    # 5. /addchannel with admin rights -> stored ---------------------------
    reset()
    await feed(private_msg(5, "/addchannel @testchan"))
    assert calls("getChat") and calls("getChatMember") and calls("getChatMemberCount")
    chans = await storage.list_channels()
    assert any(c["chat_id"] == -1001 for c in chans)
    assert "registered" in last_call("sendMessage")["text"]
    ok("/addchannel verifies admin + post rights")

    # 6. /channels lists registered channel -------------------------------
    reset()
    await feed(private_msg(6, "/channels"))
    body = last_call("sendMessage")
    assert "g:to:-1001" in body["reply_markup"], body
    ok("/channels lists channel with send buttons")

    # 7. send-now picker ---------------------------------------------------
    reset()
    await feed(cb_update(7, "g:send"))
    body = last_call("sendMessage")
    assert "g:to:-1001" in body["reply_markup"]
    ok("send picker offers the channel")

    # 8. actual publish -> sendRichMessage to the channel ------------------
    reset()
    await feed(cb_update(8, "g:to:-1001"))
    body = last_call("sendRichMessage")
    assert body["chat_id"] == "-1001", body
    confirm = last_call("sendMessage")
    assert "Published" in confirm["text"] and "bot-api/sendRichMessage" in confirm["text"]
    ok("publish -> sendRichMessage(chat_id=-1001) + confirmation")

    # 9. scheduler publishes a due post ------------------------------------
    reset()
    draft = await storage.get_draft(111)
    post_id = await storage.add_post(111, -1001, draft, run_at=time.time() - 1)
    await scheduler.tick()
    body = last_call("sendRichMessage")
    assert body["chat_id"] == "-1001"
    assert "Scheduled post" in last_call("sendMessage")["text"]
    rows = await storage.pending_posts(111)
    assert not any(r["id"] == post_id for r in rows), "post should be done"
    ok("scheduler publishes due post and notifies owner")

    # 10. /richdemo -> blocks showcase -------------------------------------
    reset()
    await feed(private_msg(10, "/richdemo"))
    rich = json.loads(last_call("sendRichMessage")["rich_message"])
    assert "blocks" in rich and len(rich["blocks"]) >= 10, rich
    ok("/richdemo sends 10.3 block showcase")

    # 11. /latest without MTProto -> honest refusal ------------------------
    reset()
    await feed(private_msg(11, "/latest @testchan"))
    body = last_call("sendMessage")
    assert "MTProto engine is off" in body["text"]
    ok("/latest degrades gracefully without API_ID/API_HASH")

    # 12. /status -> engine report -----------------------------------------
    reset()
    await feed(private_msg(12, "/status"))
    rich = json.loads(last_call("sendRichMessage")["rich_message"])
    assert "aiogram" in rich["html"] and "Kurigram" in rich["html"]
    ok("/status reports both engines")

    # 13. streaming draft + stop -------------------------------------------
    reset()
    await feed(cb_update(13, "g:stream"))
    await asyncio.sleep(3.0)
    drafts = calls("sendRichMessageDraft")
    assert len(drafts) >= 2, f"expected >=2 draft updates, got {len(drafts)}"
    assert drafts[0]["can_stop"].lower() == "true", drafts[0]
    finals = [b for b in calls("sendRichMessage") if b["chat_id"] == "111"]
    assert finals, "final streamed message missing"
    ok("sendRichMessageDraft streams with can_stop, then final send")

    # 14. stopped_message_generation -> cancel + notice ---------------------
    reset()
    await feed(cb_update(14, "g:stream"))
    await asyncio.sleep(0.4)
    stop_upd = Update(
        update_id=15,
        stopped_message_generation=MessageGenerationStopped(
            chat=Chat(id=111, type="private", first_name="Owner"), draft_id=424242
        ),
    )
    await feed(stop_upd)
    await asyncio.sleep(0.3)
    assert any("stopped" in b["text"].lower() for b in calls("sendMessage")), \
        calls("sendMessage")
    await asyncio.sleep(2.5)
    finals = [b for b in calls("sendRichMessage") if b["chat_id"] == "111"]
    # no *new* final rich message after cancellation (the one from reset window)
    assert len(finals) == 0, f"stream finalized despite stop: {len(finals)}"
    ok("stopped_message_generation cancels the stream")

    # 15. button editor flow ------------------------------------------------
    reset()
    await feed(cb_update(16, "g:btnadd"))
    assert (await storage.get_draft(111)).awaiting == "button"
    await feed(private_msg(17, "Docs | https://core.telegram.org/bots/api"))
    draft = await storage.get_draft(111)
    assert draft.buttons and draft.buttons[0]["label"] == "Docs"
    ok("URL button added via Label | url prompt")

    # 16. queued listing ----------------------------------------------------
    reset()
    await feed(private_msg(18, "/queue"))
    assert "empty" in last_call("sendMessage")["text"].lower()
    ok("/queue reports empty queue")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
