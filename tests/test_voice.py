"""Voice engine tests: queue, player, cards, playlists and the away watcher.

Nothing here needs a network, a voice chat or py-tgcalls: the player runs on the
recording null backend and yt-dlp is stubbed out, which is exactly how a
deployment without the extra behaves.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import Chat, Message, User

from gohan.config import Settings
from gohan.storage import Database
from gohan.voice import (
    LoopMode,
    NullBackend,
    PlaylistError,
    PlaylistService,
    Player,
    QueueFull,
    Track,
    VoiceQueue,
    capabilities,
    format_duration,
    parse_time,
    progress_bar,
)
from gohan.voice import cards
from gohan.voice.afk import AfkWatcher, set_away


# ---------------------------------------------------------------------------
#  helpers
# ---------------------------------------------------------------------------


def track(title: str = "Song", *, duration: int | None = 200, video: str | None = None) -> Track:
    return Track(
        title=title,
        url=f"https://youtu.be/{video or title.lower().replace(' ', '-')}",
        duration=duration,
        video_id=video or title.lower().replace(" ", "-"),
        uploader="Channel",
    )


class FakeBot:
    """Just enough Bot for the rich sender and the away watcher."""

    id = 999

    def __init__(self) -> None:
        self.rich: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    async def send_rich_message(self, **kwargs: Any) -> Any:
        self.rich.append(kwargs)
        return SimpleNamespace(
            message_id=len(self.rich),
            chat=SimpleNamespace(id=kwargs.get("chat_id"), type="supergroup"),
        )

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return SimpleNamespace(
            message_id=100 + len(self.sent),
            chat=SimpleNamespace(id=kwargs.get("chat_id"), type="supergroup"),
        )


@pytest.fixture
def patched_sources(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the yt-dlp layer with something instant and countable."""
    from gohan.voice import player as player_module

    calls: dict[str, Any] = {"prepare": [], "related": None}

    async def fake_prepare(t: Track, *, settings: Any = None, prefer_file: bool = False) -> str:
        calls["prepare"].append(t.title)
        return f"file:///tmp/{t.video_id or 'track'}.mp3"

    async def fake_related(t: Track, *, settings: Any = None) -> Track | None:
        return calls["related"]

    monkeypatch.setattr(player_module.sources, "prepare", fake_prepare)
    monkeypatch.setattr(player_module.sources, "related", fake_related)
    return calls


@pytest.fixture
def player(patched_sources: dict[str, Any]) -> Player:
    backend = NullBackend()
    instance = Player(backend=backend, settings=Settings(voice_backend="off"))
    return instance


# ---------------------------------------------------------------------------
#  models
# ---------------------------------------------------------------------------


def test_time_helpers() -> None:
    assert format_duration(0) == "0:00"
    assert format_duration(83) == "1:23"
    assert format_duration(3725) == "1:02:05"
    assert format_duration(None) == "--:--"
    assert parse_time("1:23") == 83
    assert parse_time("1:02:05") == 3725
    assert parse_time("45") == 45
    assert parse_time("nope") is None
    assert parse_time("") is None


def test_progress_bar_shapes() -> None:
    assert "🔘" in progress_bar(0, 100)
    assert progress_bar(100, 100).endswith("🔘")  # finished
    live = progress_bar(12, None)
    assert live.count("━") + live.count("─") == 14


def test_loop_mode_cycles() -> None:
    assert LoopMode.parse("queue") is LoopMode.QUEUE
    assert LoopMode.parse("nonsense") is LoopMode.OFF
    assert LoopMode.OFF.cycle() is LoopMode.ONE
    assert LoopMode.ONE.cycle() is LoopMode.QUEUE
    assert LoopMode.QUEUE.cycle() is LoopMode.OFF
    assert "queue" in LoopMode.QUEUE.label


def test_track_serialisation_never_leaks_stream_urls() -> None:
    item = track("Hello")
    item.stream_url = "https://googlevideo.example/signed?expires=soon"
    payload = item.to_dict()
    assert payload["stream_url"] is None
    assert payload["title"] == "Hello"

    restored = Track.from_dict({"title": "Hi", "url": "u", "unexpected": 5})
    assert restored.title == "Hi" and not hasattr(restored, "unexpected")
    assert Track.from_dict({}).title == "unknown"  # old rows must not crash us


# ---------------------------------------------------------------------------
#  queue
# ---------------------------------------------------------------------------


