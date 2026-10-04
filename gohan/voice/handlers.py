"""The music commands and the ``v:*`` control callbacks.

Ported from AnvuMusic's ``play.go``/``cb.go``/``playlist.go``/``afk.go`` and
rebuilt around the new Bot API pieces:

* search runs behind a streamed ``sendRichMessageDraft``, so the person sees
  "searching…" instead of a typing indicator;
* in groups the control panel is posted **ephemerally** - visible only to the
  person who asked - and a button press *replaces* it
  (``EphemeralMessageParameters.replace_callback_query_message``), so the chat
  history stays clean;
* every screen is a rich message with tables, cover art and a live
  ``<tg-time>`` countdown;
* the buttons are coloured inline buttons with custom-emoji glyphs, on top of
  the rich-body ``<tg-button-row>``.

Nothing here assumes audio can actually be produced: with a null backend the
queue, the cards and the panel behave the same.
"""

from __future__ import annotations

import random
import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ..config import Settings
from ..filters import RateLimit, is_admin, is_owner_id
from ..logging_setup import get_logger
from ..rich import keys
from ..rich import ui
from ..rich.sender import (
    ephemeral_send,
    rich_edit,
    rich_reply,
)
from ..storage import Database
from . import cards
from .afk import set_away
from .models import Track, format_duration, parse_time
from .player import Player, RoomError
from .playlists import PlaylistError, PlaylistService
from .queue import QueueFull
from .sources import Capabilities, SourceError, capabilities, expand, resolve, search, too_long

log = get_logger("voice.handlers")

router = Router(name="voice")

#: one search at a time per person; playback controls are cheap but still capped
play_limit = RateLimit(2.5, message="one search at a time 🎧")
control_limit = RateLimit(0.7, message="slow down a touch 🙂")

#: last results per chat, so ``v:pick:<chat>:<n>`` can resolve a choice
_searches: dict[int, tuple[float, list[Track]]] = {}
SEARCH_TTL = 300.0

#: chat-administrator lookups, cached briefly (they cost an API call each)
_admins: dict[tuple[int, int], tuple[float, bool]] = {}
ADMIN_TTL = 60.0


# ---------------------------------------------------------------------------
#  small shared helpers
# ---------------------------------------------------------------------------


def _caps(settings: Settings) -> Capabilities:
    return capabilities(settings)


def _playlists(db: Database) -> PlaylistService:
    return PlaylistService(db)


async def _can_control(bot: Bot, db: Database, settings: Settings, chat_id: int, user_id: int) -> bool:
    """Owner, chat admin or an explicitly authorised controller."""
    if is_owner_id(user_id, settings):
        return True
    try:
        if await db.is_voice_allowed(chat_id, user_id):
            return True
    except Exception:  # pragma: no cover - older databases
        pass
    key = (chat_id, user_id)
    now = time.monotonic()
    cached = _admins.get(key)
    if cached and now - cached[0] < ADMIN_TTL:
        return cached[1]
    try:
        allowed = await is_admin(bot, chat_id, user_id, settings)
    except Exception:  # pragma: no cover - avoid breaking playback on API hiccups
        allowed = False
    _admins[key] = (now, allowed)
    return allowed


async def _deny(message: Message, settings: Settings) -> None:
    await rich_reply(
        message,
        cards.error_card(
            "only admins drive the player here",
            "ask an admin to /auth you, or use /voice to see who is connected",
        ),
    )


def _service(data: dict[str, Any], name: str, default: Any = None) -> Any:
    return data.get(name, default)


async def _player(data: dict[str, Any]) -> Player | None:
    player = data.get("player")
    return player if isinstance(player, Player) else None


def _remember_search(chat_id: int, tracks: list[Track]) -> None:
    _searches[chat_id] = (time.monotonic(), tracks)
    # keep the cache small: drop stale entries
    for key, (stamp, _) in list(_searches.items()):
        if time.monotonic() - stamp > SEARCH_TTL:
            _searches.pop(key, None)


def _last_search(chat_id: int) -> list[Track]:
    entry = _searches.get(chat_id)
    if entry is None or time.monotonic() - entry[0] > SEARCH_TTL:
        return []
    return entry[1]


def _looks_like_playlist(url: str) -> bool:
    text = str(url or "").lower()
    return any(
        marker in text
        for marker in ("list=", "/playlist", "/album/", "/sets/", "/channel/", "/@", "music.youtube.com/playlist")
    )


def _chat_title(message: Message) -> str:
    chat = message.chat
    return chat.title or getattr(chat, "full_name", "") or str(chat.id)


async def _stream_draft(bot: Bot, chat_id: int, html: str) -> int | None:
    """Show a search progress note through ``sendRichMessageDraft``."""
    draft_id = random.randint(1, 2_000_000_000)
    try:
        from aiogram.types import InputRichMessage

        await bot.send_rich_message_draft(
            chat_id=chat_id, draft_id=draft_id, rich_message=InputRichMessage(html=html)
        )
        return draft_id
    except TelegramAPIError as exc:
        log.debug("rich draft rejected: %s", exc)
        return None


async def _clear_draft(bot: Bot, chat_id: int, draft_id: int | None, *, keep: bool = False) -> None:
    if draft_id is None:
        return
    try:
        from aiogram.types import InputRichMessage

        await bot.send_rich_message_draft(
            chat_id=chat_id,
            draft_id=draft_id,
            rich_message=InputRichMessage(html=" "),
            can_stop=True,
            keep_on_stop=keep,
        )
    except TelegramAPIError as exc:
        log.debug("draft cleanup failed: %s", exc)


# ---------------------------------------------------------------------------
#  the control panel
# ---------------------------------------------------------------------------


async def _post_panel(
    message: Message,
    data: dict[str, Any],
    html: str,
    markup: InlineKeyboardMarkup | None,
    *,
    ephemeral_for: int | None = None,
) -> None:
    """Post a card.

    In groups it is sent *ephemerally* to the requester (``ephemeral_for``), so
    only they see the controls; everywhere else it is a normal message whose id
    we remember so later updates can edit it in place.
    """
    bot: Bot = data["bot"]
    player = await _player(data)
    chat_id = message.chat.id
    settings: Settings = data["settings"]

    if ephemeral_for and message.chat.type in ("group", "supergroup") and settings.voice_enabled:
        sent = await ephemeral_send(
            bot, chat_id, html, receiver_user_id=ephemeral_for, reply_markup=markup
        )
        if sent is not None:
            return

    sent = await rich_reply(message, html, reply_markup=markup)
    if sent is not None and player is not None:
        room = player.rooms.get(chat_id)
        if room is not None:
            room.panel = (chat_id, sent.message_id)


