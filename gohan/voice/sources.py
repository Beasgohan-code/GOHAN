"""Turning a link or a search phrase into something playable.

The engine is `yt-dlp <https://github.com/yt-dlp/yt-dlp>`_, which also happens to
be the library that powers most "music bot" forks - including the Go original
this package is ported from, which shells out to the same ecosystem.

Design rules:

* **Nothing here hard-requires yt-dlp.** The import happens inside the
  functions; :func:`capabilities` reports what is missing so ``/voice`` can show
  an actionable card instead of an exception.
* **Everything blocking runs in a worker thread** (``asyncio.to_thread``), so a
  slow extraction never freezes the bot's update loop.
* **Never persist a stream URL.** ``googlevideo`` links are signed and expire
  within hours; they are resolved fresh for each playback and stripped from
  anything that goes to the database.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..logging_setup import get_logger
from .models import Track

log = get_logger("voice.sources")

__all__ = [
    "Capabilities",
    "SourceError",
    "capabilities",
    "download",
    "expand",
    "prepare",
    "related",
    "resolve",
    "search",
    "too_long",
]

#: yt-dlp format selectors per quality setting.
QUALITY_FORMATS = {
    "high": "bestaudio[abr<=320]/bestaudio/best",
    "medium": "bestaudio[abr<=160]/bestaudio/best",
    "low": "worstaudio/worst",
    "auto": "bestaudio/best",
}

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

#: host suffix → where the audio actually lives
_HOSTS = (
    ("youtube.com", "youtube"),
    ("youtu.be", "youtube"),
    ("music.youtube.com", "youtube-music"),
    ("soundcloud.com", "soundcloud"),
    ("spotify.com", "spotify"),
    ("bandcamp.com", "bandcamp"),
    ("vimeo.com", "vimeo"),
)


class SourceError(RuntimeError):
    """Raised when a link or query cannot be turned into a track."""


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What the media half of the bot can currently do."""

    yt_dlp: bool
    ffmpeg: bool
    cookies: bool
    pytgcalls: bool

    @property
    def ready(self) -> bool:
        """True when we can both find *and* stream audio."""
        return self.yt_dlp and self.pytgcalls

    @property
    def note(self) -> str:
        missing = [name for name, present in (("yt-dlp", self.yt_dlp), ("py-tgcalls", self.pytgcalls)) if not present]
        if missing:
            return f"{' and '.join(missing)} not installed (pip install 'gohan[voice]')"
        if not self.ffmpeg:
            return "ffmpeg is missing - live streams and format conversion may fail"
        return "everything installed"


def capabilities(settings: Any = None) -> Capabilities:
    """Probe the environment once - cheap enough to call per command."""
    return Capabilities(
        yt_dlp=importlib.util.find_spec("yt_dlp") is not None,
        ffmpeg=shutil.which("ffmpeg") is not None,
        cookies=_cookies_file(settings) is not None,
        pytgcalls=importlib.util.find_spec("pytgcalls") is not None,
    )


def _cookies_file(settings: Any) -> Path | None:
    """``YOUTUBE_COOKIES_FILE`` / ``COOKIES_FILE``, when it exists."""
    for attribute in ("cookies_file", "youtube_cookies_file"):
        raw = getattr(settings, attribute, None)
        if not raw:
            continue
        path = Path(str(raw)).expanduser()
        if path.is_file():
            return path
    return None


