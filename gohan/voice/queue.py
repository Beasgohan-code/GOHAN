"""The per-chat play queue.

Pure logic, no I/O, no Telegram: everything here is exercised directly by the
tests. The player owns one :class:`VoiceQueue` per room and only ever asks it
two questions - "what plays next?" (:meth:`VoiceQueue.advance`) and "what is
queued?" (:meth:`VoiceQueue.page`).
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Any

from .models import LoopMode, Track, format_duration

__all__ = ["QUEUE_LIMIT", "PAGE_SIZE", "QueueFull", "VoiceQueue"]

#: A sane ceiling: past this the /queue card is unreadable and memory is wasted.
QUEUE_LIMIT = 200
#: Rows per page of the /queue card.
PAGE_SIZE = 10


class QueueFull(RuntimeError):
    """Raised when the queue is at :data:`QUEUE_LIMIT`."""


class VoiceQueue:
    """An ordered list of tracks plus loop/shuffle/history behaviour."""

    __slots__ = ("chat_id", "slots", "history", "loop", "limit", "loop_count")

    def __init__(self, chat_id: int, *, limit: int = QUEUE_LIMIT, loop: LoopMode = LoopMode.OFF) -> None:
        self.chat_id = chat_id
        self.slots: list[Track] = []
        self.history: list[Track] = []
        self.loop = LoopMode.parse(loop)
        self.limit = int(limit)
        #: how many times the *current* track has been repeated (loop=one)
        self.loop_count = 0

    # -- dunders -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.slots)

    def __bool__(self) -> bool:
        return bool(self.slots)

    def __iter__(self) -> Iterator[Track]:
        return iter(self.slots)

    def __getitem__(self, index: int) -> Track:
        return self.slots[index]

    # -- mutation ------------------------------------------------------------

    def add(self, track: Track, *, front: bool = False) -> int:
        """Queue ``track``; returns its 1-based position."""
        if len(self.slots) >= self.limit:
            raise QueueFull(f"the queue is full ({self.limit} tracks)")
        if front:
            self.slots.insert(0, track)
            return 1
        self.slots.append(track)
        return len(self.slots)

    def extend(self, tracks: list[Track], *, front: bool = False) -> int:
        added = 0
        for track in tracks:
            try:
                self.add(track, front=front)
            except QueueFull:
                break
            added += 1
        return added

    def pop(self) -> Track | None:
        """Take the next track off the front."""
        return self.slots.pop(0) if self.slots else None

    def advance(self, finished: Track | None) -> Track | None:
        """Decide what plays after ``finished`` - this is where loop lives.

        ``off``   drop it and play the next queued track (``None`` if empty)
        ``one``   the same track again
        ``queue`` loop back to the start of the queue once it drains
        """
        if finished is not None:
            self.history.append(finished)
            del self.history[:-50]
        if self.loop is LoopMode.ONE and finished is not None:
            self.loop_count += 1
            return finished
        nxt = self.pop()
        if nxt is not None:
            self.loop_count = 0
            return nxt
        if self.loop is LoopMode.QUEUE and finished is not None:
            self.loop_count += 1
            return finished
        return None

    def remove(self, index: int) -> Track | None:
        """Remove the 1-based ``index`` from the queue."""
        position = int(index) - 1
        if 0 <= position < len(self.slots):
            return self.slots.pop(position)
        return None

    def move(self, source: int, target: int) -> bool:
        """Move a queued track (both 1-based) - ``/cmove`` in the original bot."""
        start, end = int(source) - 1, int(target) - 1
        if not (0 <= start < len(self.slots)) or not (0 <= end < len(self.slots)):
            return False
        self.slots.insert(end, self.slots.pop(start))
        return True

    def jump(self, index: int) -> Track | None:
        """Pull a queued track to the front so it plays next."""
        position = int(index) - 1
        if 0 <= position < len(self.slots):
            return self.slots.insert(0, self.slots.pop(position))
        return None

    def shuffle(self, rng: random.Random | None = None) -> int:
        """Shuffle the *upcoming* tracks (history and the current track stay)."""
        (rng or random).shuffle(self.slots)
        return len(self.slots)

    def clear(self) -> int:
        count = len(self.slots)
        self.slots.clear()
        return count

    def deduplicate(self) -> int:
        """Drop duplicate video ids, keeping the first occurrence."""
        seen: set[str] = set()
        kept: list[Track] = []
        for track in self.slots:
            key = track.video_id or track.url
            if key and key in seen:
                continue
            seen.add(key)
            kept.append(track)
        removed = len(self.slots) - len(kept)
        self.slots = kept
        return removed

    # -- reads ---------------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self.slots)

    @property
    def next_track(self) -> Track | None:
        return self.slots[0] if self.slots else None

    @property
    def total_duration(self) -> int:
        return sum(track.duration or 0 for track in self.slots)

    @property
    def total_text(self) -> str:
        return format_duration(self.total_duration)

    def page(self, number: int = 1, *, size: int = PAGE_SIZE) -> tuple[list[Track], int, int]:
        """One page of the queue → ``(tracks, page, pages)``."""
        size = max(1, int(size))
        pages = max(1, (len(self.slots) + size - 1) // size)
        current = max(1, min(int(number), pages))
        start = (current - 1) * size
        return self.slots[start : start + size], current, pages

    def index_of(self, track: Track) -> int | None:
        for position, candidate in enumerate(self.slots, start=1):
            if candidate is track:
                return position
        return None

    def snapshot(self, *, limit: int = PAGE_SIZE) -> dict[str, Any]:
        """A JSON-friendly view for the web panel and the SSE stream."""
        return {
            "chat_id": self.chat_id,
            "size": len(self.slots),
            "loop": self.loop.value,
            "total_duration": self.total_duration,
            "total_text": self.total_text,
            "track_ids": [t.video_id or t.url for t in self.slots],
            "tracks": [
                {
                    "position": position,
                    "title": track.display_title,
                    "duration": track.duration,
                    "duration_text": track.duration_text,
                    "uploader": track.uploader,
                    "requested_by": track.requested_by,
                    "requested_name": track.requested_name,
                    "url": track.url,
                    "thumbnail": track.thumbnail,
                }
                for position, track in enumerate(self.slots[: max(1, limit)], start=1)
            ],
        }