async def _refresh_panel(bot: Bot, player: Player, chat_id: int) -> None:
    """Edit the tracked panel (if any) into the current state."""
    room = player.rooms.get(chat_id)
    if room is None or room.panel is None:
        return
    panel_chat, panel_message = room.panel
    state = room.state(backend=player.backend_name)
    try:
        await rich_edit(
            bot,
            chat_id=panel_chat,
            message_id=panel_message,
            html=cards.now_playing(state),
            reply_markup=cards.now_playing_buttons(state, compact=True),
        )
    except TelegramAPIError as exc:  # pragma: no cover - panel may be deleted
        log.debug("panel refresh failed: %s", exc)


def make_panel_hook(bot: Bot) -> Any:
    """The player's event hook: keep a tracked panel honest as tracks change."""

    async def hook(event: str, room: Any) -> None:
        if event not in ("play", "next", "finished", "stop", "pause", "resume", "loop"):
            return
        if room.panel is None:
            return
        chat_id, message_id = room.panel
        try:
            state = room.state(backend="pytgcalls")
            await rich_edit(
                bot,
                chat_id=chat_id,
                message_id=message_id,
                html=cards.now_playing(state),
                reply_markup=cards.now_playing_buttons(state, compact=True),
            )
        except TelegramAPIError as exc:
            log.debug("panel hook skipped: %s", exc)

    return hook


# ---------------------------------------------------------------------------
#  playback commands
# ---------------------------------------------------------------------------


@router.message(Command("play", "p"), play_limit)
async def cmd_play(
    message: Message,
    command: CommandObject,
    db: Database,
    settings: Settings,
    bot: Bot,
    player: Player,
) -> None:
    """``/play <song|link>`` - search, then queue (or offer choices)."""
    query = (command.args or "").strip()
    chat_id = message.chat.id
    requester = message.from_user

    # a replied-to audio file is a track too
    if not query and message.reply_to_message is not None:
        track = await _track_from_reply(message.reply_to_message, bot, settings)
        if track is not None:
            track.requested_by = requester.id if requester else None
            track.requested_name = requester.full_name if requester else None
            await _queue_track(message, {"bot": bot, "settings": settings, "player": player}, track)
            return

    if not query:
        await rich_reply(
            message,
            cards.error_card("give me something to play", "/play <song name> · /play <link> · or reply to an audio file"),
        )
        return

    preferences = await player.preferences(chat_id)
    draft = await _stream_draft(bot, chat_id, cards.loading_card(query))
    try:
        if _looks_like_playlist(query):
            tracks = await expand(query, settings=settings, limit=50)
            tracks = _filter_duration(tracks, int(preferences.get("max_duration") or 0))
            if not tracks:
                raise SourceError("that playlist has nothing playable")
            for track in tracks:
                track.requested_by = requester.id if requester else None
                track.requested_name = requester.full_name if requester else None
            room, added = await player.enqueue_many(chat_id, tracks)
            state = room.state(backend=player.backend_name)
            await _clear_draft(bot, chat_id, draft)
            await _post_panel(
                message,
                {"bot": bot, "settings": settings, "player": player},
                "\n\n".join(
                    [
                        cards.now_playing(state),
                        ui.panel(f"{ui.e('check')} queued <b>{added}</b> track(s) from the playlist", icon="playlist"),
                    ]
                ),
                cards.now_playing_buttons(state),
                ephemeral_for=requester.id if requester else None,
            )
            return

        tracks = await search(query, limit=5, settings=settings)
        allowed = _filter_duration(tracks, int(preferences.get("max_duration") or 0))
        if not allowed:
            raise SourceError(
                f"everything found is longer than {preferences.get('max_duration')} min"
                if preferences.get("max_duration")
                else "nothing found"
            )
        _remember_search(chat_id, allowed)
        if len(allowed) == 1:
            track = allowed[0]
            track.requested_by = requester.id if requester else None
            track.requested_name = requester.full_name if requester else None
            await _clear_draft(bot, chat_id, draft)
            await _queue_track(message, {"bot": bot, "settings": settings, "player": player}, track)
            return

        await _clear_draft(bot, chat_id, draft)
        await _post_panel(
            message,
            {"bot": bot, "settings": settings, "player": player},
            cards.search_card(query, allowed),
            cards.search_buttons(allowed, chat_id),
            ephemeral_for=requester.id if requester else None,
        )
    except SourceError as exc:
        await _clear_draft(bot, chat_id, draft)
        await rich_reply(message, cards.error_card(str(exc), "try a different spelling, or paste a link"))
    except Exception as exc:  # pragma: no cover - last line of defence
        await _clear_draft(bot, chat_id, draft)
        log.warning("play failed: %s", exc)
        await rich_reply(message, cards.error_card("the search failed", str(exc)[:140]))


def _filter_duration(tracks: list[Track], minutes: int) -> list[Track]:
    if not minutes:
        return tracks
    return [track for track in tracks if not too_long(track, minutes)]


async def _track_from_reply(message: Message, bot: Bot, settings: Settings) -> Track | None:
    """Turn a replied-to audio/voice/document into a playable track."""
    media = message.audio or message.voice or message.document or message.video
    if media is None:
        return None
    title = (
        getattr(media, "title", None)
        or getattr(media, "file_name", None)
        or f"{message.from_user.full_name if message.from_user else 'someone'}'s audio"
    )
    track = Track(
        title=str(title),
        url=f"telegram:{getattr(media, 'file_unique_id', getattr(media, 'file_id', 'file'))}",
        duration=getattr(media, "duration", None),
        source="telegram",
        thumbnail=None,
    )
    try:
        target_dir = settings.voice_download_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / f"{getattr(media, 'file_unique_id', 'file')}.audio"
        file = await bot.get_file(media.file_id)
        await bot.download_file(file.file_path, destination=destination)
        track.path = str(destination)
        track.url = str(destination)
    except Exception as exc:  # pragma: no cover - download quotas etc.
        log.warning("could not fetch the replied file: %s", exc)
        return None
    return track


