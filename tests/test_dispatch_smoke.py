"""End-to-end smoke tests: real dispatcher, mocked Telegram.

An aiogram :class:`Dispatcher` is built exactly as the bot builds it, then
updates are fed through it. Every Bot API call is mocked, so these tests catch
handler wiring bugs (wrong data keys, unbound services, bad filters) without a
token or a network.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

import pytest_asyncio

from gohan.config import Settings
from gohan.dispatcher import build_dispatcher
from gohan.storage import Database

pytestmark = pytest.mark.asyncio


def _message(chat_id: int, text: str, *, user_id: int = 42, chat_type: str = "private") -> Message:
    """A minimal but valid Message (built without validation, like aiogram's own tests)."""
    message = Message.model_construct(
        message_id=100,
        date=datetime.now(timezone.utc),
        chat=Chat.model_construct(id=chat_id, type=chat_type, title="Testers"),
        from_user=User.model_construct(id=user_id, is_bot=False, first_name="Gohan", username="gohan"),
        text=text,
        caption=None,
        entities=[],
        reply_to_message=None,
    )
    return message


def _update(message: Message, update_id: int = 1) -> Update:
    return Update.model_construct(update_id=update_id, message=message)


class FakeBot:
    """Enough of a Bot for the handlers: every method is a recording AsyncMock."""

    #: aiogram reads ``bot.id`` while logging every update
    id = 999
    session = None
    default = None

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.photos: list[dict[str, Any]] = []
        self._sent_id = 500

    async def send_rich_message(self, **kwargs: Any) -> Any:
        self._sent_id += 1
        self.sent.append(kwargs)
        return _sent(kwargs["chat_id"], self._sent_id)

    async def send_message(self, chat_id: Any = None, text: Any = None, **kwargs: Any) -> Any:
        self._sent_id += 1
        self.sent.append({"plain": True, "chat_id": chat_id, "text": text, **kwargs})
        return _sent(chat_id, self._sent_id)

    async def edit_message_text(self, **kwargs: Any) -> Any:
        self.edits.append(kwargs)
        return _sent(kwargs.get("chat_id"), kwargs.get("message_id", 1))

    async def send_photo(self, chat_id: int, photo: Any, **kwargs: Any) -> Any:
        self.photos.append({"chat_id": chat_id, "photo": photo, **kwargs})
        return _sent(chat_id, 501)

    async def send_audio(self, chat_id: int, audio: Any, **kwargs: Any) -> Any:
        self.sent.append({"audio": True, "chat_id": chat_id, **kwargs})
        return _sent(chat_id, 502)

    async def send_document(self, chat_id: int, document: Any, **kwargs: Any) -> Any:
        return _sent(chat_id, 503)

    async def send_dice(self, chat_id: int, **kwargs: Any) -> Any:
        return SimpleNamespace(message_id=504, dice=SimpleNamespace(value=6))

    async def get_me(self) -> Any:
        return User.model_construct(id=999, is_bot=True, first_name="Gohan", username="gohan_bot")

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        from aiogram.enums import ChatMemberStatus

        return SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR, user=SimpleNamespace(id=user_id))

    async def get_chat(self, chat_id: int) -> Any:
        return Chat.model_construct(id=chat_id, type="supergroup", title="Testers")

    async def delete_message(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def answer_callback_query(self, *args: Any, **kwargs: Any) -> bool:
        return True

    # attributes aiogram/telegram libraries may probe; answering them with a
    # coroutine function would break the event loop, so they raise instead
    _NOT_CALLABLE = {"session", "id", "default", "context", "token", "parse_mode", "username"}

    async def _noop(self, *args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            message_id=599, chat=SimpleNamespace(id=kwargs.get("chat_id", 1))
        )

    def __getattr__(self, name: str) -> Any:
        # anything else (restrict_chat_member, ban_chat_member, …) is a no-op mock
        if name.startswith("_") or name in self._NOT_CALLABLE:
            raise AttributeError(name)
        return self._noop


def _sent(chat_id: Any, message_id: int) -> Any:
    return SimpleNamespace(
        message_id=message_id,
        chat=SimpleNamespace(id=chat_id, title="Testers", type="private"),
        from_user=SimpleNamespace(id=999, is_bot=True, username="gohan_bot"),
    )


def _services(db: Database) -> dict[str, Any]:
    from gohan.ai.chat import AIConversation, ConversationStore
    from gohan.ai.llm import DemoGenerator
    from gohan.fun import AnimationProvider
    from gohan.games import GameManager
    from gohan.guardian.antiraid import RaidDetector
    from gohan.guardian.captcha import CaptchaManager

    settings = Settings(bot_token="123456:TEST", owner_user_ids="42", llm_provider="off")
    bot = FakeBot()
    store = ConversationStore()
    from gohan.voice import AfkWatcher, build_player

    return {
        "settings": settings,
        "player": build_player(settings, db=db),
        "afk": AfkWatcher(db, bot),
        "db": db,
        "bot": bot,
        "ai": AIConversation(
            bot=bot,  # type: ignore[arg-type]
            llm=DemoGenerator(),
            store=store,
            db=db,
            settings=settings,
        ),
        "store": store,
        "animations": AnimationProvider(db),
        "captcha": CaptchaManager(),
        "games": GameManager(),
        "raid": RaidDetector(),
        "log_channel": None,
        "mtproto": None,
        "watchdog": None,
    }


@pytest_asyncio.fixture
async def harness(tmp_path: Path):
    """A dispatcher + database that always clean up, even when a test fails."""
    db = Database(tmp_path / "smoke.sqlite3")
    await db.connect()
    services = _services(db)
    dp, order = build_dispatcher(services["settings"], services)
    try:
        yield services["bot"], db, dp, order
    finally:
        await db.close()


async def _dispatch(harness, updates: list[Update]) -> FakeBot:
    bot, _db, dp, _order = harness
    for update in updates:
        await dp.feed_update(bot, update)  # type: ignore[arg-type]
    return bot


async def test_start_replies_with_the_brand_line(harness) -> None:
    bot = await _dispatch(harness, [_update(_message(1, "/start"))])
    assert bot.sent, "/start must answer"
    body = str(bot.sent[0])
    assert "AI Chatbot" in body or "ᴀɪ" in body
    user = await harness[1].get_user(42)
    assert user is not None and user.start_count == 1


async def test_owner_panel_command_renders_buttons(harness) -> None:
    bot = await _dispatch(harness, [_update(_message(1, "/owner"), 2)])
    assert bot.sent, "/owner must answer"
    markup = bot.sent[0].get("reply_markup")
    assert markup is not None, "the owner panel must come with a keyboard"
    styles = {b.style for row in markup.inline_keyboard for b in row}
    assert styles <= {"primary", "success", "danger", "link"}
    assert {"success", "danger"} <= styles  # actions and destructive ones are coloured


async def test_group_filter_is_saved_and_answered(harness) -> None:
    group = _message(-1001, "/filter hello hi there!", user_id=42, chat_type="supergroup")
    trigger = _message(-1001, "well hello everyone", user_id=7, chat_type="supergroup")
    bot = await _dispatch(harness, [_update(group, 3), _update(trigger, 4)])

    row = await harness[1].get_filter(-1001, "hello")
    assert row is not None and row["reply"].startswith("hi there")
    assert row["uses"] == 1, "the trigger must be counted when it fires"
    assert any("hi there" in str(call) for call in bot.sent)


async def test_help_lists_the_new_sections(harness) -> None:
    bot = await _dispatch(harness, [_update(_message(1, "/help"), 5)])
    body = str(bot.sent)
    for probe in ("/help filters", "/help anime", "/help music", "/help notes", "/help owner"):
        assert probe in body, probe


async def test_guardian_panel_is_admin_only_in_groups(harness) -> None:
    # user 42 is the owner (and therefore an admin) - the panel must render
    bot = await _dispatch(harness, [_update(_message(-1001, "/guardian", chat_type="supergroup"), 6)])
    assert bot.sent, "/guardian must answer for an admin"


async def test_ping_runs_without_a_reply_target(harness) -> None:
    bot = await _dispatch(harness, [_update(_message(1, "/ping"), 7)])
    assert bot.sent, "/ping must answer"


async def test_every_router_is_attached(harness) -> None:
    _bot, _db, _dp, order = harness
    assert len(order) == 13
    assert "voice" in order, "the music player must be attached"
    assert order.index("voice") < order.index("ai"), "the player answers before the AI catch-all"
    assert order[-2:] == ["guardian.filters", "guardian.events"], (
        "filters must run before the guard, and the guard must run last"
    )


async def test_play_degrades_gracefully_without_ytdlp(harness, monkeypatch) -> None:
    """No yt-dlp (or no py-tgcalls) must produce a card, never a traceback."""
    from gohan.voice import SourceError
    from gohan.voice import handlers as voice_handlers

    async def exploding(*_args, **_kwargs):
        raise SourceError("yt-dlp is not installed - run: pip install 'gohan[voice]'")

    monkeypatch.setattr(voice_handlers, "search", exploding)
    bot = await _dispatch(harness, [_update(_message(-1001, "/play never gonna give you up", chat_type="supergroup"), 8)])
    assert bot.sent, "the player must explain itself"
    body = str(bot.sent)
    assert "yt-dlp" in body or "not installed" in body


async def test_queue_without_a_room_explains_itself(harness) -> None:
    bot = await _dispatch(harness, [_update(_message(-1002, "/queue", chat_type="supergroup"), 9)])
    assert bot.sent
    assert "no queue" in str(bot.sent).lower() or "queue" in str(bot.sent).lower()


async def test_music_help_lists_the_command_groups(harness) -> None:
    bot = await _dispatch(harness, [_update(_message(1, "/musichelp"), 10)])
    body = str(bot.sent)
    assert "/play" in body and "/queue" in body and "/loop" in body