def test_queue_ordering_and_positions() -> None:
    queue = VoiceQueue(-100)
    assert queue.add(track("A")) == 1
    assert queue.add(track("B")) == 2
    assert queue.add(track("First"), front=True) == 1
    assert [t.title for t in queue] == ["First", "A", "B"]
    assert queue.size == 3
    assert queue.total_duration == 600
    assert queue.next_track is queue.slots[0]


def test_queue_loop_modes_on_advance() -> None:
    queue = VoiceQueue(-100)
    a, b = track("A"), track("B")
    queue.add(a)
    queue.add(b)

    played = queue.pop()
    assert queue.advance(played).title == "B"  # loop off → the next queued track
    assert queue.advance(b) is None  # … and nothing after that
    assert [t.title for t in queue.history] == ["A", "B"]

    queue = VoiceQueue(-100, loop=LoopMode.ONE)
    queue.add(a)
    only = queue.pop()
    assert queue.advance(only) is only  # loop one → repeat
    assert queue.loop_count == 1

    queue = VoiceQueue(-100, loop=LoopMode.QUEUE)
    queue.add(a)
    last = queue.pop()
    assert queue.advance(last) is last  # nothing queued → start over
    assert queue.history and queue.history[-1] is last


def test_queue_limits_and_editing() -> None:
    queue = VoiceQueue(-100, limit=3)
    for name in "ABC":
        queue.add(track(name))
    with pytest.raises(QueueFull):
        queue.add(track("D"))

    assert queue.remove(2).title == "B"
    assert [t.title for t in queue] == ["A", "C"]
    assert queue.move(2, 1) is True
    assert [t.title for t in queue] == ["C", "A"]
    assert queue.jump(2) is None  # only two tracks
    assert queue.move(9, 1) is False

    queue.add(track("A"))  # duplicate video id
    removed = queue.deduplicate()
    assert removed == 1
    assert queue.clear() == 2
    assert len(queue) == 0


def test_queue_pages() -> None:
    queue = VoiceQueue(-100)
    for index in range(25):
        queue.add(track(f"T{index}"))
    page, current, pages = queue.page(2)
    assert (current, pages) == (2, 3)
    assert [t.title for t in page] == [f"T{i}" for i in range(10, 20)]
    snapshot = queue.snapshot(limit=3)
    assert snapshot["size"] == 25 and len(snapshot["tracks"]) == 3
    assert snapshot["tracks"][0]["position"] == 1


# ---------------------------------------------------------------------------
#  player
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_player_plays_enqueued_tracks(player: Player, patched_sources: dict[str, Any]) -> None:
    room, position = await player.enqueue(-100, track("A"))
    assert position == 1
    assert room.current is not None and room.current.title == "A"
    assert room.playing and not room.idle
    assert patched_sources["prepare"] == ["A"]
    backend = player.backend
    assert isinstance(backend, NullBackend)
    assert [call[0] for call in backend.calls][:2] == ["join", "play"]
    assert room.connected is True


@pytest.mark.asyncio
async def test_player_skip_replay_and_queue_advance(player: Player) -> None:
    await player.enqueue(-100, track("A"))
    _, position = await player.enqueue(-100, track("B"))
    assert position == 1, "the queue position of the first waiting track is 1"

    skipped = await player.skip(-100)
    assert skipped is not None and skipped.title == "A"
    assert player.rooms[-100].current.title == "B"

    await player.replay(-100)
    assert player.rooms[-100].offset == 0.0

    await player.enqueue(-100, track("C"))
    await player.skip(-100)
    await player.skip(-100)
    assert -100 not in player.rooms, "skipping past the end leaves the room"


@pytest.mark.asyncio
async def test_player_pause_resume_and_position_math(player: Player) -> None:
    room, _ = await player.enqueue(-100, track("A", duration=400))
    await asyncio.sleep(0.02)
    await player.pause(-100)
    frozen = room.position()
    await asyncio.sleep(0.02)
    assert room.position() == pytest.approx(frozen, abs=0.01), "a paused room must not advance"

    playing = await player.toggle(-100)
    assert playing is True
    assert room.paused is False
    await player.speed(-100, 1.5)
    assert player.rooms[-100].speed == pytest.approx(1.5)
    await player.volume(-100, 130)
    assert player.rooms[-100].volume == 130
    assert await player.mute(-100, True) is True


