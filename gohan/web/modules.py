"""The module registry behind the dashboard's plugin grid.

Every switch the bot understands is described once, here. The web panel, the
guardian panel in Telegram and the API all read the same list, so a new feature
only has to be added in one place.

``scope`` is the important field:

``chat``    the setting lives in a chat's settings blob and is toggled per group
``global``  the setting is process-wide (an env value or a key in ``kv``)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

__all__ = ["MODULES", "CATEGORIES", "categories", "chat_modules", "global_modules", "module"]


@dataclass(frozen=True, slots=True)
class Module:
    key: str
    name: str
    icon: str
    category: str
    blurb: str
    scope: str = "chat"
    command: str = ""
    default: bool = False
    tags: tuple[str, ...] = field(default=(), repr=False)

    @property
    def is_chat(self) -> bool:
        return self.scope == "chat"


#: Display order of the categories on the Modules page.
CATEGORIES: Final[dict[str, tuple[str, str]]] = {
    "protection": ("🛡 protection", "keeps the group clean without an admin watching"),
    "community": ("💬 community", "the things members actually notice"),
    "fun": ("🎮 fun", "games and the assistant"),
    "system": ("⚙️ system", "process-wide behaviour"),
}


MODULES: Final[tuple[Module, ...]] = (
    # -- protection ---------------------------------------------------------
    Module(
        "guardian",
        "Guardian",
        "🛡",
        "protection",
        "master switch for every automatic rule below",
        command="/guardian",
        default=True,
    ),
    Module(
        "captcha",
        "Captcha",
        "🔢",
        "protection",
        "new members tap a button before they can talk",
        command="/captcha on",
        default=True,
        tags=("join",),
    ),
    Module(
        "antiraid",
        "Anti-raid",
        "🚨",
        "protection",
        "detects join spikes and freezes the chat automatically",
        command="/antiraid on",
        default=True,
        tags=("raid", "lockdown"),
    ),
    Module(
        "antispam",
        "Anti-spam",
        "🚫",
        "protection",
        "scam patterns, floods, duplicates and invite spam",
        command="/antispam on",
        default=True,
        tags=("scam", "flood"),
    ),
    Module(
        "delete_links",
        "No links",
        "🔗",
        "protection",
        "delete any message containing a link",
        command="/guardian",
        default=False,
    ),
    Module(
        "delete_forwards",
        "No forwards",
        "📤",
        "protection",
        "delete forwarded messages",
        command="/guardian",
        default=False,
    ),
    # -- community ----------------------------------------------------------
    Module(
        "welcome",
        "Welcome",
        "👋",
        "community",
        "greet every new member ({name}, {chat})",
        command="/welcome <text>",
        default=True,
    ),
    Module(
        "goodbye",
        "Goodbye",
        "🌙",
        "community",
        "say farewell when someone leaves",
        command="/guardian",
        default=False,
    ),
    Module(
        "filters",
        "Filters",
        "🧲",
        "community",
        "automatic replies to keywords",
        command="/filters",
        default=True,
        tags=("triggers", "auto-reply"),
    ),
    Module(
        "notes",
        "Notes",
        "📝",
        "community",
        "saved snippets members can print with #name",
        command="/notes",
        default=True,
    ),
    Module(
        "warnings",
        "Warnings",
        "⚠️",
        "community",
        "warn, mute, kick, ban - with escalation at 3/5/7",
        command="/warn",
        default=True,
    ),
    # -- fun ----------------------------------------------------------------
    Module(
        "games",
        "Games",
        "🎮",
        "fun",
        "quiz, guess, word chain, dice and slots with a leaderboard",
        command="/games",
        default=True,
    ),
    Module(
        "fun_actions",
        "Fun actions",
        "✨",
        "fun",
        "19 commands like /hug and /kiss, with teachable GIFs",
        command="/actions",
        default=True,
    ),
    Module(
        "anime",
        "Anime",
        "🎬",
        "fun",
        "AniList cards, trending lists and character search",
        command="/anime",
        default=True,
    ),
    Module(
        "music",
        "Music",
        "🎧",
        "fun",
        "search YouTube and deliver the audio file",
        command="/music",
        default=True,
    ),
    Module(
        "ai",
        "AI assistant",
        "🤖",
        "fun",
        "streamed answers; /ai on makes it answer the chat itself",
        scope="global",
        command="/ask",
        default=True,
        tags=("llm", "chatbot"),
    ),
    # -- system -------------------------------------------------------------
    Module(
        "rich_messages",
        "Rich messages",
        "🔮",
        "system",
        "headings, tables, blockquotes and colour buttons",
        scope="global",
        command="/rich",
        default=True,
    ),
    Module(
        "custom_emoji",
        "Custom emoji",
        "😺",
        "system",
        "600 premium emoji ids from @TgEmojis",
        scope="global",
        command="/emojis",
        default=True,
    ),
    Module(
        "watchdog",
        "Watchdog",
        "🧹",
        "system",
        "disk, temp files, abandoned flows and hung connections",
        scope="global",
        command="/watchdog",
        default=True,
    ),
    Module(
        "keep_alive",
        "Keep-alive",
        "🌐",
        "system",
        "self-ping so free hosts do not idle the process",
        scope="global",
        command="/status",
        default=True,
    ),
    Module(
        "log_channel",
        "Log channel",
        "📨",
        "system",
        "every event mirrored to a channel plus owner DMs",
        scope="global",
        command="/logs",
        default=False,
    ),
    Module(
        "maintenance",
        "Maintenance",
        "🛠",
        "system",
        "pause the bot for regular users while you work",
        scope="global",
        command="/maintenance",
        default=False,
    ),
)

_BY_KEY: Final[dict[str, Module]] = {m.key: m for m in MODULES}


def module(key: str) -> Module | None:
    return _BY_KEY.get(key)


def chat_modules() -> tuple[Module, ...]:
    return tuple(m for m in MODULES if m.is_chat)


def global_modules() -> tuple[Module, ...]:
    return tuple(m for m in MODULES if not m.is_chat)


def categories() -> tuple[tuple[str, str, str], ...]:
    """``(key, label, blurb)`` for every category that has modules."""
    return tuple(
        (key, label, blurb)
        for key, (label, blurb) in CATEGORIES.items()
        if any(m.category == key for m in MODULES)
    )
