"""The vocabulary of the voice engine.

Everything that crosses a module boundary - a queued track, the state of a room,
the loop mode - is defined here, so the handlers, the player, the web panel and
the tests all agree on the same shapes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "LoopMode",
    "PlaybackState",
    "Track",
    "format_duration",
    "parse_time",
    "progress_bar",
    "source_label",
]


class LoopMode(str, Enum):
    """What happens when a track ends (``/loop`` cycles through these)."""

    OFF = "off"
    ONE = "one"
    QUEUE = "queue"

    @classmethod
    def parse(cls, value: Any) -> LoopMode:
        if isinstance(value, LoopMode):
            return value
        text = str(value or "").strip().lower()
        for mode in cls:
            if text == mode.value:
                return mode
        return cls.OFF

    def cycle(self) -> LoopMode:
        order = [LoopMode.OFF, LoopMode.ONE, LoopMode.QUEUE]
        return order[(order.index(self) + 1) % len(order)]

    @property
    def label(self) -> str:
        return {LoopMode.OFF: "off", LoopMode.ONE: "one track", LoopMode.QUEUE: "whole queue"}[self]

    @property
    def icon(self) -> str:
        return {LoopMode.OFF: "➡️", LoopMode.ONE: "🔂", LoopMode.QUEUE: "🔁"}[self]


#: How a track arrived: it decides the badge on the now-playing card.
_SOURCE_LABELS = {
    "youtube": "youtube",
    "youtube-music": "yt music",
    "soundcloud": "soundcloud",
    "spotify": "spotify",
    "telegram": "telegram",
    "url": "direct link",
    "file": "local file",
    "radio": "radio",
    "unknown": "unknown",
}


def source_label(source: str) -> str:
    return _SOURCE_LABELS.get((source or "").lower(), (source or "unknown").lower())


def _column(row: Any, name: str, default: Any = None) -> Any:
    """Read ``row[name]`` when the column exists, else ``default``.

    ``sqlite3.Row`` exposes ``keys()``, but membership on the row itself tests
    *values* - so the column names have to be checked explicitly.
    """
    try:
        keys = row.keys()
    except AttributeError:  # a plain mapping
        return (row or {}).get(name, default)
    if name not in keys:
        return default
    try:
        return row[name]
    except (IndexError, KeyError):  # pragma: no cover - defensive
        return default


@dataclass(slots=True)
class Track:
    """One playable item.

    ``path`` and ``stream_url`` are filled in lazily: the queue only needs the
    metadata, and resolving a real audio stream costs a yt-dlp call.
    """

    title: str
    url: str
    duration: int | None = None
    video_id: str | None = None
    uploader: str | None = None
    thumbnail: str | None = None
    source: str = "youtube"
    requested_by: int | None = None
    requested_name: str | None = None
    live: bool = False
    path: str | None = None
    stream_url: str | None = None
    track_id: int | None = None
    added_at: float = field(default_factory=time.time)

    # -- presentation --------------------------------------------------------

    @property
    def duration_text(self) -> str:
        if self.live:
            return "ʟɪᴠᴇ"
        return format_duration(self.duration)

    @property
    def display_title(self) -> str:
        title = (self.title or "unknown").strip()
        return title if len(title) <= 70 else title[:67] + "…"

    @property
    def requester(self) -> str:
        if self.requested_name:
            return self.requested_name
        if self.requested_by:
            return str(self.requested_by)
        return "the queue"

    # -- (de)serialisation ---------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "duration": self.duration,
            "video_id": self.video_id,
            "uploader": self.uploader,
            "thumbnail": self.thumbnail,
            "source": self.source,
            "requested_by": self.requested_by,
            "requested_name": self.requested_name,
            "live": self.live,
            "path": self.path,
            "stream_url": None,  # never persist a signed googlevideo URL
            "track_id": self.track_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Track:
        """Rebuild a track, ignoring unknown keys (old rows must not crash us)."""
        allowed = set(cls.__dataclass_fields__)
        payload = {k: v for k, v in (data or {}).items() if k in allowed}
        payload.setdefault("title", "unknown")
        payload.setdefault("url", "")
        try:
            return cls(**payload)
        except TypeError:  # pragma: no cover - defensive, e.g. renamed fields
            return cls(title=str(payload.get("title", "unknown")), url=str(payload.get("url", "")))

    @classmethod
    def from_row(cls, row: Any) -> Track:
        """Build from a sqlite row of the ``playlist_tracks`` table.

        Older rows may predate columns added later, so every field goes through
        :func:`_column` instead of assuming today's shape.
        """
        return cls(
            title=_column(row, "title") or "unknown",
            url=_column(row, "url") or "",
            duration=_column(row, "duration"),
            video_id=_column(row, "video_id"),
            uploader=_column(row, "uploader"),
            thumbnail=_column(row, "thumbnail"),
            source=_column(row, "source") or "youtube",
            requested_by=_column(row, "added_by"),
            track_id=_column(row, "id"),
        )


# ---------------------------------------------------------------------------
#  time helpers
# ---------------------------------------------------------------------------


def format_duration(seconds: float | int | None) -> str:
    """``3725`` → ``1:02:05`` (and ``83`` → ``1:23``)."""
    if seconds is None:
        return "--:--"
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "--:--"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_time(text: str) -> int | None:
    """``"1:23"``/``"83"``/``"1:02:03"`` → seconds; ``None`` when unreadable."""
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    parts = raw.split(":")
    if not 1 < len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds


def progress_bar(
    position: float | int,
    duration: float | int | None,
    *,
    width: int = 14,
    played: str = "━",
    remaining: str = "─",
    knob: str = "🔘",
) -> str:
    """A text progress bar, e.g. ``━━━━━🔘─────────``.

    Without a duration there is nothing to measure against, so the knob simply
    sits in the middle - the caller usually shows "ʟɪᴠᴇ" next to it.
    """
    if not duration or duration <= 0:
        left = width // 2
        return played * left + knob + remaining * (width - left)
    ratio = max(0.0, min(1.0, float(position) / float(duration)))
    filled = int(round(ratio * width))
    filled = max(0, min(width, filled))
    if filled >= width:
        return played * width + knob
    return played * filled + knob + remaining * (width - filled)


# ---------------------------------------------------------------------------
#  room state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PlaybackState:
    """A read-only snapshot of one voice room, for the cards and the web panel."""

    chat_id: int
    title: str | None = None
    url: str | None = None
    thumbnail: str | None = None
    source: str = "youtube"
    uploader: str | None = None
    requested_by: int | None = None
    requested_name: str | None = None
    position: float = 0.0
    duration: int | None = None
    paused: bool = False
    muted: bool = False
    volume: int = 100
    speed: float = 1.0
    loop: LoopMode = LoopMode.OFF
    autoplay: bool = False
    queue_length: int = 0
    queue_duration: int = 0
    history_length: int = 0
    connected: bool = False
    playing: bool = False
    backend: str = "none"
    started_at: float | None = None
    updated_at: float = field(default_factory=time.time)

    @property
    def progress_text(self) -> str:
        return f"{format_duration(self.position)} / {format_duration(self.duration)}"

    @property
    def bar(self) -> str:
        return progress_bar(self.position, self.duration)

    @property
    def ends_at(self) -> float | None:
        """Unix timestamp when the current track ends (for ``<tg-time>``)."""
        if not self.duration or self.paused:
            return None
        remaining = max(0.0, self.duration - self.position)
        if self.speed:
            remaining /= self.speed
        return time.time() + remaining

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "url": self.url,
            "thumbnail": self.thumbnail,
            "source": self.source,
            "uploader": self.uploader,
            "requested_by": self.requested_by,
            "requested_name": self.requested_name,
            "position": round(self.position, 2),
            "position_text": format_duration(self.position),
            "duration": self.duration,
            "duration_text": format_duration(self.duration),
            "progress_text": self.progress_text,
            "paused": self.paused,
            "muted": self.muted,
            "volume": self.volume,
            "speed": self.speed,
            "loop": self.loop.value,
            "autoplay": self.autoplay,
            "queue_length": self.queue_length,
            "queue_duration": self.queue_duration,
            "queue_duration_text": format_duration(self.queue_duration),
            "connected": self.connected,
            "playing": self.playing,
            "backend": self.backend,
            "live": self.duration is None,
        }