@pytest.mark.asyncio
async def test_tick_advances_a_finished_track(player: Player) -> None:
    room, _ = await player.enqueue(-100, track("A", duration=1))
    await player.enqueue(-100, track("B", duration=1))
    assert room.current.title == "A"

    room.current.duration = 1
    room.offset = 5.0
    room.started_at = 0.0  # long past the end

    events = await player.tick()
    assert "next" in events
    assert player.rooms[-100].current.title == "B"


@pytest.mark.asyncio
async def test_tick_autoplay_and_autoleave(
    player: Player, patched_sources: dict[str, Any]
) -> None:
    patched_sources["related"] = track("Similar")
    room, _ = await player.enqueue(-100, track("A", duration=1))
    room.autoplay = True
    room.offset = 5.0
    room.started_at = 0.0

    await player.tick()
    assert player.rooms[-100].current.title == "Similar", "autoplay keeps the music going"

    # now drain everything and let autoleave fire
    player.settings.voice_autoleave_sec = 1
    current = player.rooms[-100]
    current.current = None
    current.queue.clear()
    current.empty_since = 0.0
    events = await player.tick()
    assert "autoleave" in events
    assert -100 not in player.rooms
    assert ("leave", -100) in [(call[0], call[1]) for call in player.backend.calls]


@pytest.mark.asyncio
async def test_player_survives_a_broken_source(player: Player, monkeypatch: pytest.MonkeyPatch) -> None:
    from gohan.voice import player as player_module

    async def exploding(t: Track, *, settings: Any = None, prefer_file: bool = False) -> str:
        from gohan.voice import SourceError

        raise SourceError("yt-dlp is not installed")

    monkeypatch.setattr(player_module.sources, "prepare", exploding)
    room, _ = await player.enqueue(-100, track("A"))
    assert room.current is None, "an unplayable track must not be left in the room"


@pytest.mark.asyncio
async def test_player_overview_shape(player: Player) -> None:
    await player.enqueue(-100, track("A"))
    overview = player.overview()
    assert overview["playing"] == 1
    assert overview["backend"]["name"] == "null"
    assert overview["rooms"][0]["state"]["title"]


# ---------------------------------------------------------------------------
#  playlists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_playlist_service_round_trip(tmp_path: Path) -> None:
    db = Database(tmp_path / "playlists.sqlite3")
    await db.connect()
    try:
        service = PlaylistService(db)
        info = await service.create(42, "  Chill  Vibes! ")
        assert info.name == "Chill Vibes"

        with pytest.raises(PlaylistError):
            await service.create(42, "chill vibes")  # case-insensitive clash

        info, added = await service.add_tracks(42, "Chill Vibes", [track("A"), track("B")])
        assert added == 2 and info.tracks == 2

        by_id = await service.find(42, str(info.id))
        assert by_id.id == info.id

        tracks = await service.tracks(info.id)
        assert [t.title for t in tracks] == ["A", "B"]

        info, removed = await service.remove_track(42, "Chill Vibes", 1)
        assert removed.title == "A" and info.tracks == 1

        with pytest.raises(PlaylistError):
            await service.remove_track(42, "Chill Vibes", 9)

        with pytest.raises(PlaylistError):
            await service.find(7, "chill vibes")  # somebody else's playlist

        deleted = await service.delete(42, "chill vibes")
        assert deleted.name == "Chill Vibes"
        assert await service.listing(42) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_playlist_limits(tmp_path: Path) -> None:
    db = Database(tmp_path / "limits.sqlite3")
    await db.connect()
    try:
        service = PlaylistService(db, max_playlists=1, max_tracks=2)
        await service.create(42, "one")
        with pytest.raises(PlaylistError):
            await service.create(42, "two")

        # it fills what fits, then refuses to grow past the cap
        info, added = await service.add_tracks(42, "one", [track("A"), track("B"), track("C")])
        assert added == 2 and info.tracks == 2
        with pytest.raises(PlaylistError):
            await service.add_tracks(42, "one", [track("D")])
    finally:
        await db.close()


