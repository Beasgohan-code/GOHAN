"""Voice: a full music player for group voice chats.

Ported from the Go bot `AnvuMusic <https://github.com/Naman-Devio/AnvuMusic>`_
(GPL-3.0) and re-imagined around this project's rich-message UI.

The shape of it:

``models``    the vocabulary - :class:`~gohan.voice.models.Track`, ``LoopMode``,
              :class:`~gohan.voice.models.PlaybackState`
``queue``     the per-chat queue: loop, shuffle, jump, history
``sources``   yt-dlp: search, resolve, playlists, direct streams, downloads
``backend``   py-tgcalls when it is installed, a recording null backend if not
``player``    the state machine that drives a room, one per chat
``playlists`` saved collections, backed by sqlite
``cards``     every screen as a rich message (tables, cover art, live clocks)
``handlers``  the commands and the ``v:*`` callbacks
``afk``       away mode, as a middleware so it never starves other routers

Nothing in this package requires a voice chat to exist: without py-tgcalls, the
assistant session or even yt-dlp, the bot still queues tracks, renders cards and
mirrors everything into the web panel.
"""

from __future__ import annotations

from .afk import AfkWatcher, set_away
from .backend import BackendStatus, NullBackend, PyTgCallsBackend, VoiceBackend, build_backend
from .models import LoopMode, PlaybackState, Track, format_duration, parse_time, progress_bar
from .player import EventHook, Player, Room, RoomError, build_player
from .playlists import PlaylistError, PlaylistInfo, PlaylistService
from .queue import PAGE_SIZE, QUEUE_LIMIT, QueueFull, VoiceQueue
from .sources import Capabilities, SourceError, capabilities, download, related, resolve, search

__all__ = [
    "AfkWatcher",
    "BackendStatus",
    "Capabilities",
    "EventHook",
    "LoopMode",
    "NullBackend",
    "PAGE_SIZE",
    "PlaybackState",
    "Player",
    "PlaylistError",
    "PlaylistInfo",
    "PlaylistService",
    "PyTgCallsBackend",
    "QUEUE_LIMIT",
    "QueueFull",
    "Room",
    "RoomError",
    "SourceError",
    "Track",
    "VoiceBackend",
    "VoiceQueue",
    "build_backend",
    "build_player",
    "capabilities",
    "download",
    "format_duration",
    "parse_time",
    "progress_bar",
    "related",
    "resolve",
    "search",
    "set_away",
]