async def _queue_track(message: Message, data: dict[str, Any], track: Track) -> None:
    player: Player = data["player"]
    bot: Bot = data["bot"]
    settings: Settings = data["settings"]
    chat_id = message.chat.id
    try:
        room, position = await player.enqueue(chat_id, track)
    except QueueFull as exc:
        await rich_reply(message, cards.error_card(str(exc), "clear some room with /remove or /clear"))
        return
    state = room.state(backend=player.backend_name)
    queued = position > 1 or room.current is not track
    note = (
        ui.panel(
            f"{ui.e('add')} added at position <b>{position}</b>",
            f"\n{ui.e('list')} {len(room.queue)} waiting · {ui.esc(room.queue.total_text)}",
            icon="queue",
        )
        if queued
        else ""
    )
    body = "\n\n".join(part for part in (cards.now_playing(state), note) if part)
    await _post_panel(
        message,
        {"bot": bot, "settings": settings, "player": player},
        body,
        cards.now_playing_buttons(state),
        ephemeral_for=message.from_user.id if message.from_user else None,
    )
    await _refresh_panel(bot, player, chat_id)


@router.message(Command("playlist", "pl"), play_limit)
async def cmd_playlist(message: Message, command: CommandObject, settings: Settings, player: Player) -> None:
    """``/playlist <url>`` - queue an album or playlist."""
    url = (command.args or "").strip()
    if not url:
        await rich_reply(message, cards.error_card("paste a playlist link", "/playlist <url>"))
        return
    try:
        tracks = await expand(url, settings=settings, limit=50)
    except SourceError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    if message.from_user:
        for track in tracks:
            track.requested_by = message.from_user.id
            track.requested_name = message.from_user.full_name
    room, added = await player.enqueue_many(message.chat.id, tracks)
    state = room.state(backend=player.backend_name)
    await _post_panel(
        message,
        {"bot": message.bot, "settings": settings, "player": player},
        "\n\n".join(
            [
                cards.now_playing(state),
                ui.panel(f"{ui.e('playlist')} queued <b>{added}</b> track(s)", icon="playlist"),
            ]
        ),
        cards.now_playing_buttons(state),
        ephemeral_for=message.from_user.id if message.from_user else None,
    )


@router.message(Command("pause"), control_limit)
async def cmd_pause(message: Message, player: Player, settings: Settings) -> None:
    await _simple_control(message, player, settings, lambda: player.pause(message.chat.id), "paused")


@router.message(Command("resume"), control_limit)
async def cmd_resume(message: Message, player: Player, settings: Settings) -> None:
    await _simple_control(message, player, settings, lambda: player.resume(message.chat.id), "resumed")


@router.message(Command("skip", "next"), control_limit)
async def cmd_skip(message: Message, command: CommandObject, player: Player, settings: Settings) -> None:
    count = 1
    if command.args and command.args.strip().isdigit():
        count = int(command.args.strip())
    try:
        skipped = await player.skip(message.chat.id, count)
    except RoomError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    room = player.rooms.get(message.chat.id)
    if room is None or room.current is None:
        await rich_reply(message, cards.error_card("the queue ran out", "add something with /play"))
        return
    state = room.state(backend=player.backend_name)
    await rich_reply(
        message,
        "\n\n".join(
            [
                ui.panel(
                    f"{ui.e('forward')} skipped <b>{ui.esc(skipped.display_title if skipped else '?')}</b>"
                    + (f" ×{count}" if count > 1 else ""),
                    icon="forward",
                ),
                cards.now_playing(state),
            ]
        ),
        reply_markup=cards.now_playing_buttons(state),
    )


@router.message(Command("stop", "end"), control_limit)
async def cmd_stop(message: Message, player: Player, settings: Settings, bot: Bot) -> None:
    room = await player.stop(message.chat.id, leave=True)
    await rich_reply(
        message,
        cards.error_card("stopped", "the queue is empty and the assistant left the call", icon="stop")
        if room
        else cards.error_card("nothing was playing"),
    )


@router.message(Command("replay"), control_limit)
async def cmd_replay(message: Message, player: Player, settings: Settings) -> None:
    try:
        await player.replay(message.chat.id)
    except RoomError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    room = player.rooms[message.chat.id]
    state = room.state(backend=player.backend_name)
    await rich_reply(
        message, cards.now_playing(state), reply_markup=cards.now_playing_buttons(state)
    )


@router.message(Command("seek"), control_limit)
async def cmd_seek(message: Message, command: CommandObject, player: Player, settings: Settings) -> None:
    seconds = parse_time(command.args or "")
    if seconds is None:
        await rich_reply(message, cards.error_card("give me a time", "/seek 1:23 or /seek 83"))
        return
    try:
        position = await player.seek(message.chat.id, seconds)
    except RoomError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    await rich_reply(
        message,
        ui.panel(f"{ui.e('forward')} jumped to <b>{ui.esc(format_duration(position))}</b>", icon="forward"),
    )


@router.message(Command("seekback", "rewind"), control_limit)
async def cmd_seekback(message: Message, command: CommandObject, player: Player, settings: Settings) -> None:
    step = int(command.args.strip()) if (command.args or "").strip().isdigit() else 10
    try:
        position = await player.seek_back(message.chat.id, step)
    except RoomError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    await rich_reply(
        message,
        ui.panel(f"{ui.e('back')} rewound to <b>{ui.esc(format_duration(position))}</b>", icon="back"),
    )


@router.message(Command("position", "pos"))
async def cmd_position(message: Message, player: Player) -> None:
    room = player.rooms.get(message.chat.id)
    if room is None or room.current is None:
        await rich_reply(message, cards.error_card("nothing is playing"))
        return
    state = room.state(backend=player.backend_name)
    await rich_reply(
        message,
        ui.panel(
            f"{ui.e('music')} <b>{ui.esc(state.title or '')}</b>",
            f"\n{ui.e('clock')} {ui.esc(state.progress_text)}",
            f"<code>{state.bar}</code>",
            icon="clock",
        ),
        reply_markup=cards.now_playing_buttons(state, compact=True),
    )


