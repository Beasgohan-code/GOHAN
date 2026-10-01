"""Message heuristics.

Pure functions over a message's *content*, so they can be tested without a bot
and tuned without fear. Each detector returns a :class:`Verdict` with a
confidence score; :func:`inspect_message` combines them into one
:class:`SpamVerdict` that handlers act on.

Scoring model
-------------
* every hit adds to a score (higher = more certain),
* ``score >= 100`` → delete immediately,
* ``score >= 60`` → delete only if the author is not a trusted member,
* anything below → log for the admins.

That "trusted member" escape hatch matters: a long-time active member posting a
link is not a raid.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

from ..content import SCAM_PATTERNS, SPAM_PHRASES

__all__ = [
    "LinkVerdict",
    "SpamVerdict",
    "Verdict",
    "inspect_message",
    "is_forward_from_channel",
    "normalize",
]

_LINK_RE = re.compile(r"(?:https?://|www\.|t\.me/|\b[a-z0-9-]+\.(?:com|net|org|ru|in|io|xyz|top|club|vip|site|online|app|me)\b)", re.I)
_MENTION_RE = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,}")
_CURRENCY_RE = re.compile(r"(?:\$|₹|€|₿|usdt|btc|eth)\s?\d", re.I)
_INVITE_RE = re.compile(r"(?:t\.me/\+|joinchat/|t\.me/joinchat)", re.I)
_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff"


class Verdict(str, Enum):
    """What the guardian thinks about a message."""

    CLEAN = "clean"
    SUSPECT = "suspect"
    SPAM = "spam"


def normalize(text: str) -> str:
    """Fold the tricks spammers use to dodge regexes.

    * NFKC-normalise (so ``ᶠʳᵉᵉ`` → ``free``),
    * strip zero-width characters (``b\u200bitcoin``),
    * collapse repeated punctuation and spaces.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text)
    for char in _ZERO_WIDTH:
        folded = folded.replace(char, "")
    folded = re.sub(r"([.!*_~\-=|])\1{2,}", r"\1", folded)
    return re.sub(r"\s+", " ", folded).strip().lower()


@dataclass(slots=True)
class LinkVerdict:
    """Link analysis."""

    has_link: bool = False
    invite: bool = False
    count: int = 0
    domains: list[str] = field(default_factory=list)


def inspect_links(text: str) -> LinkVerdict:
    """Find URLs, invite links and the domains behind them."""
    raw = text or ""
    matches = _LINK_RE.findall(raw)
    domains: list[str] = []
    for match in re.finditer(r"https?://([^/\s]+)|www\.([^/\s]+)", raw, re.I):
        domain = (match.group(1) or match.group(2) or "").lower()
        if domain and domain not in domains:
            domains.append(domain)
    return LinkVerdict(
        has_link=bool(matches),
        invite=bool(_INVITE_RE.search(raw)),
        count=len(matches),
        domains=domains[:5],
    )


def is_forward_from_channel(message) -> bool:
    """True when a message is forwarded from a channel (common spam vector)."""
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        # Older/newer field names across Bot API versions.
        return bool(getattr(message, "forward_from_chat", None))
    return getattr(origin, "type", None) == "channel" or getattr(origin, "chat", None) is not None


@dataclass(slots=True)
class SpamVerdict:
    """The combined verdict for one message."""

    verdict: Verdict = Verdict.CLEAN
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    links: LinkVerdict = field(default_factory=LinkVerdict)

    @property
    def should_delete(self) -> bool:
        return self.score >= 100

    @property
    def should_flag(self) -> bool:
        return self.score >= 60

    @property
    def reason_text(self) -> str:
        return ", ".join(self.reasons) or "clean"


# weights: how suspicious each signal is on its own
_W_SCAM_PATTERN = 55
_W_SPAM_PHRASE = 70
_W_INVITE_LINK = 45
_W_MANY_LINKS = 40
_W_CRYPTO_LINK = 35
_W_CHANNEL_FORWARD = 30
_W_CURRENCY_BAIT = 30
_W_MENTION_BLAST = 35


