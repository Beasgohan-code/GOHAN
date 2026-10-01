"""Raid detection and automatic lockdown.

A "raid" is a burst of joins from accounts that then spam. Guarding against it
happens in two stages:

1. :class:`RaidDetector` watches join events in a sliding window. A burst above
   ``join_threshold`` inside ``window`` seconds flips the chat into
   :class:`RaidState.LOCKDOWN`.
2. While locked down, joins are *declined* (join requests) or new members are
   muted on arrival and reported to admins, and every suspicious message is
   deleted rather than flagged.

Admins lift it with ``/lockdown off`` (or it expires on its own after
``auto_release_minutes``, so a raid cannot lock a group forever by accident).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

__all__ = ["RaidDetector", "RaidEvent", "RaidState"]


class RaidState(str, Enum):
    """How guarded a chat currently is."""

    NORMAL = "normal"
    ALERT = "alert"        # suspicious, watching closely
    LOCKDOWN = "lockdown"  # auto-restrict everything


@dataclass(slots=True)
class RaidEvent:
    """One join, kept for scoring."""

    user_id: int
    at: float
    had_username: bool = True
    had_photo: bool = False
    is_premium: bool = False


@dataclass(slots=True)
class ChatRaid:
    joins: deque[RaidEvent] = field(default_factory=deque)
    state: RaidState = RaidState.NORMAL
    since: float = 0.0
    released_at: float = 0.0
    declines: int = 0

    def is_locked(self, auto_release_minutes: float = 30.0) -> bool:
        if self.state is not RaidState.LOCKDOWN:
            return False
        if self.since and (time.monotonic() - self.since) > auto_release_minutes * 60:
            return False  # expired, the detector will reset it
        return True


class RaidDetector:
    """Sliding-window join analyser, one entry per chat."""

    def __init__(
        self,
        *,
        window: float = 60.0,
        join_threshold: int = 8,
        alert_threshold: int = 4,
        auto_release_minutes: float = 30.0,
    ) -> None:
        self.window = window
        self.join_threshold = join_threshold
        self.alert_threshold = alert_threshold
        self.auto_release_minutes = auto_release_minutes
        self._chats: dict[int, ChatRaid] = {}

    # -- inspection ----------------------------------------------------------

    def chat(self, chat_id: int) -> ChatRaid:
        state = self._chats.get(chat_id)
        if state is None:
            state = ChatRaid()
            self._chats[chat_id] = state
        return state

    def state_of(self, chat_id: int) -> RaidState:
        entry = self.chat(chat_id)
        if entry.state is RaidState.LOCKDOWN and not entry.is_locked(self.auto_release_minutes):
            entry.state = RaidState.NORMAL
            entry.since = 0.0
        return entry.state

    def joins_in_window(self, chat_id: int) -> int:
        entry = self.chat(chat_id)
        cutoff = time.monotonic() - self.window
        while entry.joins and entry.joins[0].at < cutoff:
            entry.joins.popleft()
        return len(entry.joins)

    # -- recording -----------------------------------------------------------

    def record_join(
        self,
        chat_id: int,
        user_id: int,
        *,
        had_username: bool = True,
        had_photo: bool = False,
        is_premium: bool = False,
    ) -> RaidState:
        """Record a join and return the resulting state."""
        entry = self.chat(chat_id)
        entry.joins.append(
            RaidEvent(
                user_id=user_id,
                at=time.monotonic(),
                had_username=had_username,
                had_photo=had_photo,
                is_premium=is_premium,
            )
        )
        recent = self.joins_in_window(chat_id)
        if recent >= self.join_threshold:
            if entry.state is not RaidState.LOCKDOWN:
                entry.state = RaidState.LOCKDOWN
                entry.since = time.monotonic()
        elif recent >= self.alert_threshold and entry.state is RaidState.NORMAL:
            entry.state = RaidState.ALERT
        return entry.state

    def lock(self, chat_id: int) -> None:
        entry = self.chat(chat_id)
        entry.state = RaidState.LOCKDOWN
        entry.since = time.monotonic()

    def release(self, chat_id: int) -> None:
        entry = self.chat(chat_id)
        entry.state = RaidState.NORMAL
        entry.since = 0.0
        entry.released_at = time.monotonic()
        entry.joins.clear()

    # -- scoring -------------------------------------------------------------

    def suspicion(self, chat_id: int) -> float:
        """0.0 - 1.0: how raid-like the recent joins look.

        Fresh accounts (no photo, no username) joining in a burst is the classic
        signature; the score is the burst size scaled by how anonymous they are.
        """
        entry = self.chat(chat_id)
        cutoff = time.monotonic() - self.window
        recent = [j for j in entry.joins if j.at >= cutoff]
        if not recent:
            return 0.0
        anonymous = sum(1 for j in recent if not j.had_photo or not j.had_username)
        burst = min(1.0, len(recent) / max(1, self.join_threshold))
        return round(min(1.0, burst * (0.5 + 0.5 * anonymous / len(recent))), 2)

    def summary(self, chat_id: int) -> dict[str, object]:
        entry = self.chat(chat_id)
        return {
            "state": self.state_of(chat_id).value,
            "joins_window": self.joins_in_window(chat_id),
            "suspicion": self.suspicion(chat_id),
            "locked_s": int(time.monotonic() - entry.since) if entry.since else 0,
            "declines": entry.declines,
        }

    def forget(self, chat_id: int) -> None:
        self._chats.pop(chat_id, None)