@router.message(Command("queue", "q"))
async def cmd_queue(message: Message, command: CommandObject, player: Player) -> None:
    room = player.rooms.get(message.chat.id)
    if room is None:
        await rich_reply(message, cards.error_card("no queue here yet", "/play something first"))
        return
    page = int(command.args.strip()) if (command.args or "").strip().isdigit() else 1
    tracks, current, pages = room.queue.page(page)
    state = room.state(backend=player.backend_name)
    await rich_reply(
        message,
        cards.queue_card(state, tracks, current, pages, total=len(room.queue)),
        reply_markup=cards.queue_buttons(state, current, pages, tracks=tracks),
    )


@router.message(Command("active", "activevc", "activevoice"))
async def cmd_active(message: Message, player: Player) -> None:
    rooms = player.active()
    if not rooms:
        await rich_reply(message, cards.error_card("the assistant is not streaming anywhere"))
        return
    rows = [
        (
            ui.esc(str(room.chat_id)),
            ui.esc(str(room.current.display_title if room.current else "-")[:40]),
            ui.esc(format_duration(room.position())),
            str(len(room.queue)),
        )
        for room in rooms
    ]
    await rich_reply(
        message,
        ui.screen(
            "active voice chats",
            icon="mic",
            subtitle=f"{len(rooms)} room(s) · backend {player.backend_name}",
            blocks_=[ui.table(["chat", "playing", "position", "queue"], rows, raw=True)],
        ),
    )


@router.message(Command("voice", "vc"))
async def cmd_voice(message: Message, player: Player, settings: Settings, mtproto: Any = None) -> None:
    """``/voice`` - is audio actually possible, and what is playing where."""
    overview = player.overview()
    note = mtproto.status.describe() if mtproto is not None and hasattr(mtproto, "status") else None
    await rich_reply(message, cards.status_card(overview, _caps(settings), mtproto_note=note))


@router.message(Command("musichelp", "vhelp", "mhelp"))
async def cmd_music_help(message: Message, command: CommandObject, settings: Settings) -> None:
    topic = (command.args or "all").strip().lower()
    await rich_reply(
        message, cards.help_card(topic, capabilities=_caps(settings)), reply_markup=cards.help_buttons()
    )


@router.message(Command("vsettings", "playersettings"))
async def cmd_vsettings(message: Message, db: Database, player: Player) -> None:
    preferences = await player.preferences(message.chat.id)
    await rich_reply(
        message,
        cards.settings_card(preferences, chat_title=_chat_title(message)),
        reply_markup=cards.settings_buttons(preferences),
    )


async def _simple_control(message: Message, player: Player, settings: Settings, action: Any, verb: str) -> None:
    try:
        changed = await action()
    except RoomError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    if not changed:
        await rich_reply(message, cards.error_card(f"already {verb}"))
        return
    room = player.rooms.get(message.chat.id)
    if room is None:
        await rich_reply(message, ui.panel(f"{ui.e('check')} {verb}", icon="check"))
        return
    state = room.state(backend=player.backend_name)
    await rich_reply(
        message, cards.now_playing(state), reply_markup=cards.now_playing_buttons(state, compact=True)
    )


# ---------------------------------------------------------------------------
#  queue management
# ---------------------------------------------------------------------------


@router.message(Command("shuffle"), control_limit)
async def cmd_shuffle(message: Message, player: Player) -> None:
    try:
        count = await player.shuffle(message.chat.id)
    except RoomError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    await rich_reply(
        message,
        ui.panel(f"{ui.e('refresh')} shuffled <b>{count}</b> track(s)", icon="refresh"),
    )


@router.message(Command("jump"), control_limit)
async def cmd_jump(message: Message, command: CommandObject, player: Player) -> None:
    index = int(command.args.strip()) if (command.args or "").strip().isdigit() else 0
    if not index:
        await rich_reply(message, cards.error_card("which one?", "/jump 3 - see /queue for numbers"))
        return
    track = await player.jump(message.chat.id, index)
    if track is None:
        await rich_reply(message, cards.error_card(f"there is no #{index} in the queue"))
        return
    await rich_reply(
        message,
        ui.panel(f"{ui.e('forward')} <b>{ui.esc(track.display_title)}</b> plays next", icon="forward"),
    )


@router.message(Command("remove"), control_limit)
async def cmd_remove(message: Message, command: CommandObject, player: Player) -> None:
    index = int(command.args.strip()) if (command.args or "").strip().isdigit() else 0
    if not index:
        await rich_reply(message, cards.error_card("which one?", "/remove 2 - see /queue for numbers"))
        return
    track = await player.remove(message.chat.id, index)
    if track is None:
        await rich_reply(message, cards.error_card(f"there is no #{index} in the queue"))
        return
    await rich_reply(
        message,
        ui.panel(f"{ui.e('delete')} removed <b>{ui.esc(track.display_title)}</b>", icon="delete"),
    )


@router.message(Command("clear"), control_limit)
async def cmd_clear(message: Message, player: Player) -> None:
    room = player.rooms.get(message.chat.id)
    if room is None:
        await rich_reply(message, cards.error_card("nothing is queued here"))
        return
    removed = room.queue.clear()
    await rich_reply(
        message,
        ui.panel(
            f"{ui.e('delete')} cleared <b>{removed}</b> track(s)",
            "\n<i>the current track keeps playing</i>",
            icon="delete",
        ),
    )


@router.message(Command("loop", "repeat"), control_limit)
async def cmd_loop(message: Message, command: CommandObject, player: Player) -> None:
    argument = (command.args or "").strip().lower()
    try:
        mode = await player.loop(message.chat.id, argument or None)
    except RoomError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    await rich_reply(
        message,
        ui.panel(
            f"{ui.e('loop')} loop is now <b>{ui.esc(mode.label)}</b>",
            "\n/loop cycles: off → one track → whole queue" if not argument else "",
            icon="loop",
        ),
    )