def inspect_message(
    text: str | None,
    *,
    caption: str | None = None,
    from_channel: bool = False,
    is_trusted: bool = False,
) -> SpamVerdict:
    """Score one message.

    ``is_trusted`` (a known active member) halves link-related penalties, so
    regulars are not punished for sharing a link.
    """
    body = " ".join(filter(None, (text, caption)))
    verdict = SpamVerdict(links=inspect_links(body))
    if not body.strip():
        return verdict

    flat = normalize(body)
    score = 0

    for pattern in SCAM_PATTERNS:
        if re.search(pattern, flat):
            score += _W_SCAM_PATTERN
            verdict.reasons.append("scam pattern")
            break

    for phrase in SPAM_PHRASES:
        if phrase in flat:
            score += _W_SPAM_PHRASE
            verdict.reasons.append(f'spam phrase "{phrase}"')
            break

    link_weight = 0.5 if is_trusted else 1.0
    if verdict.links.invite:
        score += int(_W_INVITE_LINK * link_weight)
        verdict.reasons.append("invite link")
    if verdict.links.count >= 3:
        score += int(_W_MANY_LINKS * link_weight)
        verdict.reasons.append(f"{verdict.links.count} links")
    if verdict.links.domains and any(
        d in {"1xbet.com", "mostbet.com", "melbet.com"} for d in verdict.links.domains
    ):
        score += _W_CRYPTO_LINK
        verdict.reasons.append("gambling domain")

    if _CURRENCY_RE.search(flat) and (verdict.links.has_link or "profit" in flat):
        score += _W_CURRENCY_BAIT
        verdict.reasons.append("money bait")

    if from_channel and verdict.links.has_link:
        score += _W_CHANNEL_FORWARD
        verdict.reasons.append("channel forward with link")

    mentions = _MENTION_RE.findall(body)
    if len(mentions) >= 5:
        score += _W_MENTION_BLAST
        verdict.reasons.append(f"{len(mentions)} mentions")

    verdict.score = score
    if score >= 100:
        verdict.verdict = Verdict.SPAM
    elif score >= 60:
        verdict.verdict = Verdict.SUSPECT
    else:
        verdict.verdict = Verdict.CLEAN
    return verdict


# ---------------------------------------------------------------------------
#  flood detection
# ---------------------------------------------------------------------------


class FloodTracker:
    """Sliding-window message counter per user, per chat.

    Telegram itself drops messages that arrive faster than ~1/s per chat, so this
    only needs to catch *bursts* (a flooded wall of text or a joining raid).
    """

    __slots__ = ("window", "limit", "_hits", "_mtime")

    def __init__(self, window: float = 8.0, limit: int = 7) -> None:
        self.window = window
        self.limit = limit
        self._hits: dict[tuple[int, int], list[float]] = {}
        self._mtime = 0.0

    def hit(self, chat_id: int, user_id: int) -> tuple[bool, int]:
        """Record a message. Returns ``(is_flooding, count_in_window)``."""
        now = time.monotonic()
        key = (chat_id, user_id)
        stamps = [t for t in self._hits.get(key, ()) if now - t < self.window]
        stamps.append(now)
        self._hits[key] = stamps

        if now - self._mtime > 120:  # periodic sweep keeps memory bounded
            self._mtime = now
            for stale in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window * 4]:
                self._hits.pop(stale, None)
        return len(stamps) >= self.limit, len(stamps)

    def forget(self, chat_id: int, user_id: int) -> None:
        self._hits.pop((chat_id, user_id), None)


class DuplicateTracker:
    """Detects the same (normalized) text being pasted repeatedly."""

    __slots__ = ("window", "limit", "_seen")

    def __init__(self, window: float = 30.0, limit: int = 3) -> None:
        self.window = window
        self.limit = limit
        self._seen: dict[tuple[int, int, str], list[float]] = {}

    def check(self, chat_id: int, user_id: int, text: str) -> bool:
        flat = normalize(text)
        if len(flat) < 12:
            return False
        now = time.monotonic()
        key = (chat_id, user_id, flat[:200])
        stamps = [t for t in self._seen.get(key, ()) if now - t < self.window]
        stamps.append(now)
        self._seen[key] = stamps
        if len(self._seen) > 20_000:
            self._seen.clear()
        return len(stamps) >= self.limit
