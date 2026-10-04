"""The player: one voice room per chat.

A :class:`Room` holds everything about one voice chat - what is playing, how far
in, the queue, volume, speed, loop - and :class:`Player` is the small state
machine that drives them. It is deliberately independent of Telegram: the
handlers translate commands into calls here and render the result, the web panel
reads :meth:`Player.active` and :meth:`Player.snapshot`, and the tests drive it
with a :class:`~gohan.voice.backend.NullBackend`.

Playback progress is computed, not polled: ``offset`` is the position the last
seek/pause left us at, and the wall clock since ``started_at`` is added on top,
scaled by the playback speed. :meth:`Player.tick` - called once every few
seconds by the bot - decides when a track has finished and what happens next
(loop, autoplay, autoleave).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from . import sources
from .backend import NullBackend, VoiceBackend, build_backend
from .models import LoopMode, PlaybackState, Track
from .queue import VoiceQueue

log = get_logger("voice.player")

__all__ = ["EventHook", "Player", "Room", "RoomError", "build_player"]

#: Called as ``await hook(event, room)`` after every state change worth showing.
EventHook = Callable[[str, "Room"], Awaitable[None]]

#: How many seconds past the end we tolerate before declaring a track finished.
FINISH_GRACE = 1.5


class RoomError(RuntimeError):
    """A playback command that cannot be applied (no room, empty queue, …)."""


@dataclass
class Room:
    """One active voice chat."""

    chat_id: int
    queue: VoiceQueue = field(default_factory=VoiceQueue)
    current: Track | None = None
    paused: bool = False
    muted: bool = False
    volume: int = 100
    speed: float = 1.0
    #: position (seconds) when the current track was last (re)started
    offset: float = 0.0
    started_at: float | None = None
    connected: bool = False
    autoplay: bool = False
    empty_since: float | None = None
    #: (chat_id, message_id) of the control panel we keep editing
    panel: tuple[int, int] | None = None
    resolving: bool = False
    failures: int = 0

    # -- derived state -------------------------------------------------------

    @property
    def loop(self) -> LoopMode:
        return self.queue.loop

    @loop.setter
    def loop(self, mode: LoopMode | str) -> None:
        self.queue.loop = LoopMode.parse(mode)

    @property
    def playing(self) -> bool:
        return self.current is not None and self.started_at is not None and not self.paused

    @property
    def idle(self) -> bool:
        return self.current is None

    def position(self, *, at: float | None = None) -> float:
        """Where the needle is right now."""
        if self.current is None:
            return 0.0
        if self.paused or self.started_at is None:
            return max(0.0, self.offset)
        moment = at if at is not None else time.time()
        elapsed = max(0.0, (moment - self.started_at) * self.speed)
        return max(0.0, self.offset + elapsed)

    def remaining(self) -> float | None:
        if self.current is None or not self.current.duration:
            return None
        return max(0.0, self.current.duration - self.position())

    def finished(self, *, at: float | None = None) -> bool:
        """True once the current track has played past its end."""
        if self.current is None or self.paused:
            return False
        if self.current.live or not self.current.duration:
            return False
        return self.position(at=at) >= self.current.duration + FINISH_GRACE

    def state(self, *, backend: str = "none") -> PlaybackState:
        current = self.current
        return PlaybackState(
            chat_id=self.chat_id,
            title=current.display_title if current else None,
            url=current.url if current else None,
            thumbnail=sources.thumbnail_for(current) if current else None,
            source=current.source if current else "youtube",
            uploader=current.uploader if current else None,
            requested_by=current.requested_by if current else None,
            requested_name=current.requested_name if current else None,
            position=self.position(),
            duration=current.duration if current else None,
            paused=self.paused,
            muted=self.muted,
            volume=self.volume,
            speed=self.speed,
            loop=self.queue.loop,
            autoplay=self.autoplay,
            queue_length=len(self.queue),
            queue_duration=self.queue.total_duration,
            history_length=len(self.queue.history),
            connected=self.connected,
            playing=self.playing,
            backend=backend,
            started_at=self.started_at,
        )

    def snapshot(self, *, backend: str = "none", queue_limit: int = 10) -> dict[str, Any]:
        return {
            "state": self.state(backend=backend).to_dict(),
            "queue": self.queue.snapshot(limit=queue_limit),
            "panel": list(self.panel) if self.panel else None,
        }


class Player:
    """Owns every room and the transitions between tracks."""

    def __init__(
        self,
        *,
        backend: VoiceBackend | None = None,
        settings: Any = None,
        db: Any = None,
        hook: EventHook | None = None,
    ) -> None:
        self.backend: VoiceBackend = backend or NullBackend()
        self.settings = settings
        self.db = db
        self.hook = hook
        self.rooms: dict[int, Room] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._started = False

    # -- plumbing ------------------------------------------------------------

    def lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(int(chat_id), asyncio.Lock())

    @property
    def backend_name(self) -> str:
        return getattr(self.backend, "name", "none")

    @property
    def backend_ready(self) -> bool:
        return bool(getattr(self.backend, "available", False))

    async def start(self) -> bool:
        """Bring the backend up (no-op for the null backend)."""
        if self._started:
            return self.backend_ready
        try:
            self._started = await self.backend.start()
        except Exception as exc:  # pragma: no cover - backend specific
            log.warning("voice backend failed to start: %s", exc)
            self._started = False
        return self._started

    async def close(self) -> None:
        for chat_id in list(self.rooms):
            await self.leave(chat_id)
        await self.backend.close()

    async def _emit(self, event: str, room: Room) -> None:
        if self.hook is None:
            return
        try:
            await self.hook(event, room)
        except Exception as exc:  # pragma: no cover - a broken card must not stop playback
            log.warning("voice event hook failed for %r: %s", event, exc)

    def room(self, chat_id: int) -> Room | None:
        return self.rooms.get(int(chat_id))

    def ensure_room(self, chat_id: int, *, autoplay: bool = False, volume: int | None = None) -> Room:
        room = self.rooms.get(int(chat_id))
        if room is None:
            room = Room(
                chat_id=int(chat_id),
                queue=VoiceQueue(int(chat_id)),
                autoplay=autoplay,
                volume=volume if volume is not None else self.default_volume(),
            )
            self.rooms[int(chat_id)] = room
        return room

    def default_volume(self) -> int:
        value = int(getattr(self.settings, "voice_default_volume", 80) or 80)
        return max(0, min(value, 200))

    def default_autoleave(self) -> int:
        return int(getattr(self.settings, "voice_autoleave_sec", 300) or 0)

    def queue_limit(self) -> int:
        return int(getattr(self.settings, "voice_queue_limit", 100) or 100)

    # -- chat preferences ----------------------------------------------------

    async def preferences(self, chat_id: int) -> dict[str, Any]:
        """Per-chat playback preferences, stored in the chat settings blob."""
        defaults = {
            "autoplay": False,
            "autoleave": self.default_autoleave(),
            "volume": self.default_volume(),
            "max_duration": int(getattr(self.settings, "voice_max_duration_min", 0) or 0),
            "loop": LoopMode.OFF.value,
        }
        if self.db is None:
            return defaults
        try:
            stored = await self.db.get_chat_settings(chat_id)
        except Exception as exc:  # pragma: no cover - db hiccup must not break playback
            log.debug("could not read chat settings for %s: %s", chat_id, exc)
            return defaults
        merged = dict(defaults)
        for key in defaults:
            if key in stored and isinstance(stored[key], type(defaults[key])):
                merged[key] = stored[key]
            elif key in stored and isinstance(defaults[key], bool):
                merged[key] = bool(stored[key])
            elif key in stored:
                merged[key] = stored[key]
        return merged

    async def set_preference(self, chat_id: int, key: str, value: Any) -> Any:
        if self.db is None:
            raise RoomError("no database attached")
        await self.db.set_chat_setting(chat_id, key, value)
        if key == "autoplay":
            room = self.rooms.get(int(chat_id))
            if room is not None:
                room.autoplay = bool(value)
        if key == "volume":
            room = self.rooms.get(int(chat_id))
            if room is not None:
                room.volume = max(0, min(int(value), 200))
                await self.backend.volume(int(chat_id), room.volume)
        if key == "loop":
            room = self.rooms.get(int(chat_id))
            if room is not None:
                room.loop = LoopMode.parse(value)
        return value

    # -- adding to the queue -------------------------------------------------

    async def enqueue(
        self,
        chat_id: int,
        track: Track,
        *,
        front: bool = False,
        play_now: bool = True,
    ) -> tuple[Room, int]:
        """Queue a track and (unless the room is busy) start playing."""
        chat_id = int(chat_id)
        async with self.lock(chat_id):
            preferences = await self.preferences(chat_id)
            room = self.ensure_room(
                chat_id,
                autoplay=bool(preferences.get("autoplay")),
                volume=int(preferences.get("volume") or self.default_volume()),
            )
            if room.queue.loop is LoopMode.OFF and preferences.get("loop"):
                room.queue.loop = LoopMode.parse(preferences.get("loop"))
            position = room.queue.add(track, front=front)
            room.empty_since = None
            should_play = play_now and room.current is None and not room.resolving

        if should_play:
            await self._play_current(chat_id)
        await self._emit("enqueue", self.rooms[chat_id])
        return self.rooms[chat_id], position

    async def enqueue_many(self, chat_id: int, tracks: list[Track]) -> tuple[Room, int]:
        chat_id = int(chat_id)
        async with self.lock(chat_id):
            preferences = await self.preferences(chat_id)
            room = self.ensure_room(chat_id, autoplay=bool(preferences.get("autoplay")))
            added = room.queue.extend(tracks)
            room.empty_since = None
            start = room.current is None
        if start and added:
            await self._play_current(chat_id)
        return self.rooms[chat_id], added

    # -- playback ------------------------------------------------------------

    async def _play_current(self, chat_id: int) -> bool:
        """Resolve and start ``room.current`` (or the next queued track)."""
        chat_id = int(chat_id)
        room = self.rooms.get(chat_id)
        if room is None:
            return False
        if room.current is None:
            room.current = room.queue.pop()
        if room.current is None:
            room.empty_since = time.time()
            return False

        track = room.current
        room.resolving = True
        try:
            target = await sources.prepare(track, settings=self.settings, prefer_file=track.live)
        except sources.SourceError as exc:
            log.warning("could not prepare %s: %s", track.title, exc)
            target = None
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("unexpected failure preparing %s: %s", track.title, exc)
            target = None
        room.resolving = False

        if not target:
            room.failures += 1
            log.warning("skipping %s - no playable stream", track.title)
            if room.failures < 3:
                room.current = None
                return await self._play_current(chat_id)
            room.failures = 0
            room.current = None
            room.empty_since = time.time()
            await self._emit("failed", room)
            return False

        joined = await self.backend.join(chat_id)
        started = await self.backend.play(chat_id, target)
        room.connected = bool(joined or started)
        room.offset = 0.0
        room.started_at = time.time()
        room.paused = False
        room.failures = 0
        if room.loop is LoopMode.ONE:
            pass  # already primed by queue.advance()
        await self.backend.volume(chat_id, room.volume)
        if room.speed != 1.0:
            await self.backend.speed(chat_id, room.speed)
        if room.muted:
            await self.backend.mute(chat_id)
        log.info("voice: playing %r in %s (%s)", track.display_title, chat_id, self.backend_name)
        await self._emit("play", room)
        return True

    async def play(self, chat_id: int, query: str, *, requested_by: int | None = None,
                   requested_name: str | None = None, front: bool = False) -> tuple[Room, Track, int]:
        """``/play``: find the track behind ``query`` and enqueue it."""
        track = await sources.resolve(query, settings=self.settings)
        track.requested_by = requested_by
        track.requested_name = requested_name
        room, position = await self.enqueue(chat_id, track, front=front)
        return room, track, position

    async def skip(self, chat_id: int, count: int = 1) -> Track | None:
        """Drop the current track and play the next one."""
        chat_id = int(chat_id)
        room = self.rooms.get(chat_id)
        if room is None:
            raise RoomError("nothing is playing here")
        skipped = room.current
        async with self.lock(chat_id):
            for _ in range(max(1, count) - 1):
                room.queue.pop()
            room.queue.loop = LoopMode.OFF if room.queue.loop is LoopMode.ONE else room.queue.loop
            room.current = None
            room.started_at = None
            room.current = room.queue.pop()
        if room.current is None:
            await self.stop(chat_id, leave=True)
            return skipped
        await self._play_current(chat_id)
        return skipped

    async def replay(self, chat_id: int) -> bool:
        """Start the current track again from zero."""
        chat_id = int(chat_id)
        room = self.rooms.get(chat_id)
        if room is None or room.current is None:
            raise RoomError("nothing is playing here")
        room.offset = 0.0
        room.started_at = time.time()
        room.paused = False
        await self.backend.seek(chat_id, 0)
        await self.backend.resume(chat_id)
        await self._emit("replay", room)
        return True

    async def stop(self, chat_id: int, *, leave: bool = True) -> Room | None:
        """Clear the room; ``leave=True`` also disconnects from the voice chat."""
        chat_id = int(chat_id)
        room = self.rooms.get(chat_id)
        if room is None:
            return None
        room.queue.clear()
        room.current = None
        room.started_at = None
        room.offset = 0.0
        room.paused = False
        if leave:
            await self.backend.leave(chat_id)
            room.connected = False
            self.rooms.pop(chat_id, None)
        await self._emit("stop", room)
        return room

    async def leave(self, chat_id: int) -> bool:
        """Disconnect without touching anything else (autoleave, /stop)."""
        chat_id = int(chat_id)
        room = self.rooms.pop(chat_id, None)
        await self.backend.leave(chat_id)
        if room is not None:
            room.connected = False
        return True

    async def pause(self, chat_id: int) -> bool:
        room = self._require_room(chat_id)
        if room.current is None:
            raise RoomError("nothing is playing here")
        if room.paused:
            return False
        room.offset = room.position()
        room.started_at = None
        room.paused = True
        await self.backend.pause(int(chat_id))
        await self._emit("pause", room)
        return True

    async def resume(self, chat_id: int) -> bool:
        room = self._require_room(chat_id)
        if room.current is None:
            raise RoomError("nothing is playing here")
        if not room.paused:
            return False
        room.started_at = time.time()
        room.paused = False
        await self.backend.resume(int(chat_id))
        await self._emit("resume", room)
        return True

    async def toggle(self, chat_id: int) -> bool:
        """``/pause`` and ``/resume`` in one button: returns True when now playing."""
        room = self._require_room(chat_id)
        if room.paused:
            await self.resume(chat_id)
            return True
        await self.pause(chat_id)
        return False

    async def seek(self, chat_id: int, seconds: int) -> float:
        room = self._require_room(chat_id)
        if room.current is None:
            raise RoomError("nothing is playing here")
        total = room.current.duration
        target = max(0, int(seconds))
        if total:
            target = min(target, max(0, total - 2))
        room.offset = float(target)
        if not room.paused:
            room.started_at = time.time()
        await self.backend.seek(int(chat_id), target)
        await self._emit("seek", room)
        return room.offset

    async def seek_back(self, chat_id: int, seconds: int = 10) -> float:
        room = self._require_room(chat_id)
        return await self.seek(chat_id, int(room.position() - abs(int(seconds))))

    async def speed(self, chat_id: int, rate: float) -> float:
        room = self._require_room(chat_id)
        rate = max(0.5, min(float(rate), 2.0))
        room.offset = room.position()
        room.started_at = time.time() if not room.paused else None
        room.speed = rate
        await self.backend.speed(int(chat_id), rate)
        await self._emit("speed", room)
        return rate

    async def volume(self, chat_id: int, percent: int) -> int:
        room = self._require_room(chat_id)
        room.volume = max(0, min(int(percent), 200))
        await self.backend.volume(int(chat_id), room.volume)
        await self._emit("volume", room)
        return room.volume

    async def mute(self, chat_id: int, muted: bool = True) -> bool:
        room = self._require_room(chat_id)
        room.muted = bool(muted)
        if room.muted:
            await self.backend.mute(int(chat_id))
        else:
            await self.backend.unmute(int(chat_id))
        await self._emit("mute", room)
        return room.muted

    async def loop(self, chat_id: int, mode: LoopMode | str | None = None) -> LoopMode:
        room = self._require_room(chat_id)
        room.queue.loop = room.queue.loop.cycle() if mode is None else LoopMode.parse(mode)
        if self.db is not None:
            try:
                await self.db.set_chat_setting(int(chat_id), "loop", room.queue.loop.value)
            except Exception as exc:  # pragma: no cover
                log.debug("could not persist loop mode: %s", exc)
        await self._emit("loop", room)
        return room.queue.loop

    async def shuffle(self, chat_id: int) -> int:
        room = self._require_room(chat_id)
        count = room.queue.shuffle()
        await self._emit("shuffle", room)
        return count

    async def jump(self, chat_id: int, index: int) -> Track | None:
        room = self._require_room(chat_id)
        track = room.queue.jump(index)
        if track is None:
            return None
        if room.current is None:
            await self._play_current(int(chat_id))
        await self._emit("jump", room)
        return track

    async def remove(self, chat_id: int, index: int) -> Track | None:
        room = self._require_room(chat_id)
        track = room.queue.remove(index)
        if track is not None:
            await self._emit("remove", room)
        return track

    async def autoplay(self, chat_id: int, enabled: bool | None = None) -> bool:
        room = self._require_room(chat_id)
        room.autoplay = (not room.autoplay) if enabled is None else bool(enabled)
        if self.db is not None:
            try:
                await self.db.set_chat_setting(int(chat_id), "autoplay", room.autoplay)
            except Exception as exc:  # pragma: no cover
                log.debug("could not persist autoplay: %s", exc)
        await self._emit("autoplay", room)
        return room.autoplay

    # -- the beat ------------------------------------------------------------

    async def tick(self) -> list[str]:
        """Advance finished tracks, top up autoplay, and leave idle rooms.

        Returns the list of events emitted, which the tests assert on and the
        handlers use to know whether a card needs redrawing.
        """
        events: list[str] = []
        for chat_id in list(self.rooms):
            room = self.rooms.get(chat_id)
            if room is None:
                continue
            if room.finished():
                log.info("voice: %r finished in %s", room.current.display_title if room.current else "?", chat_id)
                finished = room.current
                room.current = room.queue.advance(finished)
                if room.current is None:
                    if room.autoplay and finished is not None:
                        replacement = await sources.related(finished, settings=self.settings)
                        if replacement is not None:
                            room.current = replacement
                    if room.current is None:
                        room.started_at = None
                        room.offset = 0.0
                        room.empty_since = time.time()
                        await self.backend.pause(chat_id)
                        await self._emit("finished", room)
                        events.append("finished")
                        continue
                await self._play_current(chat_id)
                events.append("next")

            room = self.rooms.get(chat_id)
            if room is None:
                continue
            # autoleave: an idle room does not hold the assistant hostage
            if room.current is None and room.empty_since is None:
                room.empty_since = time.time()
            if room.current is None and room.empty_since is not None:
                preferences = await self.preferences(chat_id)
                window = int(preferences.get("autoleave") or 0)
                if window > 0 and time.time() - room.empty_since >= window:
                    log.info("voice: leaving idle room %s after %ss", chat_id, window)
                    await self.leave(chat_id)
                    events.append("autoleave")
        return events

    async def on_stream_end(self, chat_id: int) -> None:
        """Hook for a backend that reports the end of a stream itself."""
        room = self.rooms.get(int(chat_id))
        if room is None or room.current is None:
            return
        room.offset = float(room.current.duration or 0)
        room.started_at = time.time()
        await self.tick()

    # -- reads ---------------------------------------------------------------

    def _require_room(self, chat_id: int) -> Room:
        room = self.rooms.get(int(chat_id))
        if room is None:
            raise RoomError("no voice chat is active here")
        return room

    def active(self) -> list[Room]:
        return [room for room in self.rooms.values() if room.current is not None or room.queue]

    def snapshot(self, chat_id: int) -> dict[str, Any] | None:
        room = self.rooms.get(int(chat_id))
        if room is None:
            return None
        return room.snapshot(backend=self.backend_name)

    def overview(self) -> dict[str, Any]:
        """Everything the web panel and ``/voice`` need in one blob."""
        rooms = self.active()
        status = self.backend.status().to_dict() if hasattr(self.backend, "status") else {
            "name": self.backend_name,
            "available": self.backend_ready,
        }
        return {
            "backend": status,
            "rooms": [room.snapshot(backend=self.backend_name) for room in rooms],
            "playing": sum(1 for room in rooms if room.playing),
            "queued": sum(len(room.queue) for room in rooms),
            "listeners": len(self.rooms),
        }


def build_player(settings: Any, db: Any = None, mtproto: Any = None, hook: EventHook | None = None) -> Player:
    """Wire a player with the backend the settings ask for."""
    return Player(backend=build_backend(settings, mtproto), settings=settings, db=db, hook=hook)