@router.message(Command("autoplay"), control_limit)
async def cmd_autoplay(message: Message, player: Player) -> None:
    try:
        state = await player.autoplay(message.chat.id)
    except RoomError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    await rich_reply(
        message,
        ui.panel(
            f"{ui.e('party')} autoplay is {ui.badge(state)}",
            "\nwhen the queue runs dry, a similar track is found automatically",
            icon="party",
        ),
    )


# ---------------------------------------------------------------------------
#  sound: volume / mute / speed
# ---------------------------------------------------------------------------


@router.message(Command(commands=["vmute", "songmute"]), control_limit)
async def cmd_mute(message: Message, player: Player) -> None:
    await _mute(message, player, True)


@router.message(Command(commands=["vunmute", "songunmute"]), control_limit)
async def cmd_unmute(message: Message, player: Player) -> None:
    await _mute(message, player, False)


async def _mute(message: Message, player: Player, muted: bool) -> None:
    try:
        state = await player.mute(message.chat.id, muted)
    except RoomError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    await rich_reply(
        message,
        ui.panel(
            f"{ui.e('bell') if not state else ui.e('cross')} the assistant is "
            f"<b>{'muted' if state else 'audible'}</b>",
            icon="bell",
        ),
    )


@router.message(Command("volume", "vol"), control_limit)
async def cmd_volume(message: Message, command: CommandObject, player: Player) -> None:
    raw = (command.args or "").strip()
    if not raw:
        room = player.rooms.get(message.chat.id)
        current = room.volume if room else player.default_volume()
        await rich_reply(message, cards.error_card(f"volume is {current}%", "/volume 120 - between 0 and 200"))
        return
    try:
        value = int(raw.replace("%", ""))
    except ValueError:
        await rich_reply(message, cards.error_card("give me a number", "/volume 120"))
        return
    try:
        volume = await player.volume(message.chat.id, value)
    except RoomError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    await rich_reply(
        message,
        ui.panel(f"{ui.e('bell')} volume set to <b>{volume}%</b>", icon="bell"),
        reply_markup=keys.keyboard(
            keys.row(
                keys.button("−10", callback=cards.cb("vol", message.chat.id, -10), style="link", icon="back"),
                keys.button("+10", callback=cards.cb("vol", message.chat.id, 10), style="link", icon="forward"),
            )
        ),
    )


@router.message(Command("speed", "rate"), control_limit)
async def cmd_speed(message: Message, command: CommandObject, player: Player) -> None:
    raw = (command.args or "").strip().rstrip("x×")
    if not raw:
        await rich_reply(message, cards.error_card("give me a rate", "/speed 1.25 - between 0.5 and 2"))
        return
    try:
        rate = float(raw)
    except ValueError:
        await rich_reply(message, cards.error_card("give me a number", "/speed 1.25"))
        return
    try:
        applied = await player.speed(message.chat.id, rate)
    except RoomError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    await rich_reply(message, ui.panel(f"{ui.e('zap')} speed is now <b>{applied:g}×</b>", icon="zap"))


# ---------------------------------------------------------------------------
#  controllers, away mode, playlists
# ---------------------------------------------------------------------------