# ---------------------------------------------------------------------------
#  storage: away mode and controllers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_afk_and_voice_auth_storage(tmp_path: Path) -> None:
    db = Database(tmp_path / "afk.sqlite3")
    await db.connect()
    try:
        await db.upsert_user(7, username="meera", first_name="Meera")
        await db.upsert_chat(-100, title="Group", type="supergroup")

        await set_away(db, 7, -100, "sleeping")
        entry = await db.afk_entry(7, -100)
        assert entry is not None and entry["reason"] == "sleeping"
        assert await db.count_afk() == 1
        assert (await db.afk_in_chat(-100))[0]["first_name"] == "Meera"
        assert await db.clear_afk(7, -100) is True
        assert await db.clear_afk(7, -100) is False

        assert await db.allow_voice(-100, 7, added_by=42) is True
        assert await db.allow_voice(-100, 7, added_by=42) is False  # already there
        assert await db.is_voice_allowed(-100, 7) is True
        assert await db.voice_allowed(-100) == [7]
        assert (await db.voice_controllers())[0]["chat_title"] == "Group"
        assert await db.deny_voice(-100, 7) is True
        assert await db.voice_allowed(-100) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_afk_watcher_greets_and_clears(tmp_path: Path) -> None:
    db = Database(tmp_path / "watcher.sqlite3")
    await db.connect()
    bot = FakeBot()
    try:
        await db.upsert_user(7, username="meera", first_name="Meera")
        await set_away(db, 7, -100, "lunch")

        watcher = AfkWatcher(db, bot)
        message = Message.model_construct(
            message_id=5,
            date=datetime.now(timezone.utc),
            chat=Chat.model_construct(id=-100, type="supergroup", title="Group"),
            from_user=User.model_construct(id=7, is_bot=False, first_name="Meera"),
            text="back now",
            entities=[],
            caption_entities=[],
            reply_to_message=None,
        )
        seen: list[str] = []

        async def handler(event: Message, data: dict[str, Any]) -> str:
            seen.append("handled")
            return "ok"

        result = await watcher(handler, message, {"bot": bot, "db": db})
        assert result == "ok" and seen == ["handled"], "the update must still be processed"
        assert bot.rich, "the welcome-back card is sent"
        assert await db.afk_entry(7, -100) is None

        # a mention of somebody who is away gets a notice
        await set_away(db, 8, -100, "busy")
        await db.upsert_user(8, username="arjun", first_name="Arjun")
        mention = Message.model_construct(
            message_id=6,
            date=datetime.now(timezone.utc),
            chat=Chat.model_construct(id=-100, type="supergroup", title="Group"),
            from_user=User.model_construct(id=7, is_bot=False, first_name="Meera"),
            text="@arjun ping",
            entities=[SimpleNamespace(type="mention", offset=0, length=6)],
            caption_entities=[],
            reply_to_message=None,
        )
        before = len(bot.rich)
        await watcher(handler, mention, {"bot": bot, "db": db})
        assert len(bot.rich) > before, "an away user must be announced"
        body = str(bot.rich[-1])
        assert "busy" in body or "ᴀᴡᴀʏ" in body.lower() or "away" in body.lower()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
#  cards
# ---------------------------------------------------------------------------


def _state(**overrides: Any) -> Any:
    from gohan.voice.models import PlaybackState

    payload = {
        "chat_id": -100,
        "title": "Song",
        "url": "https://youtu.be/x",
        "thumbnail": "https://i.ytimg.com/vi/x/hqdefault.jpg",
        "uploader": "Channel",
        "requested_by": 7,
        "requested_name": "Meera",
        "position": 30.0,
        "duration": 200,
        "queue_length": 2,
        "queue_duration": 400,
        "connected": True,
        "playing": True,
    }
    payload.update(overrides)
    return PlaybackState(**payload)


def test_now_playing_card_uses_tables_media_and_a_live_clock() -> None:
    html = cards.now_playing(_state())
    assert "<h2>" in html and "ɴᴏᴡ ᴘʟᴀʏɪɴɢ" in html
    assert "<table" in html
    assert "<img src=" in html, "cover art belongs in the card"
    assert "<tg-time unix=" in html, "the end time must be a live entity"
    assert "0:30 / 3:20" in html


def test_now_playing_without_a_track() -> None:
    from gohan.rich import ui

    html = cards.now_playing(_state(title=None, duration=None, thumbnail=None, position=0))
    assert ui.caps("now playing") in html
    assert "/play" in html  # it tells you what to do about it
    assert "<table" not in html