def detect_source(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for suffix, label in _HOSTS:
        if host.endswith(suffix):
            return label
    if url.startswith("tg://") or "t.me/" in url:
        return "telegram"
    return "url" if host else "unknown"


def _search_target(target: str) -> str:
    """A Spotify link cannot be extracted - search YouTube for its slug instead."""
    if detect_source(target) == "spotify":
        slug = urlparse(target).path.rstrip("/").split("/")[-1].replace("-", " ")
        if slug:
            log.info("spotify link mapped to a youtube search: %s", slug)
            return slug
    return target


def _base_options(settings: Any = None, *, quality: str | None = None) -> dict[str, Any]:
    settings_quality = getattr(settings, "voice_quality", "high") or "high"
    chosen = QUALITY_FORMATS.get(str(quality or settings_quality).lower(), QUALITY_FORMATS["high"])
    options: dict[str, Any] = {
        "format": chosen,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "socket_timeout": 15,
        "retries": 2,
        "extract_flat": False,
        "user_agent": _UA,
    }
    cookies = _cookies_file(settings)
    if cookies is not None:
        options["cookiefile"] = str(cookies)
    proxy = getattr(settings, "http_proxy", None) or getattr(settings, "proxy", None)
    if proxy:
        options["proxy"] = str(proxy)
    cache_dir = getattr(settings, "voice_cache_dir", None)
    if cache_dir:
        options["cachedir"] = str(cache_dir)
    return options


def _import_ytdlp() -> Any:
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover - exercised via capability card
        raise SourceError("yt-dlp is not installed - run: pip install 'gohan[media]'") from exc
    return yt_dlp


# ---------------------------------------------------------------------------
#  mapping yt-dlp payloads onto our Track
# ---------------------------------------------------------------------------


def _entry_to_track(entry: dict[str, Any], *, source: str | None = None) -> Track | None:
    if not entry or not isinstance(entry, dict):
        return None
    url = entry.get("webpage_url") or entry.get("url") or ""
    title = entry.get("title")
    if not title or not url:
        return None
    duration = entry.get("duration")
    try:
        duration = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    thumbnails = entry.get("thumbnails") or []
    thumbnail = entry.get("thumbnail")
    if not thumbnail and thumbnails:
        thumbnail = thumbnails[-1].get("url")
    return Track(
        title=str(title),
        url=str(url),
        duration=duration,
        video_id=str(entry.get("id")) if entry.get("id") else None,
        uploader=entry.get("uploader") or entry.get("channel") or entry.get("artist"),
        thumbnail=thumbnail,
        source=source or detect_source(str(url)) or "youtube",
        live=bool(entry.get("is_live")),
    )


# ---------------------------------------------------------------------------
#  the blocking work (always called through to_thread)
# ---------------------------------------------------------------------------


def _search_sync(query: str, limit: int, settings: Any) -> list[Track]:
    yt_dlp = _import_ytdlp()
    options = _base_options(settings)
    options.update({"extract_flat": "in_playlist", "skip_download": True})
    target = _search_target(query)
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"ytsearch{max(1, limit)}:{target}", download=False)
    tracks: list[Track] = []
    for entry in (info or {}).get("entries") or []:
        track = _entry_to_track(entry)
        if track is not None:
            tracks.append(track)
    return tracks


def _resolve_sync(target: str, settings: Any) -> Track:
    yt_dlp = _import_ytdlp()
    options = _base_options(settings)
    if not _looks_like_url(target):
        options.update({"extract_flat": "in_playlist", "skip_download": True})
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"ytsearch1:{target}", download=False)
        for entry in (info or {}).get("entries") or []:
            track = _entry_to_track(entry)
            if track is not None:
                return track
        raise SourceError(f"nothing found for {target!r}")

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(_search_target(target), download=False)
    if info is None:
        raise SourceError("that link could not be read")
    if "entries" in info:  # a playlist or channel link: take the first item
        for entry in info.get("entries") or []:
            track = _entry_to_track(entry)
            if track is not None:
                return track
        raise SourceError("that playlist is empty")
    track = _entry_to_track(info)
    if track is None:
        raise SourceError("that link has no playable audio")
    return track


def _expand_sync(target: str, settings: Any, limit: int) -> list[Track]:
    yt_dlp = _import_ytdlp()
    options = _base_options(settings)
    options.update({"extract_flat": "in_playlist", "skip_download": True, "noplaylist": False})
    options["playlistend"] = max(1, limit)
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(_search_target(target), download=False)
    if not info:
        raise SourceError("that link could not be read")
    entries = info.get("entries") if "entries" in info else [info]
    tracks = [t for t in (_entry_to_track(e) for e in entries or []) if t is not None]
    if not tracks:
        raise SourceError("that playlist is empty")
    return tracks


def _prepare_sync(track: Track, settings: Any, *, prefer_file: bool) -> str | None:
    """Return a playable target: a local path or a fresh direct stream URL."""
    if track.path and Path(track.path).is_file():
        return track.path
    if track.stream_url and not prefer_file:
        return track.stream_url

    yt_dlp = _import_ytdlp()
    options = _base_options(settings)
    options.update({"skip_download": True, "noplaylist": True})
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(_search_target(track.url), download=False)
    if not info:
        raise SourceError("could not open the stream")
    if "entries" in info:
        info = next((e for e in info.get("entries") or [] if e), info)
    direct = info.get("url")
    if direct and not prefer_file:
        track.stream_url = str(direct)
        return track.stream_url
    requested = info.get("requested_formats") or []
    if requested and requested[0].get("url") and not prefer_file:
        track.stream_url = str(requested[0]["url"])
        return track.stream_url

    # Live streams and DRM-free dash sources cannot be handed over as a URL:
    # download once and stream the file instead.
    path = _download_sync(track, settings, on_progress=None, max_mb=None)
    return str(path) if path else None