@router.message(Command("auth", "vauth"))
async def cmd_auth(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if not await _can_control(message.bot, db, settings, message.chat.id, message.from_user.id):
        await _deny(message, settings)
        return
    target = await _target_user(message, command)
    if target is None:
        await rich_reply(message, cards.error_card("who?", "/auth @name - or reply to their message"))
        return
    user_id, name = target
    added = await db.allow_voice(message.chat.id, user_id, added_by=message.from_user.id)
    await rich_reply(
        message,
        ui.panel(
            f"{ui.e('crown')} <b>{ui.esc(name)}</b> {'can' if added else 'could already'} drive the player",
            "\nʀᴇᴠᴏᴋᴇ ᴡɪᴛʜ /ᴜɴᴀᴜᴛʜ",
            icon="crown",
        ),
    )


@router.message(Command("unauth", "vunauth"))
async def cmd_unauth(message: Message, command: CommandObject, db: Database, settings: Settings) -> None:
    if not await _can_control(message.bot, db, settings, message.chat.id, message.from_user.id):
        await _deny(message, settings)
        return
    target = await _target_user(message, command)
    if target is None:
        await rich_reply(message, cards.error_card("who?", "/unauth @name"))
        return
    user_id, name = target
    removed = await db.deny_voice(message.chat.id, user_id)
    await rich_reply(
        message,
        ui.panel(
            f"{ui.e('cross')} <b>{ui.esc(name)}</b> {'can no longer' if removed else 'was not able to'} drive the player",
            icon="cross",
        ),
    )


@router.message(Command("authlist", "controllers"))
async def cmd_authlist(message: Message, db: Database) -> None:
    controllers = await db.voice_allowed(message.chat.id)
    names: dict[int, str] = {}
    for user_id in controllers:
        row = await db.get_user(user_id)
        if row is not None:
            names[user_id] = row.first_name or (f"@{row.username}" if row.username else str(user_id))
    await rich_reply(message, cards.auth_card(controllers, names=names))


async def _target_user(message: Message, command: CommandObject) -> tuple[int, str] | None:
    """A user from ``@name``, a raw id, or the replied-to message."""
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None:
        return reply.from_user.id, reply.from_user.full_name
    raw = (command.args or "").strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return int(raw), raw
    username = raw.lstrip("@")
    if message.bot is None:  # pragma: no cover
        return None
    try:
        chat = await message.bot.get_chat(f"@{username}")
        return chat.id, chat.full_name or username
    except TelegramAPIError:
        return None


@router.message(Command("afk", "away"))
async def cmd_afk(message: Message, command: CommandObject, db: Database) -> None:
    reason = (command.args or "").strip() or None
    await set_away(db, message.from_user.id, message.chat.id, reason)
    await rich_reply(
        message,
        cards.afk_card(reason, user_name=message.from_user.full_name),
        reply_markup=keys.keyboard(
            keys.row(
                keys.button(
                    "ɪ'ᴍ ʙᴀᴄᴋ",
                    callback=cards.cb("afk_off", message.chat.id),
                    style="success",
                    icon="check",
                )
            )
        ),
    )


@router.message(Command("createplaylist", "newplaylist"))
async def cmd_create_playlist(message: Message, command: CommandObject, db: Database) -> None:
    name = (command.args or "").strip()
    if not name:
        await rich_reply(message, cards.error_card("name it", "/createplaylist chill vibes"))
        return
    try:
        info = await _playlists(db).create(message.from_user.id, name)
    except PlaylistError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    await rich_reply(
        message,
        ui.panel(
            f"{ui.e('playlist')} created <b>{ui.esc(info.name)}</b> (id <code>{info.id}</code>)",
            "\nᴀᴅᴅ ᴛʀᴀᴄᴋs: /ᴀᴅᴅᴛᴏᴘʟᴀʏʟɪꜱᴛ <ɴᴀᴍᴇ> <ꜱᴏɴɢ ʟɪɴᴋ>",
            icon="playlist",
        ),
    )


@router.message(Command("addtoplaylist", "playlistadd"))
async def cmd_add_to_playlist(
    message: Message, command: CommandObject, db: Database, settings: Settings
) -> None:
    argument = (command.args or "").strip()
    if not argument:
        await rich_reply(message, cards.error_card("usage", "/addtoplaylist <name> <song or link>"))
        return
    parts = argument.split(maxsplit=1)
    if len(parts) < 2:
        await rich_reply(message, cards.error_card("what should I add?", "/addtoplaylist <name> <song>"))
        return
    name, target = parts[0], parts[1]
    service = _playlists(db)
    try:
        info = await service.find(message.from_user.id, name)
    except PlaylistError as exc:
        await rich_reply(message, cards.error_card(str(exc), "create it with /createplaylist " + name))
        return
    try:
        if _looks_like_playlist(target):
            tracks = await expand(target, settings=settings, limit=100)
        else:
            tracks = [await resolve(target, settings=settings)]
    except SourceError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    try:
        info, added = await service.add_tracks(message.from_user.id, name, tracks)
    except PlaylistError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    await rich_reply(
        message,
        ui.panel(
            f"{ui.e('add')} added <b>{added}</b> track(s) to <b>{ui.esc(info.name)}</b>",
            f"\n{ui.e('list')} {info.tracks} track(s) now",
            icon="add",
        ),
    )


@router.message(Command("removefromplaylist", "playlistremove"))
async def cmd_remove_from_playlist(message: Message, command: CommandObject, db: Database) -> None:
    argument = (command.args or "").strip()
    if not argument:
        await rich_reply(message, cards.error_card("usage", "/removefromplaylist <name> <number>"))
        return
    parts = argument.rsplit(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await rich_reply(message, cards.error_card("which entry?", "/removefromplaylist <name> 3"))
        return
    name, position = parts[0], int(parts[1])
    try:
        info, track = await _playlists(db).remove_track(message.from_user.id, name, position)
    except PlaylistError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    await rich_reply(
        message,
        ui.panel(
            f"{ui.e('delete')} removed <b>{ui.esc(track.display_title)}</b> from <b>{ui.esc(info.name)}</b>",
            f"\n{ui.e('list')} {info.tracks} left",
            icon="delete",
        ),
    )


@router.message(Command("deleteplaylist", "delplaylist"))
async def cmd_delete_playlist(message: Message, command: CommandObject, db: Database) -> None:
    name = (command.args or "").strip()
    if not name:
        await rich_reply(message, cards.error_card("which one?", "/myplaylists"))
        return
    try:
        info = await _playlists(db).delete(message.from_user.id, name)
    except PlaylistError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    await rich_reply(
        message,
        ui.panel(f"{ui.e('delete')} deleted <b>{ui.esc(info.name)}</b> ({info.tracks} tracks)", icon="delete"),
    )


@router.message(Command("myplaylists", "playlists"))
async def cmd_my_playlists(message: Message, db: Database) -> None:
    infos = await _playlists(db).listing(message.from_user.id)
    await rich_reply(message, cards.playlists_card(infos, owner=message.from_user.full_name))


@router.message(Command("playlistinfo", "showplaylist"))
async def cmd_playlist_info(message: Message, command: CommandObject, db: Database) -> None:
    name = (command.args or "").strip()
    if not name:
        await rich_reply(message, cards.error_card("which one?", "/myplaylists"))
        return
    service = _playlists(db)
    try:
        info = await service.find(message.from_user.id, name)
    except PlaylistError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    tracks = await service.tracks(info.id, limit=10)
    pages = max(1, (info.tracks + 9) // 10)
    await rich_reply(
        message,
        cards.playlist_card(info, tracks, 1, pages),
        reply_markup=cards.playlist_buttons(info, 1, pages, chat_id=message.chat.id, tracks=tracks),
    )


@router.message(Command("playplaylist", "playpl"))
async def cmd_play_playlist(message: Message, command: CommandObject, db: Database, player: Player) -> None:
    name = (command.args or "").strip()
    if not name:
        await rich_reply(message, cards.error_card("which one?", "/myplaylists"))
        return
    service = _playlists(db)
    try:
        info = await service.find(message.from_user.id, name)
        tracks = await service.tracks(info.id)
    except PlaylistError as exc:
        await rich_reply(message, cards.error_card(str(exc)))
        return
    if not tracks:
        await rich_reply(message, cards.error_card(f"{info.name!r} is empty"))
        return
    if message.from_user:
        for track in tracks:
            track.requested_by = message.from_user.id
            track.requested_name = message.from_user.full_name
    room, added = await player.enqueue_many(message.chat.id, tracks)
    state = room.state(backend=player.backend_name)
    await rich_reply(
        message,
        "\n\n".join(
            [
                ui.panel(f"{ui.e('playlist')} queued <b>{added}</b> track(s) from <b>{ui.esc(info.name)}</b>", icon="playlist"),
                cards.now_playing(state),
            ]
        ),
        reply_markup=cards.now_playing_buttons(state),
    )


@router.message(Command("panel"))
async def cmd_panel(message: Message, player: Player, settings: Settings) -> None:
    room = player.rooms.get(message.chat.id)
    if room is None or room.current is None:
        await rich_reply(message, cards.error_card("nothing is playing", "/play something"))
        return
    state = room.state(backend=player.backend_name)
    await _post_panel(
        message,
        {"bot": message.bot, "settings": settings, "player": player},
        cards.now_playing(state),
        cards.now_playing_buttons(state),
        ephemeral_for=message.from_user.id if message.from_user else None,
    )


# ---------------------------------------------------------------------------
#  callbacks
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("v:"))
async def on_voice_button(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    bot: Bot,
    player: Player,
) -> None:
    action, chat_id, value = cards.parse_cb(callback.data or "")
    if not action:
        await callback.answer()
        return
    chat_id = chat_id or (callback.message.chat.id if callback.message else None)
    if chat_id is None:
        await callback.answer("this button is stale", show_alert=False)
        return
    user_id = callback.from_user.id

    # everyone may look, only controllers may touch
    read_only = {"queue", "q", "help", "panel", "close", "afk_off", "pl"}
    if action not in read_only and not await _can_control(bot, db, settings, chat_id, user_id):
        await callback.answer("only admins drive the player here", show_alert=True)
        return

    try:
        result = await _dispatch_action(action, value, chat_id, user_id, callback, db, settings, player)
    except RoomError as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    except PlaylistError as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    except SourceError as exc:
        await callback.answer(str(exc)[:180], show_alert=True)
        return
    except Exception as exc:  # pragma: no cover - never leave a spinner hanging
        log.warning("voice callback %r failed: %s", action, exc)
        await callback.answer("something went wrong", show_alert=True)
        return

    if result:
        await callback.answer(result[:180])


async def _dispatch_action(
    action: str,
    value: str | None,
    chat_id: int,
    user_id: int,
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    player: Player,
) -> str | None:
    """Apply one button press; returns the toast text (or ``None``)."""
    bot = callback.bot

    # -- panels --------------------------------------------------------------
    if action == "close":
        await _close_panel(callback, bot, chat_id, user_id)
        return None

    if action == "panel":
        room = player.rooms.get(chat_id)
        if room is None or room.current is None:
            return "nothing is playing"
        state = room.state(backend=player.backend_name)
        await _replace_panel(callback, state, db, settings, player)
        return None

    if action == "queue":
        room = player.rooms.get(chat_id)
        if room is None:
            return "the queue is empty"
        page = int(value) if (value or "").isdigit() else 1
        tracks, current, pages = room.queue.page(page)
        state = room.state(backend=player.backend_name)
        await _replace_panel(
            callback,
            None,
            db,
            settings,
            player,
            html=cards.queue_card(state, tracks, current, pages, total=len(room.queue)),
            markup=cards.queue_buttons(state, current, pages, tracks=tracks),
        )
        return None

    if action == "q":
        room = player.rooms.get(chat_id)
        if room is None:
            return "the queue is empty"
        page = int(value or 1)
        tracks, current, pages = room.queue.page(page)
        state = room.state(backend=player.backend_name)
        await _replace_panel(
            callback,
            None,
            db,
            settings,
            player,
            html=cards.queue_card(state, tracks, current, pages, total=len(room.queue)),
            markup=cards.queue_buttons(state, current, pages, tracks=tracks),
        )
        return None

    if action == "help":
        topic = value or "all"
        await _replace_panel(
            callback,
            None,
            db,
            settings,
            player,
            html=cards.help_card(topic, capabilities=_caps(settings)),
            markup=cards.help_buttons(),
        )
        return None

    # -- playback ------------------------------------------------------------
    if action == "toggle":
        playing = await player.toggle(chat_id)
        await _refresh_state(callback, player, chat_id, db, settings)
        return "playing" if playing else "paused"

    if action == "skip":
        skipped = await player.skip(chat_id)
        await _refresh_state(callback, player, chat_id, db, settings)
        return f"skipped {skipped.display_title[:40]}" if skipped else "queue finished"

    if action == "stop":
        await player.stop(chat_id, leave=True)
        await _replace_panel(
            callback,
            None,
            db,
            settings,
            player,
            html=cards.error_card("stopped", "the assistant left the call", icon="stop"),
            markup=cards.close_button(chat_id=chat_id),
        )
        return "stopped"

    if action == "replay":
        await player.replay(chat_id)
        await _refresh_state(callback, player, chat_id, db, settings)
        return "from the top"

    if action == "seek":
        step = int(value) if (value or "").lstrip("-").isdigit() else 10
        position = await player.seek(chat_id, int(player.rooms[chat_id].position()) + step)
        await _refresh_state(callback, player, chat_id, db, settings)
        return f"{format_duration(position)}"

    if action == "loop":
        mode = await player.loop(chat_id)
        await _refresh_state(callback, player, chat_id, db, settings)
        return f"loop: {mode.label}"

    if action == "shuffle":
        count = await player.shuffle(chat_id)
        return f"shuffled {count}"

    if action == "autoplay":
        state = await player.autoplay(chat_id)
        await _refresh_state(callback, player, chat_id, db, settings)
        return f"autoplay {'on' if state else 'off'}"

    if action == "mute":
        muted = await player.mute(chat_id, not player.rooms[chat_id].muted)
        await _refresh_state(callback, player, chat_id, db, settings)
        return "muted" if muted else "unmuted"

    if action == "clear":
        room = player.rooms.get(chat_id)
        if room is None:
            return "nothing to clear"
        removed = room.queue.clear()
        return f"cleared {removed}"

    if action == "jump":
        index = int(value) if (value or "").isdigit() else 0
        track = await player.jump(chat_id, index)
        return f"{track.display_title[:40]} plays next" if track else f"no #{index} in the queue"

    if action == "vol":
        room = player.rooms.get(chat_id)
        if room is None:
            return "nothing is playing"
        delta = int(value) if (value or "").lstrip("-").isdigit() else 10
        volume = await player.volume(chat_id, room.volume + delta)
        await _refresh_state(callback, player, chat_id, db, settings)
        return f"volume {volume}%"

    if action == "toggle_pref":
        key = value or "autoplay"
        preferences = await player.preferences(chat_id)
        new_value = not bool(preferences.get(key))
        if key == "autoleave":
            new_value = 0 if preferences.get("autoleave") else player.default_autoleave()
        await player.set_preference(chat_id, key, new_value)
        preferences = await player.preferences(chat_id)
        await _replace_panel(
            callback,
            None,
            db,
            settings,
            player,
            html=cards.settings_card(preferences, chat_title=str(chat_id)),
            markup=cards.settings_buttons(preferences),
        )
        return f"{key} updated"

    if action == "pick":
        index = int(value) if (value or "").isdigit() else 0
        tracks = _last_search(chat_id)
        if not tracks or not 1 <= index <= len(tracks):
            return "that search expired - run /play again"
        track = tracks[index - 1]
        track.requested_by = user_id
        track.requested_name = callback.from_user.full_name
        room, _ = await player.enqueue(chat_id, track)
        state = room.state(backend=player.backend_name)
        await _replace_panel(
            callback,
            state,
            db,
            settings,
            player,
            html=cards.now_playing(state),
            markup=cards.now_playing_buttons(state),
        )
        return f"queued {track.display_title[:40]}"

    if action == "afk_off":
        removed = await db.clear_afk(user_id, chat_id)
        await _replace_panel(
            callback,
            None,
            db,
            settings,
            player,
            html=cards.afk_leave_card() if removed else cards.error_card("you were not away"),
            markup=cards.close_button(chat_id=chat_id),
        )
        return "welcome back" if removed else None

    # -- playlists -----------------------------------------------------------
    if action in ("pl", "pplay", "pdel", "ptrack"):
        playlist_id, _, page = (value or "").partition(":")
        if not playlist_id.isdigit():
            return "that playlist is gone"
        service = _playlists(db)
        info = await service.info(int(playlist_id))
        if info is None:
            return "that playlist is gone"

        if action == "pplay":
            tracks = await service.tracks(info.id)
            if not tracks:
                return "that playlist is empty"
            for track in tracks:
                track.requested_by = user_id
                track.requested_name = callback.from_user.full_name
            room, added = await player.enqueue_many(chat_id, tracks)
            state = room.state(backend=player.backend_name)
            await _replace_panel(
                callback,
                state,
                db,
                settings,
                player,
                html=cards.now_playing(state),
                markup=cards.now_playing_buttons(state),
            )
            return f"queued {added} track(s)"

        if action == "pdel":
            await service.delete(info.owner_id, str(info.id))
            await _replace_panel(
                callback,
                None,
                db,
                settings,
                player,
                html=cards.error_card(f"deleted {info.name}", icon="delete"),
                markup=cards.close_button(chat_id=chat_id),
            )
            return "deleted"

        if action == "ptrack":
            position = int(page or 0)
            tracks = await service.tracks(info.id)
            if not 1 <= position <= len(tracks):
                return "that entry is gone"
            track = tracks[position - 1]
            track.requested_by = user_id
            track.requested_name = callback.from_user.full_name
            room, _ = await player.enqueue(chat_id, track)
            state = room.state(backend=player.backend_name)
            await _replace_panel(
                callback,
                state,
                db,
                settings,
                player,
                html=cards.now_playing(state),
                markup=cards.now_playing_buttons(state),
            )
            return f"queued {track.display_title[:40]}"

        # action == "pl": flip a page
        tracks = await service.tracks(info.id, limit=200)
        pages = max(1, (len(tracks) + 9) // 10)
        current = max(1, min(int(page or 1), pages))
        window = tracks[(current - 1) * 10 : current * 10]
        await _replace_panel(
            callback,
            None,
            db,
            settings,
            player,
            html=cards.playlist_card(info, window, current, pages),
            markup=cards.playlist_buttons(info, current, pages, chat_id=chat_id, tracks=window),
        )
        return None

    return None


async def _refresh_state(
    callback: CallbackQuery,
    player: Player,
    chat_id: int,
    db: Database,
    settings: Settings,
) -> None:
    room = player.rooms.get(chat_id)
    if room is None:
        return
    state = room.state(backend=player.backend_name)
    await _replace_panel(
        callback, state, db, settings, player, html=cards.now_playing(state), markup=cards.now_playing_buttons(state)
    )


async def _replace_panel(
    callback: CallbackQuery,
    state: Any,
    db: Database,
    settings: Settings,
    player: Player,
    *,
    html: str | None = None,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Swap the panel that carried the button for fresh content.

    Ephemeral panels are replaced through the Bot API's
    ``replace_callback_query_message``; older messages are edited in place.
    """
    bot = callback.bot
    message = callback.message
    if message is None:
        return
    chat_id = message.chat.id
    body = html or cards.now_playing(state)
    if markup is None and state is not None:
        markup = cards.now_playing_buttons(state)

    # 1. ephemeral message: replace it for this user only
    try:
        sent = await ephemeral_send(
            bot,
            chat_id,
            body,
            receiver_user_id=callback.from_user.id,
            callback_query_id=callback.id,
            replace_callback_query=True,
            reply_markup=markup,
        )
        if sent is not None:
            return
    except TelegramAPIError as exc:
        log.debug("replace_callback_query_message unsupported: %s", exc)

    # 2. a normal message: edit it
    try:
        await rich_edit(bot, chat_id=chat_id, message_id=message.message_id, html=body, reply_markup=markup)
        room = player.rooms.get(chat_id)
        if room is not None:
            room.panel = (chat_id, message.message_id)
    except TelegramAPIError as exc:  # pragma: no cover
        log.debug("panel edit failed: %s", exc)


async def _close_panel(callback: CallbackQuery, bot: Bot, chat_id: int, user_id: int) -> None:
    """Dismiss a panel: ephemeral messages go away entirely, others get edited."""
    message = callback.message
    if message is None:
        return
    try:
        deleted = await bot.delete_ephemeral_message(
            chat_id=chat_id, receiver_user_id=user_id, ephemeral_message_id=message.message_id
        )
        if deleted:
            return
    except TelegramAPIError:
        pass
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except TelegramAPIError:
        try:
            await rich_edit(
                bot,
                chat_id=chat_id,
                message_id=message.message_id,
                html=cards.error_card("panel closed", "/panel brings it back", icon="check"),
                reply_markup=None,
            )
        except TelegramAPIError:  # pragma: no cover
            pass
