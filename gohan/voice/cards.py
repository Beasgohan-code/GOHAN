"""Every screen of the music part, as rich messages.

The UI rules of the project apply here like everywhere else: small caps
headings, blockquote panels, real tables, custom emoji - and now the newest Bot
API pieces, which turn out to be exactly what a player needs:

* ``<tg-time unix=… format=relative>`` renders a live "in 3 minutes" clock, so
  the now-playing card never shows a stale "ends at".
* ``<img src=…>`` puts the cover art inside the message.
* ``<tg-button type="callback_data" style="…" data=…>`` gives the rich body its
  own coloured buttons, while a real :class:`InlineKeyboardMarkup` (with
  ``style`` + ``icon_custom_emoji_id``) covers every client.
* the control panel can be posted **ephemerally** (see ``handlers.py``), so the
  buttons never clutter the group.

Nothing here talks to Telegram: the functions are pure ``str``/markup builders,
which is why they are exercised directly by the tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from ..rich import keys
from ..rich import ui
from .models import LoopMode, PlaybackState, Track, format_duration, progress_bar, source_label
from .playlists import PlaylistInfo
from .queue import PAGE_SIZE

__all__ = [
    "afk_card",
    "afk_leave_card",
    "afk_ping_card",
    "auth_card",
    "error_card",
    "help_card",
    "loading_card",
    "now_playing",
    "now_playing_buttons",
    "playlist_buttons",
    "playlist_card",
    "playlists_card",
    "queue_buttons",
    "queue_card",
    "search_buttons",
    "search_card",
    "settings_buttons",
    "settings_card",
    "status_card",
]

#: callback-data prefix for every voice button in the bot
CB = "v"


def cb(action: str, chat_id: int | None = None, value: Any = None) -> str:
    """``v:skip:-100123`` - kept short, Telegram allows 64 bytes."""
    parts = [CB, action]
    if chat_id is not None or value is None:
        if chat_id is not None:
            parts.append(str(chat_id))
    elif value is not None:
        # keep the chat slot empty so the value cannot be read back as a chat id
        parts.append("")
    if value is not None:
        parts.append(str(value))
    return ":".join(parts)


def parse_cb(data: str) -> tuple[str, int | None, str | None]:
    """The inverse of :func:`cb`; tolerant, never raises."""
    parts = str(data or "").split(":")
    if len(parts) < 2 or parts[0] != CB:
        return "", None, None
    action = parts[1]
    chat_id: int | None = None
    value: str | None = None
    if len(parts) > 2 and parts[2]:
        try:
            chat_id = int(parts[2])
        except ValueError:
            value = parts[2]
    if len(parts) > 3:
        value = ":".join(parts[3:])
    return action, chat_id, value


def _time_tag(unix: float, fmt: str = "relative") -> str:
    return f'<tg-time unix="{int(unix)}" format="{fmt}">soon</tg-time>'


def _requester(state: PlaybackState) -> str:
    if state.requested_by:
        return ui.mention(ui.esc(state.requested_name or "someone"), state.requested_by)
    return ui.esc(state.requested_name or "the queue")


def _stars(state: PlaybackState) -> str:
    """A five-dot health line: is the assistant connected, muted, looping…"""
    marks = [
        ui.e("check") + " connected" if state.connected else ui.e("cross") + " waiting for assistant",
        ui.e("pause") + " paused" if state.paused else ui.e("play") + " playing",
    ]
    if state.muted:
        marks.append(ui.e("cross") + " muted")
    if state.speed != 1.0:
        marks.append(f"{state.speed:g}× speed")
    if state.loop is not LoopMode.OFF:
        marks.append(f"loop: {ui.esc(state.loop.label)}")
    if state.autoplay:
        marks.append(ui.e("refresh") + " autoplay")
    return " · ".join(marks)


# ---------------------------------------------------------------------------
#  now playing
# ---------------------------------------------------------------------------


def now_playing(state: PlaybackState, *, room_extra: dict[str, Any] | None = None) -> str:
    """The main card: cover, progress, a live end time and the facts table."""
    title = ui.esc(state.title or "nothing is playing")
    heading = ui.h("now playing", level=2, icon="music")
    if state.title is None:
        return ui.screen(
            "now playing",
            icon="music",
            subtitle="the queue is empty",
            blocks_=[ui.panel(f"{ui.e('note')} add something with /play <name or link>")],
        )

    lines: list[str] = [f"{ui.e('play')} <b>{title}</b>"]
    if state.uploader:
        lines.append(f"{ui.muted('by ' + ui.esc(state.uploader))}")
    lines.append(f"{ui.e('clock')} {ui.esc(state.progress_text)}" + (f" · {_time_tag(state.ends_at)}" if state.ends_at else ""))
    lines.append(f"<code>{progress_bar(state.position, state.duration, width=18)}</code>")
    panel = ui.panel(*lines, icon="music")

    rows: list[tuple[str, str]] = [
        ("source", ui.esc(source_label(state.source))),
        ("requested by", _requester(state)),
        ("queue", f"{state.queue_length} track(s) · {ui.esc(format_duration(state.queue_duration))}"),
        ("volume", f"{state.volume}%"),
    ]
    if state.loop is not LoopMode.OFF:
        rows.append(("loop", ui.esc(state.loop.label)))
    if state.speed != 1.0:
        rows.append(("speed", f"{state.speed:g}×"))
    table = ui.table(["field", "value"], rows, raw=True)

    blocks = [heading, panel, table]
    if state.thumbnail:
        blocks.insert(2, f'<img src="{ui.esc(state.thumbnail)}"/>')
    blocks.append(ui.footer_text(_stars(state), caps_text=False))
    return "\n\n".join(blocks)


def now_playing_buttons(state: PlaybackState, *, compact: bool = False) -> Any:
    """Coloured controls with custom-emoji glyphs.

    ``compact`` is the single-row variant used next to a group's control panel.
    """
    chat_id = state.chat_id
    if compact:
        return keys.keyboard(
            keys.row(
                keys.button("ˢᵏⁱᵖ", callback=cb("skip", chat_id), style="primary", icon="forward"),
                keys.button(
                    "ʀᴇˢᵘᴍᴇ" if state.paused else "ᴘᴀᴜˢᴇ",
                    callback=cb("toggle", chat_id),
                    style="success" if state.paused else "primary",
                    icon="play" if state.paused else "pause",
                ),
                keys.button("ˢᵗᵒᴘ", callback=cb("stop", chat_id), style="danger", icon="stop"),
            )
        )
    top = keys.row(
        keys.button(
            "ʀᴇˢᵘᴍᴇ" if state.paused else "ᴘᴀᴜˢᴇ",
            callback=cb("toggle", chat_id),
            style="success" if state.paused else "primary",
            icon="play" if state.paused else "pause",
        ),
        keys.button("ˢᵏⁱᵖ", callback=cb("skip", chat_id), style="primary", icon="forward"),
        keys.button("ʀᴇᴘʟᴀʏ", callback=cb("replay", chat_id), style="primary", icon="refresh"),
        keys.button("ˢᵗᵒᴘ", callback=cb("stop", chat_id), style="danger", icon="stop"),
    )
    seek = keys.row(
        keys.button("−30ѕ", callback=cb("seek", chat_id, -30), style="link", icon="back"),
        keys.button("+30ѕ", callback=cb("seek", chat_id, 30), style="link", icon="forward"),
        keys.button("ᴍᴜᴛᴇ" if not state.muted else "ᴜɴᴍᴜᴛᴇ", callback=cb("mute", chat_id), style="link", icon="bell"),
        keys.button(f"{state.loop.icon} {state.loop.value}", callback=cb("loop", chat_id), style="link", icon="loop"),
    )
    extras = keys.row(
        keys.button("ǫᴜᴇᴜᴇ", callback=cb("queue", chat_id), style="link", icon="queue"),
        keys.button("ˢʰᵘᶠᶠʟᴇ", callback=cb("shuffle", chat_id), style="link", icon="refresh"),
        keys.button("ᴀᴜᴛᴏᴘʟᴀʏ", callback=cb("autoplay", chat_id), style="link", icon="party"),
        keys.button("ᴄʟᴏˢᴇ", callback=cb("close", chat_id), style="danger", icon="cross"),
    )
    return keys.keyboard(top, seek, extras)


# ---------------------------------------------------------------------------
#  queue / search / playlists
# ---------------------------------------------------------------------------


def queue_card(state: PlaybackState, tracks: Sequence[Track], page: int, pages: int, *, total: int) -> str:
    heading = ui.h("queue", level=2, icon="queue")
    summary = ui.panel(
        f"{ui.e('play')} <b>{ui.esc(state.title or 'nothing')}</b>"
        f"\n{ui.muted(ui.esc(state.progress_text))}",
        f"\n{ui.e('list')} {total} track(s) waiting · {ui.esc(format_duration(state.queue_duration))}",
        f"{ui.e('loop')} loop: {ui.esc(state.loop.label)}",
        icon="queue",
    )
    if not tracks:
        return "\n\n".join([heading, summary, ui.panel(f"{ui.e('note')} nothing is queued yet - /play something")])
    rows = [
        (
            str((page - 1) * PAGE_SIZE + index),
            ui.esc(track.display_title[:48]),
            ui.esc(track.duration_text),
            ui.esc(track.requester[:18]),
        )
        for index, track in enumerate(tracks, start=1)
    ]
    table = ui.table(["#", "title", "time", "asked by"], rows, raw=True)
    return "\n\n".join([heading, summary, table, ui.footer_text(f"page {page}/{pages} · ᴛᴀᴘ ᴀ ɴᴜᴍʙᴇʀ ᴛᴏ ᴊᴜᴍᴘ")])


def queue_buttons(state: PlaybackState, page: int, pages: int, *, tracks: Sequence[Track] = ()) -> Any:
    chat_id = state.chat_id
    rows: list[list[Any]] = []
    numbers = keys.row(
        *[
            keys.button(
                str((page - 1) * PAGE_SIZE + index),
                callback=cb("jump", chat_id, (page - 1) * PAGE_SIZE + index),
                style="link",
            )
            for index in range(1, len(tracks) + 1)
        ]
    ) if tracks else []
    if numbers:
        rows.append(numbers)
    navigation = []
    if page > 1:
        navigation.append(keys.button("‹ ᴘʀᴇᴠ", callback=cb("q", chat_id, page - 1), style="primary", icon="back"))
    navigation.append(keys.button("ʀᴇғʀᴇˢʰ", callback=cb("q", chat_id, page), style="primary", icon="refresh"))
    if page < pages:
        navigation.append(keys.button("ɴᴇxᴛ ›", callback=cb("q", chat_id, page + 1), style="primary", icon="forward"))
    rows.append(navigation)
    rows.append(
        keys.row(
            keys.button("ˢʰᵘᶠᶠʟᴇ", callback=cb("shuffle", chat_id), style="link", icon="refresh"),
            keys.button("ᴄʟᴇᴀʀ", callback=cb("clear", chat_id), style="danger", icon="delete"),
            keys.button("ᴄʟᴏˢᴇ", callback=cb("close", chat_id), style="danger", icon="cross"),
        )
    )
    return keys.keyboard(*rows)


def search_card(query: str, tracks: Sequence[Track], *, note: str | None = None) -> str:
    heading = ui.h("search results", level=2, icon="search")
    panel = ui.panel(
        f"{ui.e('search')} {ui.esc(query)}",
        f"\n{ui.e('note')} {len(tracks)} match(es) - ᴛᴀᴘ ᴏɴᴇ ᴛᴏ ǫᴜᴇᴜᴇ ɪᴛ",
        icon="search",
    )
    if not tracks:
        return "\n\n".join([heading, panel, ui.panel(f"{ui.e('cross')} nothing found")])
    rows = [
        (
            str(index),
            ui.esc(track.display_title[:52]),
            ui.esc(track.duration_text),
            ui.esc((track.uploader or "?")[:16]),
        )
        for index, track in enumerate(tracks, start=1)
    ]
    blocks = [heading, panel, ui.table(["#", "title", "time", "channel"], rows, raw=True)]
    if note:
        blocks.append(ui.footer_text(note))
    return "\n\n".join(blocks)


def search_buttons(tracks: Sequence[Track], chat_id: int) -> Any:
    rows = [
        keys.row(
            *[
                keys.button(
                    f"{index} · {track.display_title[:26]}",
                    callback=cb("pick", chat_id, index),
                    style="primary" if index == 1 else "link",
                    icon="play" if index == 1 else None,
                )
            ]
        )
        for index, track in enumerate(tracks[:5], start=1)
    ]
    rows.append(keys.row(keys.button("ᴄʟᴏˢᴇ", callback=cb("close", chat_id), style="danger", icon="cross")))
    return keys.keyboard(*rows)


def playlist_card(info: PlaylistInfo, tracks: Sequence[Track], page: int, pages: int) -> str:
    heading = ui.h("playlist", level=2, icon="playlist")
    panel = ui.panel(
        f"{ui.e('note')} <b>{ui.esc(info.name)}</b>",
        f"\n{ui.e('list')} {info.tracks} track(s) · {ui.esc(info.duration_text)}",
        f"{ui.e('user')} owner: {ui.esc(info.owner_name or info.owner_id)} · id <code>{info.id}</code>",
        icon="playlist",
    )
    if not tracks:
        return "\n\n".join([heading, panel, ui.panel(f"{ui.e('note')} empty - /addtoplaylist <name> <song>")])
    rows = [
        (
            str((page - 1) * PAGE_SIZE + index),
            ui.esc(track.display_title[:48]),
            ui.esc(track.duration_text),
        )
        for index, track in enumerate(tracks, start=1)
    ]
    return "\n\n".join(
        [heading, panel, ui.table(["#", "title", "time"], rows, raw=True), ui.footer_text(f"page {page}/{pages}")]
    )


def playlist_buttons(
    info: PlaylistInfo,
    page: int,
    pages: int,
    *,
    chat_id: int = 0,
    tracks: Sequence[Track] = (),
) -> Any:
    """Buttons for one playlist.

    ``chat_id`` is the room the card lives in - every callback carries it, so a
    button pressed in one group can never act on another.
    """
    rows: list[list[Any]] = []
    if tracks:
        rows.append(
            keys.row(
                *[
                    keys.button(
                        str((page - 1) * PAGE_SIZE + index),
                        callback=cb("ptrack", chat_id, f"{info.id}:{(page - 1) * PAGE_SIZE + index}"),
                        style="link",
                    )
                    for index in range(1, len(tracks) + 1)
                ]
            )
        )
    navigation = []
    if page > 1:
        navigation.append(
            keys.button("‹ ᴘʀᴇᴠ", callback=cb("pl", chat_id, f"{info.id}:{page - 1}"), style="primary", icon="back")
        )
    if page < pages:
        navigation.append(
            keys.button("ɴᴇxᴛ ›", callback=cb("pl", chat_id, f"{info.id}:{page + 1}"), style="primary", icon="forward")
        )
    if navigation:
        rows.append(navigation)
    rows.append(
        keys.row(
            keys.button("ᴘʟᴀʏ ᴀʟʟ", callback=cb("pplay", chat_id, str(info.id)), style="success", icon="play"),
            keys.button("ᴅᴇʟᴇᴛᴇ", callback=cb("pdel", chat_id, str(info.id)), style="danger", icon="delete"),
            keys.button("ᴄʟᴏˢᴇ", callback=cb("close", chat_id), style="danger", icon="cross"),
        )
    )
    return keys.keyboard(*rows)


def playlists_card(infos: Sequence[PlaylistInfo], *, owner: str = "you") -> str:
    heading = ui.h("playlists", level=2, icon="playlist")
    if not infos:
        return "\n\n".join(
            [
                heading,
                ui.panel(f"{ui.e('note')} no playlists yet", "\nᴄʀᴇᴀᴛᴇ ᴏɴᴇ ᴡɪᴛʜ /createplaylist <name>", icon="playlist"),
            ]
        )
    rows = [
        (str(info.id), ui.esc(info.name[:28]), str(info.tracks), ui.esc(info.duration_text))
        for info in infos
    ]
    table = ui.table(["id", "name", "tracks", "length"], rows, raw=True)
    return "\n\n".join(
        [heading, ui.panel(f"{ui.e('user')} {ui.esc(owner)} · {len(infos)} playlist(s)", icon="playlist"), table]
    )


# ---------------------------------------------------------------------------
#  status / help / settings
# ---------------------------------------------------------------------------


def status_card(overview: dict[str, Any], capabilities: Any, *, mtproto_note: str | None = None) -> str:
    backend = overview.get("backend") or {}
    heading = ui.h("voice status", level=2, icon="mic")
    ready = bool(backend.get("available"))
    panel = ui.panel(
        f"{ui.e('check') if ready else ui.e('cross')} backend: <code>{ui.esc(str(backend.get('name')))}</code>",
        f"\n{ui.e('music')} playing: <b>{overview.get('playing', 0)}</b> · queued: {overview.get('queued', 0)}",
        f"{ui.e('search')} yt-dlp: {ui.badge(capabilities.yt_dlp, yes='ready', no_='missing')}"
        f" · ffmpeg: {ui.badge(capabilities.ffmpeg)}"
        f" · cookies: {ui.badge(capabilities.cookies, yes='yes', no_='no')}",
        icon="mic",
    )
    blocks = [heading, panel]
    if not ready:
        steps = [
            "install the voice extra: <code>pip install 'gohan[voice]'</code>",
            "set <code>MTPROTO_MODE=user</code> and log the assistant in once",
            "or keep it offline - the queue and the panel still work",
        ]
        blocks.append(ui.details("ʜᴏᴡ ᴛᴏ ᴇɴᴀʙʟᴇ ᴀᴜᴅɪᴏ", ui.list_ordered(steps), open=True, icon="note"))
    if backend.get("warnings"):
        blocks.append(ui.panel(f"{ui.e('warn')} {ui.esc(str(backend['warnings'][-1])[:200])}", icon="warn"))
    if mtproto_note:
        blocks.append(ui.panel(f"{ui.e('note')} {ui.esc(mtproto_note)}", icon="note"))
    active = overview.get("rooms") or []
    if active:
        rows = [
            (
                ui.esc(str(room["state"].get("chat_id"))),
                ui.esc(str(room["state"].get("title") or "-")[:36]),
                ui.esc(str(room["state"].get("progress_text") or "-")),
                str(room["queue"].get("size", 0)),
            )
            for room in active
        ]
        blocks.append(ui.table(["chat", "playing", "position", "queue"], rows, raw=True))
    blocks.append(ui.footer_text(capabilities.note, caps_text=False))
    return "\n\n".join(blocks)


HELP_SECTIONS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "play": (
        "playback",
        [
            ("/play <song or link>", "search and queue - links, playlists and Spotify work too"),
            ("/playlist <url>", "queue an entire playlist or album"),
            ("/pause · /resume", "hold and continue"),
            ("/skip [n] · /replay", "next track, or start this one again"),
            ("/seek <0:42> · /seekback [30]", "jump inside the track"),
            ("/position", "where the needle is"),
            ("/stop", "stop everything and leave the call"),
        ],
    ),
    "queue": (
        "the queue",
        [
            ("/queue [page]", "what is coming up"),
            ("/shuffle", "shuffle what is waiting"),
            ("/jump <n> · /remove <n>", "play a queue entry next, or drop it"),
            ("/clear", "empty the queue but keep playing"),
            ("/loop [off|one|queue]", "repeat nothing, this track, or everything"),
            ("/autoplay", "keep the music going with similar tracks"),
            ("/active", "every chat the assistant is streaming to"),
        ],
    ),
    "sound": (
        "sound",
        [
            ("/volume <0-200>", "set the assistant volume"),
            ("/vmute · /vunmute", "silence without stopping · /mute still warns members"),
            ("/speed <0.5-2>", "slow down or speed up"),
            ("/vsettings", "per-chat defaults (autoplay, autoleave, max length)"),
            ("/auth <user> · /unauth <user>", "let someone else control the player"),
        ],
    ),
    "playlists": (
        "playlists & extras",
        [
            ("/createplaylist <name>", "start a collection"),
            ("/addtoplaylist <name> <song|link>", "add one or many tracks"),
            ("/removefromplaylist <name> <n>", "drop an entry"),
            ("/playplaylist <name>", "queue the whole thing"),
            ("/myplaylists", "what you have saved"),
            ("/afk [reason]", "away notice with an automatic reply"),
        ],
    ),
}

_HELP_TOPICS = {
    "play": ("ᴘʟᴀʏ", "play"),
    "queue": ("qᴜᴇᴜᴇ", "queue"),
    "sound": ("ˢᴏᴜɴᴅ", "sound"),
    "playlists": ("ᴘʟᴀʏʟɪˢᴛˢ", "playlist"),
    "all": ("ᴇᴠᴇʀʏᴛʜɪɴɢ", "list"),
}


def help_card(topic: str = "all", *, capabilities: Any = None) -> str:
    """A tiny-caps help screen; ``topic`` selects one section (``/help play``)."""
    topic = (topic or "all").strip().lower()
    heading = ui.h("music & voice", level=2, icon="music")
    selected = [topic] if topic in HELP_SECTIONS else list(HELP_SECTIONS)
    blocks: list[str] = [heading]
    for key in selected:
        title, entries = HELP_SECTIONS[key]
        rows = [(ui.esc(command), ui.esc(description)) for command, description in entries]
        blocks.append(ui.h(title, level=3))
        blocks.append(ui.table(["command", "what it does"], rows, raw=True))
    if capabilities is not None and not getattr(capabilities, "ready", False):
        blocks.append(ui.panel(f"{ui.e('warn')} {ui.esc(capabilities.note)}", icon="warn"))
    blocks.append(ui.footer_text("ᴛᴀᴘ ᴀ ᴛᴏᴘɪᴄ ʙᴇʟᴏᴡ · ᴄᴛʀʟ+ᴋ ɪɴ ᴛʜᴇ ᴡᴇʙ ᴘᴀɴᴇʟ"))
    return "\n\n".join(blocks)


def help_buttons() -> Any:
    return keys.keyboard(
        keys.row(
            *[
                keys.button(label, callback=cb("help", None, key), style="primary" if key == "all" else "link", icon=icon)
                for key, (label, icon) in _HELP_TOPICS.items()
            ]
        ),
        keys.row(keys.button("ᴄʟᴏˢᴇ", callback=cb("close"), style="danger", icon="cross")),
    )


def settings_card(preferences: dict[str, Any], *, chat_title: str = "") -> str:
    heading = ui.h("player settings", level=2, icon="settings")
    rows = [
        ("autoplay", ui.badge(bool(preferences.get("autoplay")), yes="on", no_="off")),
        ("autoleave", f"{int(preferences.get('autoleave') or 0)} s" if preferences.get("autoleave") else "off"),
        ("volume", f"{int(preferences.get('volume') or 0)}%"),
        ("max track length", f"{int(preferences.get('max_duration') or 0)} min" if preferences.get("max_duration") else "unlimited"),
        ("loop", ui.esc(LoopMode.parse(preferences.get("loop")).label)),
    ]
    return "\n\n".join(
        [
            heading,
            ui.panel(f"{ui.e('note')} <b>{ui.esc(chat_title or 'this chat')}</b>", icon="note"),
            ui.table(["setting", "value"], rows, raw=True),
            ui.footer_text("ᴛᴀᴘ ᴛᴏ ᴛᴏɢɢʟᴇ · ɪɴʟɪɴᴇ ʙᴜᴛᴛᴏɴˢ ʙᴇʟᴏᴡ"),
        ]
    )


def settings_buttons(preferences: dict[str, Any]) -> Any:
    return keys.keyboard(
        keys.row(
            keys.button(
                f"ᴀᴜᴛᴏᴘʟᴀʏ {ui.caps('on' if preferences.get('autoplay') else 'off')}",
                callback=cb("toggle_pref", None, "autoplay"),
                style="success" if preferences.get("autoplay") else "link",
                icon="party",
            ),
            keys.button(
                "ᴀᴜᴛᴏʟᴇᴀᴠᴇ",
                callback=cb("toggle_pref", None, "autoleave"),
                style="success" if preferences.get("autoleave") else "link",
                icon="clock",
            ),
        ),
        keys.row(
            keys.button("ᴠᴏʟᴜᴍᴇ −", callback=cb("vol", None, -10), style="link", icon="bell"),
            keys.button("ᴠᴏʟᴜᴍᴇ +", callback=cb("vol", None, 10), style="link", icon="bell"),
            keys.button("ʟᴏᴏᴘ", callback=cb("loop", None), style="link", icon="loop"),
        ),
        keys.row(keys.button("ᴄʟᴏˢᴇ", callback=cb("close"), style="danger", icon="cross")),
    )


# ---------------------------------------------------------------------------
#  afk / auth / errors
# ---------------------------------------------------------------------------


def afk_card(reason: str | None, *, user_name: str = "you") -> str:
    detail = ui.esc(reason) if reason else "no reason given"
    return "\n\n".join(
        [
            ui.h("away", level=2, icon="clock"),
            ui.panel(
                f"{ui.e('clock')} <b>{ui.esc(user_name)}</b> is away",
                f"\n{ui.e('note')} {detail}",
                "\nᴍᴇɴᴛɪᴏɴ ᴛʜᴇᴍ ᴏʀ ʀᴇᴘʟʏ ᴛᴏ ɴᴏᴛɪғʏ · ᴛʜᴇɪʀ ɴᴇxᴛ ᴍᴇꜱꜱᴀɢᴇ ᴄʟᴇᴀʀꜱ ɪᴛ",
                icon="clock",
            ),
        ]
    )


def afk_leave_card(*, minutes: int | None = None, count: int = 0) -> str:
    span = f"after {minutes} min" if minutes else "just now"
    return "\n\n".join(
        [
            ui.panel(
                f"{ui.e('check')} <b>welcome back</b> - away mode cleared ({ui.esc(span)})",
                f"\n{ui.e('bell')} {count} mention(s) arrived while you were away" if count else "",
                icon="check",
            )
        ]
    )


def afk_ping_card(user_name: str, reason: str | None, *, since: float | None = None) -> str:
    when = f" · away {_time_tag(since, 'relative')}" if since else ""
    return ui.panel(
        f"{ui.e('clock')} <b>{ui.esc(user_name)}</b> is away{when}",
        f"\n{ui.e('note')} {ui.esc(reason) if reason else 'no reason given'}",
        icon="clock",
    )


def auth_card(controllers: Sequence[int], *, names: dict[int, str] | None = None) -> str:
    heading = ui.h("player controllers", level=2, icon="crown")
    names = names or {}
    if not controllers:
        return "\n\n".join(
            [
                heading,
                ui.panel(
                    f"{ui.e('note')} only admins and the owner can control playback here",
                    "\nɢʀᴀɴᴛ ᴀᴄᴄᴇꜱꜱ ᴡɪᴛʜ /auth <ᴜꜱᴇʀ>",
                    icon="crown",
                ),
            ]
        )
    rows = [(str(user_id), ui.esc(names.get(user_id, "-"))) for user_id in controllers]
    return "\n\n".join([heading, ui.table(["user id", "name"], rows, raw=True)])


def error_card(reason: str, hint: str | None = None, *, icon: str = "cross") -> str:
    lines = [f"{ui.e(icon)} <b>{ui.esc(reason)}</b>"]
    if hint:
        lines.append(f"\n{ui.muted(ui.esc(hint))}")
    return ui.panel(*lines, icon=icon)


def loading_card(label: str) -> str:
    """Shown via ``send_rich_message_draft`` while yt-dlp is thinking."""
    return ui.panel(f"{ui.e('search')} {ui.esc(label)}", "\n<i>searching youtube…</i>", icon="search")


# ---------------------------------------------------------------------------
#  misc helpers used by the handlers
# ---------------------------------------------------------------------------


def close_button(*, chat_id: int | None = None, label: str = "ᴄʟᴏˢᴇ") -> Any:
    return keys.keyboard(keys.row(keys.button(label, callback=cb("close", chat_id), style="danger", icon="cross")))


def jump_or_close(*, chat_id: int | None = None) -> Any:
    row = []
    if chat_id is not None:
        row.append(keys.button("ɴᴏᴡ ᴘʟᴀʏɪɴɢ", callback=cb("panel", chat_id), style="primary", icon="music"))
    row.append(keys.button("ᴄʟᴏˢᴇ", callback=cb("close"), style="danger", icon="cross"))
    return keys.keyboard(keys.row(*row))


def paginate(items: Iterable[Any], page: int, *, size: int = 10) -> tuple[list[Any], int, int]:
    """Small helper shared by the playlist and queue pages."""
    materialised = list(items)
    pages = max(1, (len(materialised) + size - 1) // size)
    current = max(1, min(int(page or 1), pages))
    start = (current - 1) * size
    return materialised[start : start + size], current, pages
