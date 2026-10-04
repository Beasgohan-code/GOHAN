"""Voice-chat backends.

The player never talks to py-tgcalls directly: it talks to a small protocol with
two implementations.

``NullBackend``
    Does *nothing* but remember what it was asked to do. This is what runs when
    py-tgcalls is not installed, and what the tests assert against - the queue,
    the cards and the web panel all behave exactly as they do in production, so
    a deployment without native libraries still gets a working bot instead of
    tracebacks.

``PyTgCallsBackend``
    The real thing: a Kurigram (MTProto) client drives a py-tgcalls call. It is
    imported lazily and every call is version-tolerant, because py-tgcalls has
    renamed its own methods more than once (``join_group_call`` → ``play``,
    ``leave_group_call`` → ``leave_call`` …). Anything we do not recognise is
    reported through :attr:`PyTgCallsBackend.warnings` instead of raising.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..logging_setup import get_logger

log = get_logger("voice.backend")

__all__ = ["BackendStatus", "NullBackend", "PyTgCallsBackend", "VoiceBackend", "build_backend"]


class VoiceBackend(Protocol):
    """What the player needs from a voice backend."""

    name: str

    @property
    def available(self) -> bool: ...

    async def start(self) -> bool: ...

    async def close(self) -> None: ...

    async def join(self, chat_id: int) -> bool: ...

    async def leave(self, chat_id: int) -> bool: ...

    async def play(self, chat_id: int, target: str, *, video: bool = False) -> bool: ...

    async def pause(self, chat_id: int) -> bool: ...

    async def resume(self, chat_id: int) -> bool: ...

    async def seek(self, chat_id: int, seconds: int) -> bool: ...

    async def speed(self, chat_id: int, rate: float) -> bool: ...

    async def mute(self, chat_id: int) -> bool: ...

    async def unmute(self, chat_id: int) -> bool: ...

    async def volume(self, chat_id: int, percent: int) -> bool: ...


@dataclass(slots=True)
class BackendStatus:
    """Human-readable backend state for ``/voice`` and the status page."""

    name: str = "none"
    available: bool = False
    joined: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "available": self.available,
            "joined": list(self.joined),
            "warnings": list(self.warnings),
            "note": self.note,
        }


class NullBackend:
    """A backend that records requests and pretends to succeed.

    It exists so the bot is *useful* without py-tgcalls: the queue fills, the
    cards render, the panel shows what is playing - and ``/voice`` explains that
    audio would start once the extra is installed.
    """

    name = "null"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, Any]] = []
        self.joined: set[int] = set()
        self.tracks: dict[int, str] = {}

    @property
    def available(self) -> bool:
        return False

    def _record(self, action: str, chat_id: int, value: Any = None) -> bool:
        self.calls.append((action, chat_id, value))
        return True

    async def start(self) -> bool:
        return False

    async def close(self) -> None:
        self.joined.clear()

    async def join(self, chat_id: int) -> bool:
        self.joined.add(int(chat_id))
        return self._record("join", chat_id)

    async def leave(self, chat_id: int) -> bool:
        self.joined.discard(int(chat_id))
        self.tracks.pop(int(chat_id), None)
        return self._record("leave", chat_id)

    async def play(self, chat_id: int, target: str, *, video: bool = False) -> bool:
        self.tracks[int(chat_id)] = target
        return self._record("play", chat_id, target)

    async def pause(self, chat_id: int) -> bool:
        return self._record("pause", chat_id)

    async def resume(self, chat_id: int) -> bool:
        return self._record("resume", chat_id)

    async def seek(self, chat_id: int, seconds: int) -> bool:
        return self._record("seek", chat_id, seconds)

    async def speed(self, chat_id: int, rate: float) -> bool:
        return self._record("speed", chat_id, rate)

    async def mute(self, chat_id: int) -> bool:
        return self._record("mute", chat_id)

    async def unmute(self, chat_id: int) -> bool:
        return self._record("unmute", chat_id)

    async def volume(self, chat_id: int, percent: int) -> bool:
        return self._record("volume", chat_id, percent)

    def status(self) -> BackendStatus:
        return BackendStatus(
            name=self.name,
            available=False,
            joined=sorted(self.joined),
            note="preview mode - install the 'voice' extra and connect the assistant to hear audio",
        )


class PyTgCallsBackend:
    """py-tgcalls driven by the Kurigram assistant session."""

    #: method-name candidates per operation, newest first
    _ALIASES: dict[str, tuple[str, ...]] = {
        "join": ("play", "join_group_call", "join_call"),
        "leave": ("leave_call", "leave_group_call", "close_call"),
        "pause": ("pause", "pause_stream"),
        "resume": ("resume", "resume_stream"),
        "seek": ("seek",),
        "speed": ("set_playback_speed", "speed"),
        "mute": ("mute",),
        "unmute": ("unmute",),
        "volume": ("set_volume", "change_volume_call"),
    }

    name = "pytgcalls"

    def __init__(self, mtproto: Any = None) -> None:
        self.mtproto = mtproto
        self.call: Any = None
        self.warnings: list[str] = []
        self.joined: set[int] = set()
        self._started = False
        self._lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._started and self.call is not None

    async def start(self) -> bool:
        """Import py-tgcalls and attach it to the assistant client."""
        async with self._lock:
            if self._started:
                return self.available
            client = None
            if self.mtproto is not None:
                client = getattr(self.mtproto, "client", None)
                if client is None and callable(getattr(self.mtproto, "get_client", None)):
                    client = self.mtproto.get_client()
            if client is None:
                self._warn("no assistant session - set MTPROTO_MODE=user and log in first")
                return False
            try:
                from pytgcalls import PyTgCalls
            except ImportError as exc:
                self._warn(f"py-tgcalls is not installed ({exc})")
                return False
            try:
                self.call = PyTgCalls(client)
                result = self.call.start()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # pragma: no cover - needs a real session
                self._warn(f"py-tgcalls failed to start: {type(exc).__name__}: {exc}")
                self.call = None
                return False
            self._started = True
            log.info("py-tgcalls started")
            return True

    async def close(self) -> None:
        if self.call is None:
            return
        for chat_id in list(self.joined):
            await self.leave(chat_id)
        self.call = None
        self._started = False

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
            log.warning("voice backend: %s", message)

    # -- operation dispatch --------------------------------------------------

    async def _call(self, operation: str, chat_id: int, *args: Any) -> bool:
        if self.call is None:
            return False
        for name in self._ALIASES.get(operation, (operation,)):
            method = getattr(self.call, name, None)
            if method is None:
                continue
            try:
                result = method(int(chat_id), *args)
                if asyncio.iscoroutine(result):
                    await result
                return True
            except Exception as exc:
                message = f"{name} failed: {type(exc).__name__}: {exc}"
                if self._is_unsupported(exc, name):
                    continue  # try the next alias
                self._warn(message)
                return False
        self._warn(f"no working method for {operation!r} in this py-tgcalls version")
        return False

    @staticmethod
    def _is_unsupported(exc: Exception, name: str) -> bool:
        text = str(exc).lower()
        return name.lower() in text and ("attribute" in text or "not found" in text or "unexpected" in text)

    # -- the protocol --------------------------------------------------------

    async def join(self, chat_id: int) -> bool:
        if not await self.start():
            return False
        ok = await self._call("join", chat_id)
        if ok:
            self.joined.add(int(chat_id))
        return ok

    async def leave(self, chat_id: int) -> bool:
        ok = await self._call("leave", chat_id)
        self.joined.discard(int(chat_id))
        return ok

    async def play(self, chat_id: int, target: str, *, video: bool = False) -> bool:
        if self.call is None and not await self.start():
            return False
        media = self._media(target, video=video)
        if media is None:
            self._warn("MediaStream is unavailable in this py-tgcalls version")
            return False
        for name in ("play", "join_group_call", "change_stream"):
            method = getattr(self.call, name, None)
            if method is None:
                continue
            try:
                result = method(int(chat_id), media)
                if asyncio.iscoroutine(result):
                    await result
                self.joined.add(int(chat_id))
                return True
            except Exception as exc:
                if self._is_unsupported(exc, name):
                    continue
                self._warn(f"{name} failed: {type(exc).__name__}: {exc}")
                return False
        return False

    def _media(self, target: str, *, video: bool) -> Any:
        try:
            from pytgcalls.types import MediaStream
        except ImportError:
            try:  # py-tgcalls 2.x
                from pytgcalls.types.input_stream import AudioPiped

                return AudioPiped(target)
            except ImportError:
                return None
        try:
            kwargs: dict[str, Any] = {}
            parameters = self._audio_parameters()
            if parameters is not None:
                # only pass it when the release actually knows the enum
                kwargs["audio_parameters"] = parameters
            if video:
                kwargs["video_parameters"] = self._video_parameters()
            return MediaStream(target, **kwargs)
        except Exception:  # pragma: no cover - parameter names moved between releases
            try:
                return MediaStream(target)
            except Exception:
                return None

    def _audio_parameters(self) -> Any:
        """``VOICE_QUALITY`` mapped onto the enum this py-tgcalls release exports."""
        settings = getattr(self, "_settings", None)
        quality = str(getattr(settings, "voice_quality", "high") or "high").lower()
        try:
            from pytgcalls.types import AudioQuality
        except ImportError:
            return None
        name = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}.get(quality, "HIGH")
        return getattr(AudioQuality, name, None)

    def _video_parameters(self) -> Any:
        try:
            from pytgcalls.types import VideoQuality
        except ImportError:
            return None
        return getattr(VideoQuality, "HD_720p", None)

    async def pause(self, chat_id: int) -> bool:
        return await self._call("pause", chat_id)

    async def resume(self, chat_id: int) -> bool:
        return await self._call("resume", chat_id)

    async def seek(self, chat_id: int, seconds: int) -> bool:
        return await self._call("seek", chat_id, int(seconds))

    async def speed(self, chat_id: int, rate: float) -> bool:
        return await self._call("speed", chat_id, float(rate))

    async def mute(self, chat_id: int) -> bool:
        return await self._call("mute", chat_id)

    async def unmute(self, chat_id: int) -> bool:
        return await self._call("unmute", chat_id)

    async def volume(self, chat_id: int, percent: int) -> bool:
        return await self._call("volume", chat_id, int(percent))

    def status(self) -> BackendStatus:
        return BackendStatus(
            name=self.name,
            available=self.available,
            joined=sorted(self.joined),
            warnings=list(self.warnings),
            note="" if self.available else (self.warnings[-1] if self.warnings else "not started"),
        )


def build_backend(settings: Any, mtproto: Any = None) -> VoiceBackend:
    """Pick a backend: py-tgcalls when we can, the recorder otherwise."""
    mode = str(getattr(settings, "voice_backend", "auto") or "auto").lower()
    if mode == "off" or not getattr(settings, "voice_enabled", True):
        return NullBackend()
    if mode in ("auto", "pytgcalls"):
        backend = PyTgCallsBackend(mtproto)
        backend._settings = settings
        return backend
    return NullBackend()