def _download_sync(
    track: Track,
    settings: Any,
    on_progress: Callable[[dict[str, Any]], None] | None,
    max_mb: int | None,
) -> Path | None:
    yt_dlp = _import_ytdlp()
    download_dir = Path(getattr(settings, "voice_download_dir", "data/downloads/voice"))
    download_dir.mkdir(parents=True, exist_ok=True)
    limit_mb = max_mb or int(getattr(settings, "max_download_mb", 2000) or 2000)

    def hook(payload: dict[str, Any]) -> None:
        if on_progress is None:
            return
        try:
            on_progress(payload)
        except Exception as exc:  # pragma: no cover - a broken callback must not kill the download
            log.debug("progress callback failed: %s", exc)

    options = _base_options(settings)
    options.update(
        {
            "outtmpl": str(download_dir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "max_filesize": limit_mb * 1024 * 1024,
        }
    )
    if on_progress is not None:
        options["progress_hooks"] = [hook]
    if shutil.which("ffmpeg"):
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(_search_target(track.url), download=True)
    if not info:
        return None
    if "entries" in info:
        info = next((e for e in info.get("entries") or [] if e), None) or {}
    downloads = info.get("requested_downloads") or []
    candidate: str | None = None
    if downloads and downloads[0].get("filepath"):
        candidate = downloads[0]["filepath"]
    elif info.get("filepath"):
        candidate = info["filepath"]
    elif info.get("id"):
        for path in download_dir.glob(f"{info['id']}.*"):
            candidate = str(path)
            break
    if not candidate:
        return None
    path = Path(candidate)
    if path.exists():
        track.path = str(path)
        if info.get("title") and not track.title:
            track.title = str(info["title"])
        return path
    return None


def _looks_like_url(text: str) -> bool:
    parsed = urlparse(str(text or ""))
    return bool(parsed.scheme in ("http", "https") and parsed.netloc) or str(text).startswith("tg://")


# ---------------------------------------------------------------------------
#  async facade
# ---------------------------------------------------------------------------


async def search(query: str, *, limit: int = 5, settings: Any = None) -> list[Track]:
    """Search YouTube and return up to ``limit`` candidates."""
    text = str(query or "").strip()
    if not text:
        raise SourceError("give me something to search for")
    if _looks_like_url(text):
        return [await resolve(text, settings=settings)]
    return await asyncio.to_thread(_search_sync, text, max(1, min(limit, 10)), settings)


async def resolve(target: str, *, settings: Any = None) -> Track:
    """A link or a phrase → exactly one playable track."""
    text = str(target or "").strip()
    if not text:
        raise SourceError("give me a link or a song name")
    return await asyncio.to_thread(_resolve_sync, text, settings)


async def expand(target: str, *, settings: Any = None, limit: int = 50) -> list[Track]:
    """A playlist / album / channel link → its tracks (``/playlist <url>``)."""
    text = str(target or "").strip()
    if not text:
        raise SourceError("give me a playlist link")
    return await asyncio.to_thread(_expand_sync, text, settings, max(1, min(limit, 200)))


async def related(track: Track, *, settings: Any = None) -> Track | None:
    """Autoplay: the next thing to play when the queue runs dry."""
    query = f"{track.title} {track.uploader or ''}".strip()
    try:
        candidates = await search(query, limit=5, settings=settings)
    except SourceError:
        return None
    for candidate in candidates:
        if track.video_id and candidate.video_id == track.video_id:
            continue
        return candidate
    return candidates[0] if candidates else None


async def prepare(track: Track, *, settings: Any = None, prefer_file: bool = False) -> str | None:
    """A yt-dlp-resolved target the voice backend can play."""
    return await asyncio.to_thread(_prepare_sync, track, settings, prefer_file)


async def download(
    track: Track,
    *,
    settings: Any = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    max_mb: int | None = None,
) -> Path | None:
    """Fetch the audio to ``data/downloads/voice`` (used by ``/song`` and live)."""
    return await asyncio.to_thread(_download_sync, track, settings, on_progress, max_mb)


def too_long(track: Track, minutes: int) -> bool:
    """False when the track is allowed under the per-chat duration limit."""
    if not minutes or minutes <= 0 or track.live or not track.duration:
        return False
    return track.duration > minutes * 60


def youtube_id(url: str) -> str | None:
    """VID from any YouTube URL shape (watch, youtu.be, shorts, music)."""
    parsed = urlparse(str(url or ""))
    if parsed.hostname and parsed.hostname.endswith("youtu.be"):
        return parsed.path.lstrip("/") or None
    values = parse_qs(parsed.query).get("v")
    if values:
        return values[0]
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live"):
        return parts[1]
    return None


def thumbnail_for(track: Track) -> str | None:
    """A cover image URL, derived from the video id when yt-dlp gave us none."""
    if track.thumbnail:
        return track.thumbnail
    vid = track.video_id or youtube_id(track.url)
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None


def cleanup_dir(settings: Any = None, *, keep_hours: float = 6.0) -> int:
    """Drop downloaded audio older than ``keep_hours`` (the watchdog calls this)."""
    directory = Path(getattr(settings, "voice_download_dir", "data/downloads/voice"))
    if not directory.is_dir():
        return 0
    import time

    cutoff = time.time() - keep_hours * 3600
    removed = 0
    for path in directory.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                os.remove(path)
                removed += 1
        except OSError:  # pragma: no cover - best effort
            continue
    return removed