def test_control_buttons_are_coloured_and_round_trip() -> None:
    markup = cards.now_playing_buttons(_state())
    flat = [button for row in markup.inline_keyboard for button in row]
    assert flat, "the card must come with controls"
    assert {button.style for button in flat} <= {"primary", "success", "danger", "link"}
    assert {"primary", "danger"} <= {button.style for button in flat}

    paused = cards.now_playing_buttons(_state(paused=True))
    assert "success" in {button.style for row in paused.inline_keyboard for button in row}

    compact = cards.now_playing_buttons(_state(), compact=True)
    assert len(compact.inline_keyboard) == 1
    assert any(button.icon_custom_emoji_id for button in flat), "custom emoji are used"

    for button in flat:
        if button.callback_data:
            action, chat_id, _value = cards.parse_cb(button.callback_data)
            assert action and chat_id == -100

    assert cards.parse_cb("garbage") == ("", None, None)


def test_queue_card_pages_and_buttons() -> None:
    from gohan.rich import ui

    state = _state()
    tracks = [track(f"T{index}") for index in range(3)]
    html = cards.queue_card(state, tracks, 1, 2, total=13)
    assert ui.caps("queue") in html and "<table" in html and f"{ui.caps('page')} 1/2" in html
    markup = cards.queue_buttons(state, 1, 2, tracks=tracks)
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "1" in labels and "ɴᴇxᴛ ›" in labels
    assert "‹ ᴘʀᴇᴠ" not in labels


def test_search_playlist_help_and_error_cards() -> None:
    results = [track(f"Hit {index}") for index in range(4)]
    from gohan.rich import ui

    search_html = cards.search_card("query", results)
    assert ui.caps("search results") in search_html and "<table" in search_html
    search_markup = cards.search_buttons(results, -100)
    picks = [button for row in search_markup.inline_keyboard for button in row if button.callback_data]
    assert picks and all(cards.parse_cb(b.callback_data)[1] == -100 for b in picks)

    assert ui.caps("search results") in cards.search_card("nothing", [])

    from gohan.voice.playlists import PlaylistInfo

    info = PlaylistInfo(id=3, name="lofi", owner_id=42, tracks=12)
    html = cards.playlist_card(info, results[:2], 1, 2)
    assert "lofi" in html and ui.caps("playlist") in html
    markup = cards.playlist_buttons(info, 1, 2, chat_id=-100, tracks=results[:2])
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert any(":pl:-100:3:2" in data for data in callbacks)
    assert any(data.endswith(":ptrack:-100:3:1") for data in callbacks)

    help_html = cards.help_card("play")
    assert ui.caps("playback") in help_html and "<table" in help_html
    assert "/play &lt;song or link&gt;" in help_html, "help text is escaped, never raw markup"
    all_help = cards.help_card("everything")
    assert ui.caps("the queue") in all_help and ui.caps("sound") in all_help

    settings_html = cards.settings_card({"autoplay": True, "autoleave": 300, "volume": 90}, chat_title="G")
    assert "autoplay" in settings_html and "<table" in settings_html
    settings_markup = cards.settings_buttons({"autoplay": True, "autoleave": 0})
    pref_callbacks = [b.callback_data for row in settings_markup.inline_keyboard for b in row]
    assert "v:toggle_pref::autoplay" in pref_callbacks
    assert "v:vol::-10" in pref_callbacks, "a value without a chat id must not be read as one"

    error = cards.error_card("nope", "hint")
    assert "nope" in error and ui.caps("hint") in error


def test_afk_cards_render() -> None:
    import time

    from gohan.rich import ui

    assert ui.caps("away") in cards.afk_card("lunch", user_name="Meera")
    assert "welcome back" in cards.afk_leave_card(count=2)
    ping = cards.afk_ping_card("Meera", "lunch", since=time.time() - 600)
    assert "Meera" in ping and "<tg-time" in ping
    assert ui.caps("player controllers") in cards.auth_card([7], names={7: "Meera"})
    status = cards.status_card(
        {"backend": {"name": "null", "available": False, "warnings": []}, "playing": 0, "queued": 0, "rooms": []},
        capabilities(Settings()),
    )
    assert ui.caps("voice status") in status and "null" in status


# ---------------------------------------------------------------------------
#  capabilities
# ---------------------------------------------------------------------------


def test_capabilities_report_actionable_notes() -> None:
    caps = capabilities(Settings())
    assert isinstance(caps.ready, bool)
    assert caps.note
    if not caps.yt_dlp:
        assert "yt-dlp" in caps.note
