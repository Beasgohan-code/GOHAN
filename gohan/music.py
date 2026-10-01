"""Music: search YouTube, then deliver the audio track.

This is deliberately *progressive*:

* ``/music <query>``   always works - it renders a rich result list built from
  yt-dlp's search (or, without yt-dlp, plain YouTube search links).
* ``/song <query>``    downloads the best match and sends it as an audio file.
  Up to ``MAX_DOWNLOAD_MB`` through the Bot API (50 MB) or the full 2 GB when
  the MTProto bridge is connected.

Downloads run in a worker thread so the bot keeps answering while yt-dlp works.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, FSInputFile, Message

from .filters import RateLimit
from .logging_setup import get_logger
from .rich import keys
from .rich import ui
from .rich.sender import rich_reply, rich_send
from .storage import Database

log = get_logger("music")

router = Router(name="music")

DOWNLOAD_DIR = Path("data/downloads/music")
#: Bot API upload ceiling is 50 MB; leave headroom for the container overhead
BOT_API_LIMIT_MB = 45

music_limit = RateLimit(3.0, message="one search at a time 🎧")
song_limit = RateLimit(8.0, message="let the previous download finish 🎧")

#: (chat_id, message_id) -> search results, so the buttons can resolve a choice
_results: dict[tuple[int, int], list["Track"]] = {}


@dataclass(slots=True)
class Track:
    title: str
    url: str
    duration: int | None = None
    uploader: str | None = None
    thumbnail: str | None = None
    video_id: str | None = None

    @property
    def duration_text(self) -> str:
        if not self.duration:
            return "—"
        minutes, seconds = divmod(int(self.duration), 60)
        return f"{minutes}:{seconds:02d}"


def ytdlp_available() -> bool:
    try:
        import yt_dlp  # noqa: F401

        return True
    except Exception:
        return False


def _search_sync(query: str, limit: int = 6) -> list[Track]:
    """Blocking yt-dlp search - always called through :func:`to_thread`."""
    from yt_dlp import YoutubeDL

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": True,
        "default_search": "ytsearch",
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)

    tracks: list[Track] = []
    for entry in (info or {}).get("entries", []) or []:
        if not entry:
            continue
        video_id = entry.get("id")
        tracks.append(
            Track(
                title=entry.get("title") or "unknown",
                url=entry.get("url") or f"https://youtu.be/{video_id}",
                duration=entry.get("duration"),
                uploader=entry.get("uploader") or entry.get("channel"),
                thumbnail=entry.get("thumbnail"),
                video_id=video_id,
            )
        )
    return tracks


def _fallback_tracks(query: str) -> list[Track]:
    """No yt-dlp? Offer YouTube search links - the feature still works."""
    slug = re.sub(r"\s+", "+", query.strip())
    return [
        Track(title=f"YouTube search: {query}", url=f"https://www.youtube.com/results?search_query={slug}"),
        Track(title=f"YouTube Music: {query}", url=f"https://music.youtube.com/search?q={slug}"),
    ]


async def search_tracks(query: str, limit: int = 6) -> list[Track]:
    if ytdlp_available():
        try:
            tracks = await asyncio.to_thread(_search_sync, query, limit)
            if tracks:
                return tracks
        except Exception as exc:
            log.warning("yt-dlp search failed: %s", exc)
    return _fallback_tracks(query)


def _download_sync(url: str, max_mb: int) -> Path | None:
    """Download the best audio track as m4a/mp3 - blocking, runs in a thread."""
    from yt_dlp import YoutubeDL

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio[filesize<{}M]/bestaudio/best".format(max_mb),
        "outtmpl": str(DOWNLOAD_DIR / "%(title).80s-%(id)s.%(ext)s"),
        "max_filesize": max_mb * 1024 * 1024,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
        "postprocessor_args": ["-metadata", "title=%(title)s"],
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
    if not info:
        return None
    path = info.get("requested_downloads", [{}])[0].get("filepath") or info.get("_filename")
    candidate = Path(path) if path else None
    if candidate and candidate.exists():
        return candidate
    # the postprocessor may have changed the extension
    if candidate:
        for suffix in (".mp3", ".m4a", ".opus", ".webm"):
            swapped = candidate.with_suffix(suffix)
            if swapped.exists():
                return swapped
    stem = str(info.get("id") or "")
    for found in DOWNLOAD_DIR.glob(f"*{stem}*"):
        return found
    return None


def _cleanup(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def track_list(tracks: list[Track], *, query: str) -> str:
    """The rich list of search results."""
    rows = [
        (f"{index}. {track.title[:70]}", f"{track.duration_text} · {track.uploader or '—'}")
        for index, track in enumerate(tracks[:6], start=1)
    ]
    return ui.screen(
        "music",
        icon="clapper",
        subtitle=query,
        blocks_=[
            ui.table(["ᴛʀᴀᴄᴋ", "ʟᴇɴɢᴛʜ · ᴀʀᴛɪꜱᴛ"], rows),
            ui.italic("ᴀ ʀᴇᴀʟ ᴛʀᴀᴄᴋ ɴᴇᴇᴅꜱ <code>yt-dlp</code> + <code>ffmpeg</code> ᴏɴ ᴛʜᴇ ꜱᴇʀᴠᴇʀ"),
        ]
        if not ytdlp_available()
        else ui.italic("ᴛᴀᴘ ᴀ ɴᴜᴍʙᴇʀ ᴛᴏ ɢᴇᴛ ᴛʜᴇ ᴀᴜᴅɪᴏ"),
    )


def track_buttons(tracks: list[Track], *, enabled: bool) -> Any:
    numbers = [
        keys.button(
            str(index),
            callback=f"music:get:{index - 1}",
            style="primary",
            icon="play",
            disabled=not enabled,
        )
        for index in range(1, min(len(tracks), 6) + 1)
    ]
    rows = [numbers[i : i + 3] for i in range(0, len(numbers), 3)]
    links = [
        keys.button("Open on YouTube", url=tracks[0].url, icon="link") if tracks else None,
    ]
    return keys.keyboard(*rows, *([[b for b in links if b]] if any(links) else []))


@router.message(Command(commands=["music", "song", "play"]), music_limit)
async def cmd_music(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """``/music <query>`` - search and show playable results."""
    query = (command.args or "").strip() or "lofi beats"
    tracks = await search_tracks(query, limit=6)
    if not tracks:
        await rich_reply(message, ui.panel(ui.no("nothing found"), icon="question"))
        return

    await db.log_event("MusicSearch", chat_id=message.chat.id, data={"q": query[:80]})
    sent = await rich_send(
        bot,
        message.chat.id,
        track_list(tracks, query=query),
        reply_markup=track_buttons(tracks, enabled=ytdlp_available()),
    )
    if sent is not None:
        _results[(sent.chat.id, sent.message_id)] = tracks
        if len(_results) > 200:  # keep the cache tiny
            _results.clear()
            _results[(sent.chat.id, sent.message_id)] = tracks


@router.message(Command(commands=["songfile", "download"]), song_limit)
async def cmd_song(message: Message, command: CommandObject, bot: Bot, db: Database, mtproto: Any = None) -> None:
    """``/download <query>`` - fetch the audio and send it."""
    query = (command.args or "").strip()
    if not query:
        await rich_reply(message, ui.panel("ᴜꜱᴀɢᴇ: <code>/download kesariya</code>", icon="clapper"))
        return
    if not ytdlp_available():
        await rich_reply(
            message,
            ui.panel(
                ui.no("yt-dlp is not installed")
                + "\n\n<code>pip install yt-dlp</code> "
                + ui.italic("(ffmpeg is needed too for mp3 conversion)"),
                icon="warning",
            ),
        )
        return

    tracks = await search_tracks(query, limit=1)
    if not tracks:
        await rich_reply(message, ui.panel(ui.no("nothing found"), icon="question"))
        return

    status = await rich_send(
        bot,
        message.chat.id,
        ui.panel(ui.italic(f"downloading <b>{ui.esc(tracks[0].title[:60])}</b>…"), icon="clapper"),
    )

    max_mb = 2000 if (mtproto is not None and mtproto.status.available) else BOT_API_LIMIT_MB
    try:
        path = await asyncio.to_thread(_download_sync, tracks[0].url, max_mb)
    except Exception as exc:
        log.warning("download failed: %s", exc)
        path = None

    if path is None or not path.exists():
        if status is not None:
            await rich_send(
                bot,
                message.chat.id,
                ui.panel(
                    ui.no("download failed")
                    + "\n"
                    + ui.italic("the track may be region-locked, too large, or ffmpeg is missing"),
                    icon="warning",
                ),
            )
        return

    size_mb = path.stat().st_size / 1_048_576
    delivered = False
    try:
        if size_mb <= BOT_API_LIMIT_MB:
            await bot.send_audio(
                message.chat.id,
                FSInputFile(path, filename=path.name),
                title=tracks[0].title[:64],
                performer=(tracks[0].uploader or "GOHAN")[:64],
                duration=int(tracks[0].duration or 0) or None,
            )
            delivered = True
        elif mtproto is not None and mtproto.status.available:
            # MTProto lifts the 50 MB cap - bots can send up to 2 GB
            chat = await bot.get_chat(message.chat.id)
            await mtproto.client.send_audio(
                chat.id,
                str(path),
                title=tracks[0].title[:64],
                performer=(tracks[0].uploader or "GOHAN")[:64],
            )
            delivered = True
    except Exception as exc:
        log.warning("audio send failed: %s", exc)

    if delivered and status is not None:
        try:
            await bot.delete_message(message.chat.id, status.message_id)
        except Exception:
            pass
    elif not delivered:
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(
                ui.no(f"the file is {size_mb:.0f} MB - too big for the Bot API")
                + "\n"
                + ui.italic("connect MTProto to send files up to 2 GB"),
                icon="satellite",
            ),
        )
    _cleanup(path)
    await db.log_event("MusicDownload", chat_id=message.chat.id, data={"q": query[:80], "mb": round(size_mb, 1)})


@router.callback_query(F.data.startswith("music:"))
async def on_music_callback(callback: CallbackQuery, bot: Bot, db: Database, mtproto: Any = None) -> None:
    """Number buttons under a search result."""
    if callback.message is None:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    if len(parts) < 3 or parts[1] != "get":
        await callback.answer()
        return

    tracks = _results.get((callback.message.chat.id, callback.message.message_id))
    index = int(parts[2])
    if not tracks or index >= len(tracks):
        await callback.answer("search again - this list expired", show_alert=True)
        return
    if not ytdlp_available():
        await callback.answer("yt-dlp is not installed on the server", show_alert=True)
        return

    track = tracks[index]
    await callback.answer(f"downloading {track.title[:30]}…")

    usage: Message | None = callback.message  # type: ignore[assignment]
    max_mb = 2000 if (mtproto is not None and mtproto.status.available) else BOT_API_LIMIT_MB
    try:
        path = await asyncio.to_thread(_download_sync, track.url, max_mb)
    except Exception as exc:
        log.warning("callback download failed: %s", exc)
        path = None

    chat_id = callback.message.chat.id
    if path is None or not path.exists():
        await rich_send(bot, chat_id, ui.panel(ui.no("could not fetch that track"), icon="warning"))
        return

    size_mb = path.stat().st_size / 1_048_576
    try:
        if size_mb <= BOT_API_LIMIT_MB:
            await bot.send_audio(
                chat_id,
                FSInputFile(path, filename=path.name),
                title=track.title[:64],
                performer=(track.uploader or "GOHAN")[:64],
                duration=int(track.duration or 0) or None,
            )
        elif mtproto is not None and mtproto.status.available:
            chat = await bot.get_chat(chat_id)
            await mtproto.client.send_audio(chat.id, str(path), title=track.title[:64])
        else:
            await rich_send(
                bot,
                chat_id,
                ui.panel(ui.no(f"{size_mb:.0f} MB - too big without MTProto"), icon="satellite"),
            )
    finally:
        _cleanup(path)
    await db.log_event("MusicDownload", chat_id=chat_id, data={"title": track.title[:80]})


def disk_usage() -> dict[str, Any]:
    """Used by the watchdog report."""
    if not DOWNLOAD_DIR.exists():
        return {"files": 0, "mb": 0.0}
    files = list(DOWNLOAD_DIR.glob("*"))
    total = sum(f.stat().st_size for f in files if f.is_file())
    return {"files": len(files), "mb": round(total / 1_048_576, 1), "since": time.time()}
