"""Named playlists (``/createplaylist``, ``/addtoplaylist`` …).

A playlist belongs to the person who created it and holds an ordered list of
tracks. The service is intentionally strict about names and sizes: a bot in a
busy group should not let one member create ten thousand entries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from .models import Track, format_duration

log = get_logger("voice.playlists")

__all__ = ["PlaylistError", "PlaylistInfo", "PlaylistService", "clean_name", "MAX_PLAYLISTS", "MAX_TRACKS"]

#: Per owner, and per playlist.
MAX_PLAYLISTS = 25
MAX_TRACKS = 200

_NAME_RE = re.compile(r"[^\w \-.'()\[\]]+", re.UNICODE)


class PlaylistError(RuntimeError):
    """Something the user should read and fix (name taken, playlist missing…)."""


def clean_name(raw: str) -> str:
    """Trim, drop control characters and cap the length."""
    text = _NAME_RE.sub("", str(raw or "").replace("\n", " ")).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return text[:40]


@dataclass(slots=True)
class PlaylistInfo:
    """A playlist plus everything the cards want to show."""

    id: int
    name: str
    owner_id: int
    tracks: int = 0
    is_public: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0
    duration: int = 0
    owner_name: str | None = None
    sample: list[str] = field(default_factory=list)

    @property
    def duration_text(self) -> str:
        return format_duration(self.duration)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "owner_id": self.owner_id,
            "owner_name": self.owner_name,
            "tracks": self.tracks,
            "is_public": self.is_public,
            "duration": self.duration,
            "duration_text": self.duration_text,
            "sample": list(self.sample),
        }

    @classmethod
    def from_row(cls, row: Any) -> PlaylistInfo:
        keys = row.keys()

        def value(name: str, default: Any = None) -> Any:
            return row[name] if name in keys else default

        return cls(
            id=int(row["id"]),
            name=str(row["name"]),
            owner_id=int(row["owner_id"]),
            tracks=int(value("tracks", 0) or 0),
            is_public=bool(value("is_public", 0)),
            created_at=float(value("created_at", 0.0) or 0.0),
            updated_at=float(value("updated_at", 0.0) or 0.0),
            duration=int(value("duration", 0) or 0),
            owner_name=value("first_name") or value("username"),
        )


class PlaylistService:
    """CRUD around ``playlists``/``playlist_tracks``."""

    def __init__(self, db: Any, *, max_playlists: int = MAX_PLAYLISTS, max_tracks: int = MAX_TRACKS) -> None:
        self.db = db
        self.max_playlists = int(max_playlists)
        self.max_tracks = int(max_tracks)

    # -- reads ---------------------------------------------------------------

    async def listing(self, owner_id: int) -> list[PlaylistInfo]:
        rows = await self.db.playlists_of(owner_id)
        return [PlaylistInfo.from_row(row) for row in rows]

    async def info(self, playlist_id: int) -> PlaylistInfo | None:
        row = await self.db.playlist(playlist_id)
        if row is None:
            return None
        info = PlaylistInfo.from_row(row)
        info.tracks = await self.db.playlist_track_count(playlist_id)
        info.duration = int(
            await self.db.fetch_value(
                "SELECT COALESCE(SUM(duration), 0) FROM playlist_tracks WHERE playlist_id = ?",
                (playlist_id,),
                0,
            )
        )
        return info

    async def find(self, owner_id: int, name_or_id: str) -> PlaylistInfo:
        """``name_or_id`` may be a number (the card shows it) or the name."""
        text = str(name_or_id or "").strip()
        row = None
        if text.isdigit():
            row = await self.db.playlist(int(text))
            if row is not None and int(row["owner_id"]) != int(owner_id):
                row = None
        if row is None:
            row = await self.db.find_playlist(owner_id, clean_name(text).lower())
        if row is None:
            raise PlaylistError(f"you have no playlist called {text!r}")
        info = PlaylistInfo.from_row(row)
        info.tracks = await self.db.playlist_track_count(info.id)
        return info

    async def tracks(self, playlist_id: int, *, limit: int = 500) -> list[Track]:
        rows = await self.db.playlist_tracks(playlist_id, limit=limit)
        return [Track.from_row(row) for row in rows]

    async def summary(self) -> list[PlaylistInfo]:
        """Every playlist in the database (the web panel's view)."""
        rows = await self.db.all_playlists()
        return [PlaylistInfo.from_row(row) for row in rows]

    # -- writes --------------------------------------------------------------

    async def create(self, owner_id: int, name: str, *, is_public: bool = False) -> PlaylistInfo:
        cleaned = clean_name(name)
        if not cleaned:
            raise PlaylistError("give the playlist a name")
        existing = await self.db.playlists_of(owner_id, limit=self.max_playlists + 50)
        if len(existing) >= self.max_playlists:
            raise PlaylistError(f"you already have {self.max_playlists} playlists")
        if await self.db.find_playlist(owner_id, cleaned.lower()) is not None:
            raise PlaylistError(f"you already have a playlist called {cleaned!r}")
        playlist_id = await self.db.create_playlist(owner_id, cleaned, is_public=is_public)
        log.info("playlist %s created by %s (%s)", playlist_id, owner_id, cleaned)
        info = await self.info(playlist_id)
        if info is None:  # pragma: no cover - the row was just written
            raise PlaylistError("could not create that playlist")
        return info

    async def delete(self, owner_id: int, name_or_id: str) -> PlaylistInfo:
        info = await self.find(owner_id, name_or_id)
        removed = await self.db.delete_playlist(info.id)
        log.info("playlist %s deleted (%s tracks)", info.id, removed)
        info.tracks = removed
        return info

    async def rename(self, owner_id: int, name_or_id: str, new_name: str) -> PlaylistInfo:
        info = await self.find(owner_id, name_or_id)
        cleaned = clean_name(new_name)
        if not cleaned:
            raise PlaylistError("the new name is empty")
        clash = await self.db.find_playlist(owner_id, cleaned.lower())
        if clash is not None and int(clash["id"]) != info.id:
            raise PlaylistError(f"you already have a playlist called {cleaned!r}")
        await self.db.rename_playlist(info.id, cleaned)
        info.name = cleaned
        return info

    async def add_tracks(
        self, owner_id: int, name_or_id: str, tracks: list[Track], *, replace: bool = False
    ) -> tuple[PlaylistInfo, int]:
        info = await self.find(owner_id, name_or_id)
        if replace:
            await self.db.clear_playlist(info.id)
        current = await self.db.playlist_track_count(info.id)
        room = max(0, self.max_tracks - current)
        if room <= 0:
            raise PlaylistError(f"{info.name!r} is full ({self.max_tracks} tracks)")
        payload = [track.to_dict() for track in tracks[:room]]
        added = await self.db.add_playlist_tracks(info.id, payload)
        info.tracks = current + added
        return info, added

    async def remove_track(self, owner_id: int, name_or_id: str, position: int) -> tuple[PlaylistInfo, Track]:
        info = await self.find(owner_id, name_or_id)
        row = await self.db.remove_playlist_track(info.id, position)
        if row is None:
            raise PlaylistError(f"{info.name!r} has no track #{position}")
        info.tracks = await self.db.playlist_track_count(info.id)
        return info, Track.from_row(row)

    async def clear(self, owner_id: int, name_or_id: str) -> tuple[PlaylistInfo, int]:
        info = await self.find(owner_id, name_or_id)
        removed = await self.db.clear_playlist(info.id)
        info.tracks = 0
        return info, removed
