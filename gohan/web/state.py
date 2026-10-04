"""The dashboard's data layer.

One object, two sources: a live :class:`~gohan.storage.Database` plus the running
services, or deterministic demo data when nothing is attached. Every method
returns plain JSON-serialisable structures, which is what keeps the browser side
dumb and the API honest.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .. import __version__
from ..config import Settings, detect_host
from ..logging_setup import get_logger
from .modules import MODULES, categories, chat_modules, global_modules, module

log = get_logger("web.state")

#: everything the Music page may ask a player to do
MUSIC_ACTIONS = frozenset(
    {"toggle", "skip", "stop", "replay", "shuffle", "loop", "autoplay", "mute", "volume", "seek", "clear"}
)

__all__ = ["EDITABLE_CHAT_KEYS", "WebState", "build_state"]

#: Text/number chat settings an admin may edit from the browser panel.
EDITABLE_CHAT_KEYS: dict[str, tuple[str, int]] = {
    "welcome_text": ("welcome message", 600),
    "rules": ("group rules", 1500),
}

#: How many points the activity chart shows (24 h in 2 h buckets = 12 points).
ACTIVITY_BUCKETS = 12
BUCKET_SECONDS = 2 * 3600

_DEMO_GROUPS: tuple[tuple[int, str, int, str], ...] = (
    (-1001987654321, "Gohan's Anime Club", 4820, "supergroup"),
    (-1001765432198, "Kerala Tech Talk", 2140, "supergroup"),
    (-1001123456789, "Study Group · Physics", 968, "supergroup"),
    (-1001555098765, "Gaming Squad", 640, "supergroup"),
    (-1001988887777, "Music & Lofi", 312, "group"),
    (-1001444333222, "Bot Testing Ground", 12, "group"),
)

_DEMO_USERS: tuple[tuple[int, str, str, int, bool], ...] = (
    (555000111, "spamking", "Spam King", 7, True),
    (555000222, "cryptoqueen", "Crypto Queen", 4, False),
    (555000333, "lurker99", "Quiet One", 3, False),
    (555000444, "asha", "Asha Nair", 1, False),
    (555000555, "rahul_dev", "Rahul", 1, False),
    (555000666, "botfarm_07", "Promo Account", 6, True),
    (555000777, "meera", "Meera", 0, False),
    (555000888, "arjun_k", "Arjun", 0, False),
)

_DEMO_WARNING_REASONS: tuple[str, ...] = (
    "scam link in a newcomer's first message",
    "flood: 9 messages in 8 s",
    "repeated identical messages",
    "invite link - links are not allowed here",
    "forwarded from a channel",
    "three warnings reached - muted for 1 h",
    "caps lock shouting",
    "manual /warn by an admin",
)

_DEMO_EVENTS: tuple[tuple[str, str], ...] = (
    ("ScamDeleted", "scam link deleted · newcomer"),
    ("CaptchaPassed", "captcha solved in 6 s"),
    ("Warning", "/warn reply-to with reason"),
    ("RaidAlert", "12 joins in 10 s - lockdown armed"),
    ("FilterHit", "#rules answered"),
    ("GameScore", "quiz finished · 14 points"),
    ("AiAnswer", "/ask streamed 812 chars"),
    ("GroupAdded", "added to Kerala Tech Talk"),
    ("WatchdogRun", "sweep removed 3 files · 4.1 MB"),
    ("MusicDownload", "track delivered · 7.8 MB"),
    ("NoteSaved", "#faq updated by admin"),
    ("AnimeSearch", "/anime frieren"),
)


def _now_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat(timespec="seconds")


def _age(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    if seconds < 172800:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"


@dataclass
class WebState:
    """Everything the API needs, from whichever source is available."""

    settings: Settings
    db: Any = None
    services: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    demo: bool = False
    demo_seed: int = 7

    # -- capability flags ----------------------------------------------------

    @property
    def live(self) -> bool:
        return self.db is not None and not self.demo

    @property
    def can_control(self) -> bool:
        """Whether actions (toggles, broadcast, restart) will actually do something."""
        return self.live

    def service(self, name: str) -> Any:
        return self.services.get(name)

    # -- bots overview -------------------------------------------------------

    async def _counts(self) -> dict[str, Any]:
        if self.live:
            stats = await self.db.stats()
            tables = await self.db.table_sizes()
            return {
                "users": stats["users"],
                "users_new": stats["users_new"],
                "active_24h": stats["users_active"],
                "groups": stats["chats"],
                "warnings": tables.get("warnings", 0),
                "filters": tables.get("filters", 0),
                "notes": tables.get("notes", 0),
                "games": stats["games"],
                "banned": stats["banned"],
                "scores": tables.get("scores", 0),
                "events": tables.get("events", 0),
            }
        rng = random.Random(self.demo_seed)
        minutes = (time.time() - self.started_at) / 60
        return {
            "users": 1240 + int(minutes * 3),
            "users_new": 18 + int(minutes / 6),
            "active_24h": 318 + int(minutes),
            "groups": len(_DEMO_GROUPS),
            "warnings": 96 + int(minutes / 3),
            "filters": 214,
            "notes": 58,
            "games": 86 + int(minutes * 2),
            "banned": 12,
            "scores": 3412,
            "events": 5120 + int(minutes * 4),
        }

    async def activity(self) -> list[dict[str, Any]]:
        """Messages/moderations per 2 h bucket, for the chart."""
        if self.live:
            now = time.time()
            rows = await self.db.fetch_all(
                "SELECT CAST(ts / ? AS INTEGER) AS bucket, COUNT(*) AS n FROM events "
                "WHERE ts > ? GROUP BY bucket ORDER BY bucket",
                (BUCKET_SECONDS, now - ACTIVITY_BUCKETS * BUCKET_SECONDS),
            )
            found = {int(row["bucket"]): int(row["n"]) for row in rows}
            base = int(now // BUCKET_SECONDS)
            return [
                {
                    "label": datetime.fromtimestamp((base - offset) * BUCKET_SECONDS).strftime("%H:%M"),
                    "value": found.get(base - offset, 0),
                }
                for offset in range(ACTIVITY_BUCKETS - 1, -1, -1)
            ]
        rng = random.Random(self.demo_seed + 1)
        base = time.time()
        out = []
        for offset in range(ACTIVITY_BUCKETS - 1, -1, -1):
            stamp = base - offset * BUCKET_SECONDS
            hour = datetime.fromtimestamp(stamp).hour
            # a believable daily curve: quiet at night, busy in the evening
            shape = 0.35 + 0.65 * max(0.0, abs(12 - hour) / 12) if hour < 12 else 0.7 + 0.3 * (hour / 24)
            out.append(
                {
                    "label": datetime.fromtimestamp(stamp).strftime("%H:%M"),
                    "value": int((60 + 220 * shape) * (0.85 + rng.random() * 0.3)),
                }
            )
        return out

    async def modules_payload(self) -> list[dict[str, Any]]:
        """Every module with its current state plus the counts behind it."""
        counts = await self._counts()
        global_state = await self.global_toggles()
        out: list[dict[str, Any]] = []
        for item in MODULES:
            if item.scope == "global":
                enabled = bool(global_state.get(item.key, item.default))
            else:
                enabled = item.default  # chat scope: the default, overridden per group
            out.append(
                {
                    **self._module_dict(item),
                    "enabled": enabled,
                    "stat": self._module_stat(item.key, counts),
                }
            )
        return out

    @staticmethod
    def _module_dict(item: Any) -> dict[str, Any]:
        return {
            "key": item.key,
            "name": item.name,
            "icon": item.icon,
            "category": item.category,
            "blurb": item.blurb,
            "scope": item.scope,
            "command": item.command,
            "default": item.default,
        }

    @staticmethod
    def _module_stat(key: str, counts: dict[str, Any]) -> str:
        return {
            "filters": f"{counts['filters']} filters",
            "notes": f"{counts['notes']} notes",
            "games": f"{counts['games']} played (24h)",
            "warnings": f"{counts['warnings']} total",
            "guardian": f"{counts['groups']} groups watched",
            "custom_emoji": "600 ids",
            "rich_messages": "richgram",
            "ai": "streaming",
            "keep_alive": "port " + str(0),
            "log_channel": f"{counts['events']} events",
            "maintenance": "all clear",
            "music": f"{counts['games']} downloads",
            "anime": "anilist",
        }.get(key, "")

    async def global_toggles(self) -> dict[str, Any]:
        """Process-wide switches (stored in ``kv``, falling back to settings)."""
        if not self.live:
            return {m.key: m.default for m in global_modules()}
        stored = await self.db.get_kv("modules", {}) or {}
        out: dict[str, Any] = {
            "rich_messages": self.settings.rich_default,
            "custom_emoji": self.settings.custom_emoji,
            "watchdog": self.settings.watchdog,
            "keep_alive": self.settings.keep_alive,
            "log_channel": bool(self.settings.log_channel_id),
            "ai": self.settings.llm_enabled,
            "maintenance": bool(await self.db.get_kv("maintenance", False)),
        }
        out.update({k: v for k, v in (stored or {}).items() if k in {m.key for m in global_modules()}})
        return out

    async def set_global_toggle(self, key: str, enabled: bool) -> dict[str, Any]:
        item = module(key)
        if item is None or item.scope != "global":
            return {"ok": False, "error": "unknown module"}
        if not self.live:
            return {"ok": True, "demo": True, "key": key, "enabled": enabled}

        stored = dict(await self.db.get_kv("modules", {}) or {})
        stored[key] = bool(enabled)
        await self.db.set_kv("modules", stored)

        # a couple of switches have a real side effect the bot also reads
        if key == "maintenance":
            await self.db.set_kv("maintenance", bool(enabled))
        if key == "custom_emoji":
            from ..rich import emoji as registry

            registry.set_custom_enabled(bool(enabled))
        if key == "rich_messages":
            self.settings.rich_default = bool(enabled)

        await self.db.log_event(
            "WebToggle", data={"module": key, "enabled": bool(enabled), "scope": "global"}
        )
        log.info("web: global module %s -> %s", key, enabled)
        return {"ok": True, "key": key, "enabled": bool(enabled)}

    # -- groups --------------------------------------------------------------

    async def groups(self, *, query: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if self.live:
            rows = await self.db.fetch_all(
                "SELECT chat_id, title, type, username, added_at, is_active, settings FROM chats "
                "ORDER BY is_active DESC, added_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
            chats = [self._group_row(row) for row in rows]
            total = await self.db.count_chats()
        else:
            rng = random.Random(self.demo_seed + 2)
            chats = []
            for index, (chat_id, title, members, kind) in enumerate(_DEMO_GROUPS):
                settings = {
                    key: (True if key in ("guardian", "captcha", "antispam", "welcome", "filters") else False)
                    for key in ("guardian", "captcha", "antiraid", "antispam", "welcome", "goodbye", "delete_links", "delete_forwards")
                }
                if index % 3 == 2:
                    settings["antiraid"] = True
                chats.append(
                    {
                        "chat_id": chat_id,
                        "title": title,
                        "type": kind,
                        "username": None,
                        "members": members,
                        "active": True,
                        "modules_on": sum(1 for v in settings.values() if v),
                        "modules_total": len(settings),
                        "warnings": rng.randint(0, 40),
                        "filters": rng.randint(0, 25),
                        "notes": rng.randint(0, 8),
                        "settings": settings,
                        "added_at": _now_iso(self.started_at - index * 86400),
                        "last_seen": _now_iso(time.time() - rng.randint(60, 7200)),
                    }
                )
            total = len(chats)

        if query:
            needle = query.lower()
            chats = [
                c
                for c in chats
                if needle in (c["title"] or "").lower() or needle in str(c["chat_id"])
            ]
        return {"ok": True, "total": total, "count": len(chats), "groups": chats, "demo": self.demo}

    @staticmethod
    def _group_row(row: Any) -> dict[str, Any]:
        import json

        try:
            settings = json.loads(row["settings"] or "{}")
        except (TypeError, ValueError):
            settings = {}
        chat_keys = [m.key for m in chat_modules()]
        return {
            "chat_id": row["chat_id"],
            "title": row["title"] or str(row["chat_id"]),
            "type": row["type"] or "group",
            "username": row["username"],
            "members": None,
            "active": bool(row["is_active"]),
            "modules_on": sum(1 for key in chat_keys if settings.get(key, module(key).default if module(key) else False)),
            "modules_total": len(chat_keys),
            "settings": settings,
            "warnings": 0,
            "filters": 0,
            "notes": 0,
            "added_at": _now_iso(row["added_at"]),
            "last_seen": _now_iso(),
        }

    async def group_detail(self, chat_id: int) -> dict[str, Any]:
        if not self.live:
            found = await self.groups(limit=200)
            for chat in found["groups"]:
                if int(chat["chat_id"]) == int(chat_id):
                    settings = chat.get("settings") or {}
                    chat["module_states"] = {m.key: bool(settings.get(m.key, m.default)) for m in chat_modules()}
                    chat["editable"] = {
                        "welcome_text": settings.get("welcome_text") or "👋 welcome {name} to {chat}!",
                        "rules": settings.get("rules") or "1. be kind\n2. no spam\n3. stay on topic",
                        "warn_limit": 3,
                    }
                    chat["top_filters"] = [
                        {"trigger": name, "uses": uses}
                        for name, uses in (("#rules", 42), ("#links", 31), ("#faq", 12))
                    ]
                    chat["top_warned"] = [{"user_id": 555000111, "warnings": 4}, {"user_id": 777222333, "warnings": 2}]
                    chat["leaderboard"] = [{"name": "Asha", "score": 240}, {"name": "Rahul", "score": 180}]
                    return {"ok": True, "group": chat, "demo": True}
            return {"ok": False, "error": "not found"}

        row = await self.db.chat_record(chat_id)
        if row is None:
            return {"ok": False, "error": "not found"}
        group = self._group_row(row)
        settings = group["settings"]
        group["module_states"] = {m.key: bool(settings.get(m.key, m.default)) for m in chat_modules()}
        group["editable"] = {
            "welcome_text": settings.get("welcome_text") or "",
            "rules": settings.get("rules") or "",
            "warn_limit": int(settings.get("warn_limit", 3) or 3),
        }
        group["warnings"] = await self.db.fetch_value(
            "SELECT COUNT(*) FROM warnings WHERE chat_id = ?", (chat_id,)
        )
        group["filters"] = await self.db.count_filters(chat_id)
        notes = await self.db.list_notes(chat_id)
        group["notes"] = len(notes)
        group["top_filters"] = [
            {"trigger": f"#{row['trigger']}", "uses": int(row["uses"])}
            for row in (await self.db.top_filters(limit=8))
        ]
        group["top_warned"] = [
            {"user_id": user_id, "warnings": count}
            for user_id, count in await self.db.top_warned(chat_id, limit=5)
        ]
        group["leaderboard"] = [
            {"name": name, "score": score}
            for name, score in await self.db.game_stats(chat_id)
        ][:5]
        return {"ok": True, "group": group}

    async def update_group_settings(self, chat_id: int, values: dict[str, Any]) -> dict[str, Any]:
        """Edit the text/number settings of one group (the Independent View editor)."""
        allowed = dict(EDITABLE_CHAT_KEYS)
        allowed["warn_limit"] = ("warnings before a ban", 3)
        changed: dict[str, Any] = {}
        for key, value in (values or {}).items():
            if key not in allowed:
                continue
            label, limit = allowed[key]
            if isinstance(limit, int) and key == "warn_limit":
                try:
                    number = max(1, min(int(value), 20))
                except (TypeError, ValueError):
                    continue
                changed[key] = number
            else:
                text = str(value)[: int(limit)]
                changed[key] = text
        if not changed:
            return {"ok": False, "error": "nothing to update"}
        if not self.live:
            return {"ok": True, "demo": True, "updated": changed}

        for key, value in changed.items():
            await self.db.set_chat_setting(chat_id, key, value)
        await self.db.log_event("WebSettings", chat_id=chat_id, data={"keys": list(changed)})
        log.info("web: chat %s settings updated: %s", chat_id, list(changed))
        return {"ok": True, "chat_id": chat_id, "updated": changed}

    async def apply_module_all(self, key: str, enabled: bool) -> dict[str, Any]:
        """Turn one chat module on or off in **every** group at once."""
        item = module(key)
        if item is None or item.scope != "chat":
            return {"ok": False, "error": "unknown chat module"}
        if not self.live:
            return {"ok": True, "demo": True, "key": key, "enabled": enabled, "groups": 6}
        chats = await self.db.chats_with_module_key(key)
        for chat_id in chats:
            await self.db.set_chat_setting(chat_id, key, bool(enabled))
        await self.db.log_event(
            "WebBulk", data={"module": key, "enabled": bool(enabled), "groups": len(chats)}
        )
        log.info("web: module %s -> %s across %d groups", key, enabled, len(chats))
        return {"ok": True, "key": key, "enabled": bool(enabled), "groups": len(chats)}

    async def set_chat_module(self, chat_id: int, key: str, enabled: bool) -> dict[str, Any]:
        item = module(key)
        if item is None or item.scope != "chat":
            return {"ok": False, "error": "unknown chat module"}
        if not self.live:
            return {"ok": True, "demo": True, "chat_id": chat_id, "key": key, "enabled": enabled}
        await self.db.set_chat_setting(chat_id, key, bool(enabled))
        await self.db.log_event(
            "WebToggle",
            chat_id=chat_id,
            data={"module": key, "enabled": bool(enabled), "scope": "chat"},
        )
        log.info("web: chat %s module %s -> %s", chat_id, key, enabled)
        return {"ok": True, "chat_id": chat_id, "key": key, "enabled": bool(enabled)}

    # -- users ---------------------------------------------------------------

    async def users(self, *, query: str = "", limit: int = 30, banned: bool | None = None) -> dict[str, Any]:
        """Search users for the moderation page."""
        limit = max(1, min(int(limit), 200))
        if self.live:
            rows = await self.db.search_users(query, limit=limit, banned=banned)
            counts = await self.db.fetch_all(
                "SELECT user_id, COUNT(*) AS n FROM warnings GROUP BY user_id"
            )
            warning_counts = {int(row["user_id"]): int(row["n"]) for row in counts}
            items = [
                {
                    "user_id": row["user_id"],
                    "username": row["username"],
                    "name": row["first_name"] or (f"@{row['username']}" if row["username"] else str(row["user_id"])),
                    "banned": bool(row["is_banned"]),
                    "premium": bool(row["is_premium"]),
                    "first_seen": _now_iso(row["first_seen"]),
                    "last_seen": _now_iso(row["last_seen"]),
                    "warnings": warning_counts.get(int(row["user_id"]), 0),
                    "starts": int(row["start_count"] or 0),
                }
                for row in rows
            ]
            return {"ok": True, "users": items, "count": len(items), "demo": False}

        rng = random.Random(self.demo_seed + 3)
        items = []
        for user_id, username, name, warnings, is_banned in _DEMO_USERS:
            if banned is not None and is_banned != banned:
                continue
            if query:
                needle = query.lstrip("@").lower()
                if needle not in username.lower() and needle not in name.lower() and needle not in str(user_id):
                    continue
            items.append(
                {
                    "user_id": user_id,
                    "username": username,
                    "name": name,
                    "banned": is_banned,
                    "premium": rng.random() > 0.7,
                    "first_seen": _now_iso(self.started_at - rng.randint(5, 200) * 86400),
                    "last_seen": _now_iso(time.time() - rng.randint(30, 40000)),
                    "warnings": warnings,
                    "starts": rng.randint(0, 12),
                }
            )
        return {"ok": True, "users": items[:limit], "count": len(items), "demo": True}

    async def user_detail(self, user_id: int) -> dict[str, Any]:
        """One user: warnings, groups, scores - the drawer."""
        if self.live:
            record = await self.db.get_user(user_id)
            if record is None:
                return {"ok": False, "error": "unknown user"}
            warnings = await self.db.warnings_by_user(user_id, limit=50)
            groups = await self.db.user_groups(user_id)
            scores = await self.db.scores_of(user_id)
            return {
                "ok": True,
                "user": {
                    "user_id": record.user_id,
                    "username": record.username,
                    "name": record.first_name or str(record.user_id),
                    "banned": record.is_banned,
                    "premium": record.is_premium,
                    "language": record.language_code,
                    "first_seen": _now_iso(record.first_seen),
                    "last_seen": _now_iso(record.last_seen),
                    "starts": record.start_count,
                    "warnings": len(warnings),
                },
                "warnings": [
                    {
                        "id": row["id"],
                        "chat_id": row["chat_id"],
                        "chat_title": row["chat_title"] if "chat_title" in row.keys() else None,
                        "reason": row["reason"] or "no reason given",
                        "admin_id": row["admin_id"],
                        "ts": _now_iso(row["created_at"]),
                        "age": _age(time.time() - row["created_at"]),
                    }
                    for row in warnings
                ],
                "groups": [
                    {"chat_id": row["chat_id"], "title": row["title"], "warnings": int(row["warnings"])}
                    for row in groups
                ],
                "scores": [
                    {"game": row["game"], "score": int(row["best"]), "chat_id": row["chat_id"]}
                    for row in scores
                ],
            }

        for user_id_, username, name, warnings, is_banned in _DEMO_USERS:
            if user_id_ != int(user_id):
                continue
            rng = random.Random(user_id_)
            rows = []
            for index in range(warnings):
                rows.append(
                    {
                        "id": index + 1,
                        "chat_id": _DEMO_GROUPS[index % len(_DEMO_GROUPS)][0],
                        "chat_title": _DEMO_GROUPS[index % len(_DEMO_GROUPS)][1],
                        "reason": rng.choice(_DEMO_WARNING_REASONS),
                        "admin_id": 42,
                        "ts": _now_iso(time.time() - index * 86400 * 2),
                        "age": _age(index * 86400 * 2 + 400),
                    }
                )
            return {
                "ok": True,
                "user": {
                    "user_id": user_id_,
                    "username": username,
                    "name": name,
                    "banned": is_banned,
                    "premium": rng.random() > 0.6,
                    "language": "en",
                    "first_seen": _now_iso(self.started_at - 90 * 86400),
                    "last_seen": _now_iso(time.time() - rng.randint(60, 8000)),
                    "starts": rng.randint(0, 9),
                    "warnings": warnings,
                },
                "warnings": rows,
                "groups": [
                    {"chat_id": g[0], "title": g[1], "warnings": max(0, warnings - index)}
                    for index, g in enumerate(_DEMO_GROUPS[:4])
                ],
                "scores": [
                    {"game": "quiz", "score": rng.randint(8, 40), "chat_id": _DEMO_GROUPS[0][0]},
                    {"game": "guess", "score": rng.randint(3, 15), "chat_id": _DEMO_GROUPS[0][0]},
                ],
            }
        return {"ok": False, "error": "unknown user"}

    async def user_action(self, user_id: int, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ban, unban or clear the warnings of a user from the browser."""
        payload = payload or {}
        if not self.live:
            return {"ok": True, "demo": True, "message": f"{action} is a demo no-op"}

        record = await self.db.get_user(user_id)
        if record is None and action != "unban":
            return {"ok": False, "error": "unknown user"}

        if action == "ban":
            await self.db.set_banned(user_id, True)
            message = "user blocked from the bot"
        elif action == "unban":
            await self.db.set_banned(user_id, False)
            message = "user unblocked"
        elif action in ("clear_warnings", "clear"):
            removed = await self.db.clear_all_warnings(user_id)
            message = f"cleared {removed} warnings"
        else:
            return {"ok": False, "error": f"unknown action {action!r}"}

        await self.db.log_event(
            "WebAction", user_id=user_id, data={"action": action, "by": "web panel"}
        )
        log.info("web: user %s -> %s", user_id, action)

        # a real Telegram-side ban/unban too, when we have a bot and a chat
        chat_id = payload.get("chat_id")
        bot = self.service("bot")
        if bot is not None and chat_id and action in ("ban", "unban"):
            try:
                from aiogram.exceptions import TelegramAPIError

                if action == "ban":
                    await bot.ban_chat_member(int(chat_id), user_id)
                else:
                    await bot.unban_chat_member(int(chat_id), user_id, only_if_banned=True)
                message += f" (in {chat_id})"
            except TelegramAPIError as exc:
                message += f" (Telegram refused: {str(exc)[:80]})"
            except Exception as exc:
                log.debug("telegram ban failed: %s", exc)
        return {"ok": True, "message": message, "action": action, "user_id": user_id}

    # -- moderation ----------------------------------------------------------

    async def moderation(self, *, days: int = 7, limit: int = 40) -> dict[str, Any]:
        """The moderation page: counts, a chart and the newest cases."""
        if self.live:
            warnings = await self.db.recent_warnings(limit=limit, days=days)
            per_day = await self.db.warnings_per_day(days=days)
            offenders = await self.db.top_offenders(limit=8, days=days)
            chats = await self.db.top_warning_chats(limit=6, days=days)
            cases = [
                {
                    "id": row["id"],
                    "user_id": row["user_id"],
                    "user": row["first_name"] or (f"@{row['username']}" if row["username"] else str(row["user_id"])),
                    "username": row["username"],
                    "chat_id": row["chat_id"],
                    "chat": row["chat_title"] or str(row["chat_id"]),
                    "reason": row["reason"] or "no reason given",
                    "ts": _now_iso(row["created_at"]),
                    "age": _age(time.time() - row["created_at"]),
                }
                for row in warnings
            ]
            total = len(cases)
            all_time = int(await self.db.fetch_value("SELECT COUNT(*) FROM warnings"))
            banned = len(await self.db.banned_users())
            return {
                "ok": True,
                "demo": False,
                "window_days": days,
                "kpis": [
                    {"icon": "⚠️", "label": f"warnings ({days}d)", "value": total, "delta": f"{all_time} all time"},
                    {"icon": "🚫", "label": "blocked users", "value": banned, "delta": "bot-wide"},
                    {"icon": "🏆", "label": "top offender", "value": (offenders[0]["n"] if offenders else 0), "delta": (offenders[0]["username"] or str(offenders[0]["user_id"])) if offenders else "none"},
                    {"icon": "💬", "label": "busiest group", "value": (chats[0]["n"] if chats else 0), "delta": (chats[0]["title"] or str(chats[0]["chat_id"])) if chats else "none"},
                ],
                "chart": self._day_buckets(per_day, days),
                "cases": cases,
                "offenders": [
                    {
                        "user_id": row["user_id"],
                        "name": row["first_name"] or (f"@{row['username']}" if row["username"] else str(row["user_id"])),
                        "username": row["username"],
                        "warnings": int(row["n"]),
                        "groups": int(row["chats"]),
                    }
                    for row in offenders
                ],
                "chats": [
                    {"chat_id": row["chat_id"], "title": row["title"] or str(row["chat_id"]), "warnings": int(row["n"])}
                    for row in chats
                ],
            }

        # demo
        rng = random.Random(self.demo_seed + 5)
        cases = []
        for index in range(min(limit, 26)):
            user_id, username, name, warnings, _ = _DEMO_USERS[index % len(_DEMO_USERS)]
            group = _DEMO_GROUPS[index % len(_DEMO_GROUPS)]
            stamp = time.time() - index * rng.randint(30, 300)
            cases.append(
                {
                    "id": index + 1,
                    "user_id": user_id,
                    "user": name,
                    "username": username,
                    "chat_id": group[0],
                    "chat": group[1],
                    "reason": rng.choice(_DEMO_WARNING_REASONS),
                    "ts": _now_iso(stamp),
                    "age": _age(time.time() - stamp),
                }
            )
        base = time.time()
        chart = [
            {
                "label": datetime.fromtimestamp(base - (days - 1 - i) * 86400).strftime("%a"),
                "value": rng.randint(4, 26),
            }
            for i in range(days)
        ]
        offenders = sorted(_DEMO_USERS, key=lambda u: -u[3])[:8]
        return {
            "ok": True,
            "demo": True,
            "window_days": days,
            "kpis": [
                {"icon": "⚠️", "label": f"warnings ({days}d)", "value": sum(c["value"] for c in chart), "delta": "96 all time"},
                {"icon": "🚫", "label": "blocked users", "value": sum(1 for u in _DEMO_USERS if u[4]), "delta": "bot-wide"},
                {"icon": "🏆", "label": "top offender", "value": offenders[0][3], "delta": offenders[0][1]},
                {"icon": "💬", "label": "busiest group", "value": 41, "delta": _DEMO_GROUPS[0][1]},
            ],
            "chart": chart,
            "cases": cases,
            "offenders": [
                {"user_id": u[0], "name": u[2], "username": u[1], "warnings": u[3], "groups": max(1, u[3] // 3)}
                for u in offenders
            ],
            "chats": [
                {"chat_id": g[0], "title": g[1], "warnings": rng.randint(3, 40)} for g in _DEMO_GROUPS[:6]
            ],
        }

    @staticmethod
    def _day_buckets(per_day: list[tuple[float, int]], days: int) -> list[dict[str, Any]]:
        """Fill the gaps so the chart always has one point per day."""
        found = {int(stamp) : count for stamp, count in per_day}
        today = int(time.time() // 86400 * 86400)
        return [
            {
                "label": datetime.fromtimestamp(today - (days - 1 - i) * 86400).strftime("%a"),
                "value": found.get(today - (days - 1 - i) * 86400, 0),
            }
            for i in range(days)
        ]

    # -- music / voice -------------------------------------------------------

    async def music(self) -> dict[str, Any]:
        """Now playing, the queues, playlists and controllers - the Music page."""
        from ..voice import capabilities as voice_capabilities

        caps = voice_capabilities(self.settings)
        player = self.service("player")
        rooms: list[dict[str, Any]] = []
        backend: dict[str, Any] = {"name": "detached", "available": False, "note": "voice engine not attached"}
        if player is not None:
            for room in player.active():
                rooms.append(room.snapshot(backend=player.backend_name, queue_limit=25))
            backend = player.backend.status().to_dict() if hasattr(player.backend, "status") else backend

        if self.live:
            playlists = [
                {
                    "id": int(row["id"]),
                    "name": row["name"],
                    "owner": row["first_name"] or (f"@{row['username']}" if row["username"] else str(row["owner_id"])),
                    "owner_id": int(row["owner_id"]),
                    "tracks": int(row["tracks"] or 0),
                }
                for row in await self.db.all_playlists(limit=60)
            ]
            controllers = [
                {
                    "chat_id": int(row["chat_id"]),
                    "chat": row["chat_title"] or str(row["chat_id"]),
                    "user_id": int(row["user_id"]),
                    "user": row["first_name"] or (f"@{row['username']}" if row["username"] else str(row["user_id"])),
                }
                for row in await self.db.voice_controllers(limit=60)
            ]
            afk = [
                {
                    "user_id": int(row["user_id"]),
                    "user": row["first_name"] or (f"@{row['username']}" if row["username"] else str(row["user_id"])),
                    "chat": row["chat_title"] or str(row["chat_id"]),
                    "reason": row["reason"] or "",
                    "since": _now_iso(row["since"]),
                }
                for row in await self.db.all_afk(limit=60)
            ]
        else:
            playlists = [
                {"id": 1, "name": "lofi nights", "owner": "you", "owner_id": 42, "tracks": 12},
                {"id": 2, "name": "workout", "owner": "you", "owner_id": 42, "tracks": 8},
                {"id": 3, "name": "hindi classics", "owner": "meera", "owner_id": 7, "tracks": 24},
            ]
            controllers = [
                {"chat_id": _DEMO_GROUPS[0][0], "chat": _DEMO_GROUPS[0][1], "user_id": 7, "user": "meera"},
            ]
            afk = [
                {"user_id": 555000333, "user": "Quiet One", "chat": _DEMO_GROUPS[0][1], "reason": "sleeping", "since": "12 min ago"},
            ]
            rooms = [self._demo_room()]

        return {
            "ok": True,
            "demo": not self.live,
            "backend": backend,
            "capabilities": {
                "yt_dlp": caps.yt_dlp,
                "ffmpeg": caps.ffmpeg,
                "cookies": caps.cookies,
                "pytgcalls": caps.pytgcalls,
                "ready": caps.ready,
                "note": caps.note,
            },
            "rooms": rooms,
            "playing": sum(1 for room in rooms if room["state"].get("playing")),
            "queued": sum(int(room["queue"].get("size", 0)) for room in rooms),
            "playlists": playlists,
            "controllers": controllers,
            "afk": afk,
        }

    def _demo_room(self) -> dict[str, Any]:
        """A believable room for the offline preview."""
        rng = random.Random(self.demo_seed + 11)
        title = "Midnight City"
        queue = [
            {
                "position": index,
                "title": name,
                "duration": 200 + index * 13,
                "duration_text": f"{(200 + index * 13) // 60}:{(200 + index * 13) % 60:02d}",
                "requested_name": "meera",
            }
            for index, name in enumerate(["Sunset Drive", "Neon Lights", "After Hours"], start=1)
        ]
        return {
            "state": {
                "chat_id": _DEMO_GROUPS[0][0],
                "chat": _DEMO_GROUPS[0][1],
                "title": title,
                "url": "https://youtu.be/demo",
                "thumbnail": None,
                "source": "youtube",
                "uploader": "M83",
                "requested_name": "meera",
                "position": 74.0,
                "position_text": "1:14",
                "duration": 244,
                "duration_text": "4:04",
                "progress_text": "1:14 / 4:04",
                "paused": False,
                "muted": False,
                "volume": 80,
                "speed": 1.0,
                "loop": "off",
                "autoplay": True,
                "queue_length": len(queue),
                "queue_duration": sum(entry["duration"] for entry in queue),
                "queue_duration_text": "12:17",
                "connected": True,
                "playing": True,
                "backend": "null",
                "live": False,
            },
            "queue": {
                "chat_id": _DEMO_GROUPS[0][0],
                "size": len(queue),
                "loop": "off",
                "total_duration": sum(entry["duration"] for entry in queue),
                "total_text": "12:17",
                "tracks": queue,
            },
            "panel": None,
        }

    async def music_action(self, chat_id: int, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Pause / skip / stop / loop / volume … from the browser."""
        payload = payload or {}
        if action not in MUSIC_ACTIONS:
            return {"ok": False, "error": f"unknown action {action!r}"}
        player = self.service("player")
        if not self.live:
            return {"ok": True, "demo": True, "action": action, "message": f"{action} is a demo no-op"}
        if player is None:
            return {"ok": False, "error": "the voice engine is not attached"}
        room = player.rooms.get(int(chat_id))
        if room is None and action != "nothing":
            return {"ok": False, "error": "no voice chat is active there"}

        if action == "toggle":
            await player.toggle(chat_id)
        elif action == "skip":
            await player.skip(chat_id)
        elif action == "stop":
            await player.stop(chat_id, leave=True)
        elif action == "replay":
            await player.replay(chat_id)
        elif action == "shuffle":
            await player.shuffle(chat_id)
        elif action == "loop":
            mode = payload.get("mode")
            await player.loop(chat_id, mode)
        elif action == "autoplay":
            await player.autoplay(chat_id, bool(payload.get("enabled", True)))
        elif action == "mute":
            await player.mute(chat_id, bool(payload.get("muted", True)))
        elif action == "volume":
            await player.volume(chat_id, int(payload.get("value", room.volume)))
        elif action == "seek":
            await player.seek(chat_id, int(payload.get("value", 0)))
        elif action == "clear":
            room.queue.clear()
        else:
            return {"ok": False, "error": f"unknown action {action!r}"}

        await self.db.log_event("WebVoice", chat_id=int(chat_id), data={"action": action})
        snapshot = player.snapshot(chat_id)
        return {"ok": True, "action": action, "room": snapshot}

    async def delete_playlist(self, playlist_id: int) -> dict[str, Any]:
        """Remove a playlist (and its tracks) from the browser."""
        if not self.live:
            return {"ok": True, "demo": True, "playlist_id": playlist_id}
        removed = await self.db.delete_playlist(int(playlist_id))
        await self.db.log_event("WebPlaylist", data={"playlist_id": playlist_id, "tracks": removed})
        return {"ok": True, "playlist_id": int(playlist_id), "removed_tracks": removed}

    # -- events --------------------------------------------------------------

    async def events(self, *, limit: int = 60, tag: str = "", query: str = "") -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        if self.live:
            rows = await self.db.events_since(
                time.time() - 7 * 86400, tag=tag or None
            )
            items = [
                {
                    "ts": _now_iso(row["ts"]),
                    "age": _age(time.time() - row["ts"]),
                    "tag": row["tag"],
                    "chat_id": row["chat_id"],
                    "user_id": row["user_id"],
                    "data": row["data"],
                }
                for row in reversed(rows)
            ]
            if query:
                needle = query.lower()
                items = [
                    e
                    for e in items
                    if needle in e["tag"].lower() or needle in str(e["data"]).lower()
                ]
            counts = await self.db.event_counts(time.time() - 7 * 86400)
            return {
                "ok": True,
                "events": items[:limit],
                "total": len(items),
                "tags": [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda kv: -kv[1])[:12]],
                "demo": False,
            }

        # demo: a rolling feed that always looks alive
        rng = random.Random(self.demo_seed + int(time.time() / 20))
        items = []
        for index in range(min(limit, 40)):
            tag_name, blurb = rng.choice(_DEMO_EVENTS)
            stamp = time.time() - index * rng.randint(20, 240)
            chat_id = rng.choice(_DEMO_GROUPS)[0]
            items.append(
                {
                    "ts": _now_iso(stamp),
                    "age": _age(time.time() - stamp),
                    "tag": tag_name,
                    "chat_id": chat_id,
                    "user_id": rng.randint(100000, 999999),
                    "data": blurb,
                }
            )
        counts: dict[str, int] = {}
        for item in items:
            counts[item["tag"]] = counts.get(item["tag"], 0) + 1
        return {
            "ok": True,
            "events": items,
            "total": len(items),
            "tags": [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda kv: -kv[1])],
            "demo": True,
        }

    # -- overview / settings -------------------------------------------------

    async def overview(self) -> dict[str, Any]:
        counts = await self._counts()
        modules = await self.modules_payload()
        events = await self.events(limit=8)
        activity = await self.activity()
        return {
            "ok": True,
            "demo": self.demo,
            "can_control": self.can_control,
            "bot": await self.bot_info(),
            "kpis": [
                {"key": "users", "label": "users", "value": counts["users"], "delta": f"+{counts['users_new']} today", "icon": "👥"},
                {"key": "active", "label": "active 24h", "value": counts["active_24h"], "delta": "seen today", "icon": "📡"},
                {"key": "groups", "label": "groups", "value": counts["groups"], "delta": "watched", "icon": "💬"},
                {"key": "filters", "label": "filters", "value": counts["filters"], "delta": f"{counts['notes']} notes", "icon": "🧲"},
                {"key": "warnings", "label": "warnings", "value": counts["warnings"], "delta": f"{counts['banned']} blocked", "icon": "⚠️"},
                {"key": "games", "label": "games 24h", "value": counts["games"], "delta": "played", "icon": "🎮"},
            ],
            "activity": activity,
            "modules": modules,
            "categories": [{"key": k, "label": label, "blurb": blurb} for k, label, blurb in categories()],
            "events": events["events"],
            "tags": events["tags"],
            "counts": counts,
        }

    async def bot_info(self) -> dict[str, Any]:
        me: dict[str, Any] = {}
        bot = self.service("bot")
        if bot is not None and not self.demo:
            try:
                user = await asyncio.wait_for(bot.get_me(), timeout=4)
                me = {"username": user.username, "id": user.id}
            except Exception as exc:
                log.debug("get_me failed: %s", exc)
        mtproto = self.service("mtproto")
        watchdog = self.service("watchdog")
        ai = self.service("ai")
        keep_alive = self.service("keep_alive")
        mode = "demo" if self.demo else "live"
        return {
            "name": self.settings.bot_name,
            "version": __version__,
            "mode": mode,
            "host": detect_host(),
            "uptime_s": int(time.time() - self.started_at),
            "python": None,
            "mtproto": (mtproto.status.describe() if mtproto is not None else "not configured"),
            "assistant": getattr(getattr(ai, "llm", None), "model", "demo generator")
            if ai is not None
            else "demo generator",
            "watchdog": bool(watchdog),
            "port": self.settings.port,
            "sweeps": getattr(getattr(watchdog, "stats", None), "sweeps", 0) if watchdog else 0,
            "owners": len(self.settings.owner_ids),
            "log_channel": self.settings.log_channel_id or None,
            "pings": getattr(keep_alive, "_pings", 0) if keep_alive else 0,
            "ping_failures": getattr(keep_alive, "_ping_failures", 0) if keep_alive else 0,
            "last_error": getattr(keep_alive, "_last_error", None) if keep_alive else None,
            **me,
        }

    async def settings_view(self) -> dict[str, Any]:
        s = self.settings
        rows = [
            ("rich messages", "on" if s.rich_default else "fallback", "🔮"),
            ("custom emoji", "on" if s.custom_emoji else "off", "😺"),
            ("mtproto", s.mtproto_mode, "📡"),
            ("llm provider", s.llm_provider, "🤖"),
            ("watchdog", f"every {s.watchdog_interval}s" if s.watchdog else "off", "🧹"),
            ("keep-alive", f"port {s.port}" if s.keep_alive else "off", "🌐"),
            ("log channel", str(s.log_channel_id or "owner dm"), "📨"),
            ("timezone", s.log_timezone, "🕒"),
            ("database", str(s.database_path), "💾"),
            ("owners", str(len(s.owner_ids)), "👑"),
        ]
        return {
            "ok": True,
            "demo": self.demo,
            "can_control": self.can_control,
            "settings": [{"label": label, "value": value, "icon": icon} for label, value, icon in rows],
            "problems": s.problems(),
            "public_url": s.resolved_keep_alive_url or None,
            "auth": self.services.get("auth_mode", "off"),
        }

    # -- actions -------------------------------------------------------------

    async def action(self, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run an owner action from the browser."""
        payload = payload or {}
        if name in ("restart", "shutdown") and self.demo:
            return {"ok": True, "demo": True, "message": f"{name} is disabled in the demo"}
        if not self.live and name not in ("refresh",):
            return {"ok": True, "demo": True, "message": f"{name} needs a running bot"}

        if name == "refresh":
            return {"ok": True, "message": "refreshed"}

        if name == "broadcast":
            text = str(payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "nothing to send"}
            from ..handlers.owner import _broadcast

            bot = self.service("bot")
            if bot is None:
                return {"ok": False, "error": "bot is not attached"}
            sent = await _broadcast(bot, self.db, text)
            return {"ok": True, "message": f"delivered to {sent} users", "sent": sent}

        if name == "sweep":
            watchdog = self.service("watchdog")
            if watchdog is None:
                return {"ok": False, "error": "watchdog is disabled"}
            report = await watchdog.sweep(aggressive=bool(payload.get("aggressive")))
            return {"ok": True, "message": f"removed {report['files']} files", "report": report}

        if name == "backup":
            from pathlib import Path

            source = Path(self.settings.database_path)
            if not source.exists():
                return {"ok": False, "error": "no database yet"}
            return {
                "ok": True,
                "message": f"{source.stat().st_size // 1024} kb",
                "size_kb": source.stat().st_size // 1024,
            }

        if name == "purge_events":
            days = int(payload.get("days") or 30)
            removed = await self.db.prune_events(keep_days=days)
            return {"ok": True, "message": f"pruned {removed} events"}

        if name == "restart":
            return {"ok": True, "message": "restart scheduled", "restart": True}

        if name == "shutdown":
            return {"ok": True, "message": "shutdown scheduled", "shutdown": True}

        return {"ok": False, "error": f"unknown action {name!r}"}

    # -- live stream ---------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        counts = await self._counts()
        events = await self.events(limit=30)
        info = await self.bot_info()
        return {
            "ok": True,
            "demo": self.demo,
            "ts": _now_iso(),
            "counts": counts,
            "bot": info,
            "events": events["events"][:20],
            "tags": events["tags"],
        }


def build_state(
    settings: Settings,
    db: Any = None,
    services: dict[str, Any] | None = None,
    *,
    demo: bool = False,
    started_at: float | None = None,
) -> WebState:
    """Create the state object the API and the dashboard read from."""
    return WebState(
        settings=settings,
        db=None if demo else db,
        services=services or {},
        demo=demo,
        started_at=started_at or time.time(),
    )
