"""Custom (premium) emoji registry.

Telegram's custom emoji are inline entities that reference a
``custom_emoji_id``. In Rich Message HTML the tag is::

    <tg-emoji emoji-id="5104989658350093220">📍</tg-emoji>

The ids come from :mod:`gohan.rich.emoji_data` (four @TgEmojis packs, 600 ids).

Two ways to use it::

    emoji.emoji("location")          # semantic name -> best matching id
    emoji.by_char("🔥", variant=1)   # pick the 2nd of the 7 fire variants

**Premium caveat.** Bots may only *use* custom emoji in outgoing messages when
the bot's owner has Telegram Premium. The registry therefore always knows the
plain fallback character, and :func:`set_custom_enabled` (wired to
``CUSTOM_EMOJI`` in the config) switches the whole bot to plain emoji at once -
no handler changes required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .emoji_data import PACK_LABELS, PACKS, all_pairs, count

__all__ = [
    "EMOJI",
    "Emoji",
    "PACKS",
    "PACK_LABELS",
    "aliases",
    "by_char",
    "by_id",
    "char",
    "custom_enabled",
    "emoji",
    "names",
    "pack_of",
    "resolve",
    "set_custom_enabled",
    "total_custom_ids",
    "variants",
]


@dataclass(frozen=True, slots=True)
class Emoji:
    """One resolved emoji.

    Attributes:
        name: canonical semantic name, e.g. ``"fire"`` (or ``"char:🔥"`` for
            characters with no friendly name).
        char: plain emoji - always safe to send.
        custom_id: Telegram ``custom_emoji_id``, or ``None``.
        pack: which pack the id came from (``"A"`` … ``"D"``), if any.
        tags: extra lookup names.
    """

    name: str
    char: str
    custom_id: str | None = None
    pack: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def is_custom(self) -> bool:
        return bool(self.custom_id)

    def html(self, *, custom: bool | None = None) -> str:
        """The emoji as rich HTML (``<tg-emoji>`` when available and enabled)."""
        use_custom = self.is_custom if custom is None else (custom and self.is_custom)
        if use_custom:
            return f'<tg-emoji emoji-id="{self.custom_id}">{self.char}</tg-emoji>'
        return self.char


# ---------------------------------------------------------------------------
#  index built from the packs
# ---------------------------------------------------------------------------

#: emoji character -> tuple of custom ids, in pack order.
_BY_CHAR: dict[str, tuple[str, ...]] = {}
#: custom id -> (char, pack)
_BY_ID: dict[str, tuple[str, str]] = {}

for _pack_name, _pack in PACKS.items():
    for _char, _cid in _pack:
        _BY_ID.setdefault(_cid, (_char, _pack_name))
        bucket = _BY_CHAR.setdefault(_char, ())
        if _cid not in bucket:
            _BY_CHAR[_char] = (*bucket, _cid)


def _first(char: str) -> tuple[str | None, str | None]:
    """First ``(custom_id, pack)`` for a character, preferring later packs."""
    ids = _BY_CHAR.get(char)
    if not ids:
        return None, None
    cid = ids[0]
    info = _BY_ID.get(cid)
    return cid, (info[1] if info else None)


# ---------------------------------------------------------------------------
#  semantic aliases: name -> emoji character
# ---------------------------------------------------------------------------
# The bot asks for a *role* ("moderation", "success"), and the registry picks a
# real character with a custom id behind it.
_ALIASES: dict[str, str] = {
    # moderation / guardian
    "shield": "🛡", "guard": "🛡", "guardian": "🛡", "protect": "🛡",
    "stop": "⛔️", "blocked": "⛔️", "denied": "⛔️",
    "ban": "🚫", "banned": "🚫", "forbidden": "🚫",
    "warn": "⚠️", "warning": "⚠️", "alert": "⚠️", "caution": "⚠️",
    "kick": "🦶", "boot": "🦶", "remove": "🦶",
    "ban_hammer": "🔨", "hammer": "🔨",
    "police": "🚨", "siren": "🚨", "raid": "🚨",
    "mute": "🔇", "silent": "🔇", "quiet": "🔇",
    "clean": "🧹", "cleanup": "🧹", "watchdog": "🧹", "purge": "🧹",
    "eye": "👁", "watch": "👁", "monitor": "👁", "review": "👁",
    "spy": "🕵️", "detective": "🔍", "scan": "🔍", "search": "🔍",
    "lock": "🔒", "locked": "🔒", "secure": "🔒",
    "unlock": "🔓", "unlocked": "🔓",
    "key": "🔑", "token": "🔑",
    "door": "🚪", "enter": "🚪", "leave": "🚪", "exit": "🚪",
    # status
    "check": "✅", "done": "✅", "ok": "✅", "success": "✅", "yes": "✅", "active": "✅",
    "cross": "❌", "fail": "❌", "error": "❌", "no": "❌", "off": "❌",
    "tick": "✔️", "valid": "✔️", "verified": "✔️",
    "info": "ℹ️", "help": "ℹ️", "about": "ℹ️",
    "question": "❓", "ask": "❓", "unknown": "❓",
    "bang": "❗", "important": "❗", "urgent": "❗️", "notice": "❗️",
    "new": "🆕", "fresh": "🆕", "soon": "🔜", "upcoming": "🔜",
    "free": "🆓", "cool": "🆒", "ok_hand": "🆗", "fine": "🆗",
    # people / ranks
    "crown": "👑", "owner": "👑", "admin": "👑", "king": "👑",
    "robot": "🤖", "bot": "🤖", "ai": "🤖",
    "ghost": "👻", "anonymous": "👻", "anon": "👻",
    "ninja": "🥷", "hacker": "🥷",
    "user": "👤", "member": "👤", "profile": "👤", "account": "👤",
    "wave": "👋", "hi": "👋", "hello": "👋", "welcome": "👋",
    "thumbs_up": "👍", "like": "👍", "approve": "👍", "good": "👍",
    "thumbs_down": "👎", "dislike": "👎", "bad": "👎",
    "muscle": "💪", "strong": "💪",
    "brain": "🧠", "smart": "🧠", "think": "🧠",
    "clown": "🤡", "jester": "🤡", "troll": "🤡", "scam": "🤡",
    "salute": "🫡", "respect": "🫡",
    "pray": "🙏", "thanks": "🙏", "please": "🙏",
    "medal_gold": "🥇", "gold": "🥇", "first": "🥇", "winner": "🥇",
    "medal_silver": "🥈", "silver": "🥈", "second": "🥈",
    "medal_bronze": "🥉", "bronze": "🥉", "third": "🥉",
    "trophy": "🏆", "top": "🏆", "leaderboard": "🏆",
    "star": "⭐️", "stars": "⭐️", "favorite": "⭐️", "premium": "⭐️",
    "sparkle": "✨", "sparkles": "✨", "magic": "✨", "shine": "✨",
    "diamond": "💎", "gem": "💎", "vip": "💎", "luxury": "💎",
    "hundred": "💯", "perfect": "💯", "score": "💯",
    "skull": "💀", "dead": "💀", "game_over": "💀", "rip": "💀",
    "tombstone": "🪦", "grave": "🪦",
    # actions
    "fire": "🔥", "hot": "🔥", "streak": "🔥", "burn": "🔥",
    "rocket": "🚀", "deploy": "🚀", "launch": "🚀", "fast": "🚀",
    "boom": "💥", "explode": "💥", "impact": "💥", "crash": "💥",
    "zap": "⚡️", "lightning": "⚡️", "fast2": "⚡️", "instant": "⚡️",
    "refresh": "🔄", "reload": "🔄", "retry": "🔄", "sync": "🔄",
    "repost": "🔁", "loop": "🔁", "repeat": "🔁",
    "plus": "➕", "add": "➕", "create": "➕", "new_entry": "➕",
    "trash": "🗑", "delete": "🗑", "remove2": "🗑",
    "edit": "✏️", "write": "✏️", "pencil": "✏️", "rename": "✏️",
    "save": "💾", "backup": "💾",
    "download": "⬇️", "down": "⬇️", "get": "⬇️", "import": "⬇️",
    "upload": "⬆️", "up": "⬆️", "send_up": "⬆️", "export": "⬆️",
    "outbox": "📤", "forward": "📤", "share": "📤",
    "link": "🔗", "url": "🔗", "chain": "🔗",
    "copy": "©", "copyright": "©", "clipboard": "📋",
    "pin": "📌", "pinned": "📌", "pushpin": "📌",
    "bookmark": "🔖", "tag": "🏷", "label": "🏷",
    "arrow_right": "➡️", "next": "➡️", "forward2": "➡️",
    "arrow_left": "⬅️", "back": "⬅️", "previous": "⬅️",
    "arrow_up": "🔼", "more": "🔼", "collapse": "🔼",
    "arrow_down": "🔽", "less": "🔽", "expand": "🔽",
    "top_arrow": "🔝", "to_top": "🔝", "best": "🔝",
    "play": "▶️", "start_play": "▶️", "resume": "▶️",
    "pause": "⏸", "hold": "⏸",
    "stop_square": "⏹", "end": "⏹",
    "timer": "⌛", "wait": "⌛", "loading": "⌛", "hourglass": "⌛",
    "clock": "⏰", "time": "⏰", "alarm": "⏰", "reminder": "⏰",
    "bell": "🔔", "notify": "🔔", "notification": "🔔",
    "bell_off": "🔕", "mute_notify": "🔕",
    # data / docs
    "chart": "📊", "stats": "📊", "analytics": "📊", "report": "📊",
    "chart_up": "📈", "growth": "📈", "increase": "📈", "trend_up": "📈",
    "chart_down": "📉", "decline": "📉", "decrease": "📉", "trend_down": "📉",
    "memo": "📝", "note": "📝", "log": "📝", "draft": "📝",
    "file": "📄", "document": "📄", "page": "📄",
    "folder": "📁", "dir": "📁", "directory": "📁",
    "folder_open": "📂", "browse": "📂", "files": "📂",
    "archive": "🗂", "storage": "🗂",
    "clip": "📎", "attach": "📎", "attachment": "📎",
    "box": "📦", "package": "📦", "build": "📦",
    "book": "📖", "docs": "📖", "guide": "📖", "readme": "📖",
    "news": "📰", "announce": "📰", "changelog": "📰",
    "megaphone": "📣", "broadcast": "📣", "announcement": "📣",
    "speech": "💬", "chat": "💬", "message": "💬", "comment": "💬",
    "thought": "💭", "thinking": "💭", "idea2": "💭",
    "mail": "✉️", "email": "✉️", "dm": "✉️",
    "inbox2": "📥", "received": "📥",
    "globe": "🌐", "web": "🌐", "internet": "🌐", "network": "🌐",
    "computer": "🖥", "server": "🖥", "host": "🖥", "vps": "🖥",
    "gear": "⚙️", "settings": "⚙️", "config": "⚙️", "cog": "⚙️",
    "wrench": "🔧", "tools": "🔧", "fix": "🔧", "maintenance": "🔧",
    "dials": "🎛", "control": "🎛", "panel": "🎛",
    "keyboard": "⌨", "input": "⌨", "typing": "⌨",
    "compass": "🧭", "navigate": "🧭",
    "puzzle": "🧩", "plugin": "🧩", "module": "🧩",
    "flask": "🧪", "test": "🧪", "experiment": "🧪", "lab": "🧪",
    "scope": "🔬", "inspect": "🔬", "debug": "🔬",
    "satellite": "📡", "signal": "📶", "connection": "📶", "ping": "📶",
    "cloud": "☁", "cloud_up": "☁",
    "battery": "🔋", "power": "🔋", "energy": "🔋",
    "plug": "🔌", "disconnect": "🔌",
    "antenna": "📡", "mtproto": "📡",
    # money / stars
    "money": "💰", "stars_wallet": "💰", "balance": "💰", "wallet": "💰",
    "cash": "💵", "payment": "💵", "invoice": "💵", "price": "💵",
    "money_wings": "💸", "refund": "💸", "spend": "💸", "payout": "💸",
    "exchange": "💱", "convert": "💱", "rate": "💱",
    "card": "💳", "credit": "💳",
    "gift": "🎁", "present": "🎁", "reward": "🎁",
    "gem_star": "💎",
    # media
    "photo": "📷", "camera": "📷", "screenshot": "📷",
    "image": "🖼", "picture": "🖼", "art": "🖼",
    "video_cam": "🎥", "video": "🎥", "record": "🎥",
    "tv": "📺", "stream": "📺", "channel": "📺",
    "music": "🎵", "audio": "🎵", "song": "🎵", "track": "🎵",
    "notes_music": "🎶", "melody": "🎶", "playlist": "🎶",
    "mic": "🎤", "voice": "🎤", "sing": "🎤",
    "studio_mic": "🎙", "podcast": "🎙",
    "guitar": "🎸", "riff": "🎸", "band": "🎸",
    "drum": "🎼", "score_music": "🎼", "sound": "🔈",
    "headphones": "🎧", "listen": "🎧",
    "movie": "🎬", "clip2": "🎬", "action": "🎬",
    # time / weather / nature
    "calendar": "🗓", "schedule": "🗓", "date": "🗓", "daily": "🗓",
    "sun": "☀️", "day": "☀️", "bright": "☀️",
    "moon": "🌙", "night": "🌙", "sleep": "🌙", "quiet_hours": "🌙",
    "moon2": "🌛", "evening": "🌛",
    "rain": "🌧", "storm": "🌩", "snow": "❄️", "winter": "❄️",
    "rainbow": "🌈", "colorful": "🌈", "pride": "🌈",
    "drop": "💧", "water": "💧",
    "leaf": "🍃", "eco": "🍃", "green": "🍃",
    "tree": "🌲", "nature": "🌲",
    "palm": "🌴", "island": "🌴", "tropical": "🌴",
    "flower": "🌹", "rose": "🌹", "beauty": "🌹",
    "blossom": "🌺", "hibiscus": "🌺",
    "seedling": "🌱", "grow": "🌱", "new_start": "🌱",
    "earth": "🌍", "world": "🌍", "global": "🌍",
    "comet": "☄️", "shooting_star": "☄️", "meteor": "☄️",
    "orbit": "🪐", "planet": "🪐", "space": "🪐",
    "ufo": "🛸", "alien": "🛸", "unknown2": "🛸",
    "alien_monster": "👾", "invader": "👾",
    "milky_way": "🌌", "galaxy": "🌌",
    # objects
    "home": "🏠", "house": "🏠", "main_menu": "🏠", "dashboard": "🏠",
    "building": "🏡", "site": "🏡",
    "plane": "✈️", "travel": "✈️", "airplane": "✈️",
    "helicopter": "🚁", "chopper": "🚁", "air": "🚁",
    "car": "🚗", "drive": "🚗", "target": "🎯",
    "game": "🎮", "controller": "🎮", "play_game": "🎮",
    "dice": "🎲", "random": "🎲", "roll": "🎲", "luck": "🎲",
    "cards": "🃏", "joker": "🃏", "wildcard": "🃏",
    "crystal": "🔮", "predict": "🔮", "future": "🔮", "oracle": "🔮",
    "balloon": "🎈", "party": "🎈", "celebrate": "🎈",
    "confetti": "🎉", "congrats": "🎉", "tada": "🎉",
    "fireworks": "🎆", "show": "🎆", "event": "🎆",
    "flag": "🚩", "milestone": "🚩", "marker": "🚩",
    "location": "📍", "place": "📍", "geo": "📍", "point": "📍",
    "pushpin_drop": "📌", "map": "🗺", "atlas": "🗺",
    "phone": "📞", "call": "📞", "support": "📞", "contact": "📞",
    "phone_mobile": "📱", "mobile": "📱", "app": "📱",
    "desktop": "🖥",
    "timer2": "⏳", "sand": "⏳",
    "vial": "💉", "inject": "💉",
    "pill": "💊", "medicine": "💊",
    "wine": "🍷", "drink": "🍷", "toast": "🥂",
    "coffee": "☕", "break": "☕", "cafe": "☕",
    "food": "🍽", "dish": "🍽", "meal": "🍽",
    "fork": "🍴", "eat": "🍴", "dinner": "🍴",
    "pot": "🍲", "cook": "🍲", "soup": "🍲",
    "cake": "🍰", "dessert": "🍰", "birthday": "🍰",
    "candy": "🍬", "sweet": "🍬",
    "cookie": "🍪", "snack": "🍪",
    "mushroom": "🍄", "boost": "🍄", "power_up": "🍄",
    "seed": "🌰", "nut": "🌰",
    "eggplant": "🍑", "peach": "🍑",
    "check_heart": "🫀", "heart_anatomy": "🫀",
    "heart": "❤️", "love": "❤️", "like2": "❤️",
    "heart_blue": "💙", "blue": "💙", "cool_blue": "💙",
    "heart_purple": "💜", "purple": "💜",
    "heart_light_blue": "🩵", "cyan": "🩵",
    "heart_fire": "❤️‍🔥", "passion": "❤️‍🔥", "burning_love": "❤️‍🔥",
    "heart_broken": "💔", "heartbreak": "💔", "sad": "💔",
    "heart_sparkle": "💖", "adore": "💖",
    "heart_gift": "💝", "valentine": "💝",
    "kiss": "💋", "lips": "💋",
    "wink": "🫦", "flirt": "🫦",
    "eyes": "👀", "look": "👀", "seen": "👀",
    "cool_face": "😎", "sunglasses": "😎",
    "smile": "🙂", "happy": "🙂",
    "laugh": "😂", "lol": "😂", "funny": "😂",
    "party_face": "🥳", "yay": "🥳",
    "angel": "😇", "innocent": "😇",
    "smirk": "😏", "sly": "😏",
    "neutral": "😐", "meh": "😐",
    "shush": "🤫", "secret": "🤫", "silent2": "🤫",
    "shock": "😱", "scream": "😱", "panic": "😱",
    "mind_blown": "🤯", "wow": "🤯",
    "smiling_devil": "😈", "devil": "😈", "evil": "😈",
    "goblin": "👺", "tengu": "👺",
    "sneezing": "🤧", "sick": "🤧",
    "sleeping": "💤", "zzz": "💤", "idle": "💤",
    "bat": "🦇", "vampire": "🦇",
    "wolf": "🐺", "lone_wolf": "🐺",
    "dragon": "🐉", "mythic": "🐉",
    "unicorn": "🦄", "rare": "🦄", "exclusive": "🦄",
    "butterfly": "🦋", "transform": "🦋", "change": "🦋",
    "frog": "🐸", "pepe": "🐸", "meme": "🐸",
    "whale": "🐋", "big": "🐋", "huge": "🐋",
    "goat": "🐐", "greatest": "🐐", "legend": "🐐",
    "crab": "🦀", "rust2": "🦀",
    "cat_black": "🐈‍⬛", "cat": "🐈", "meow": "🐈",
    "paw": "🐾", "pet": "🐾", "walk": "🐾",
    "ant": "🐜", "small": "🐜", "tiny": "🐜",
    "worm": "🪱", "creepy": "🪱",
    "bug": "🪳", "roach": "🪳", "pest": "🪳",
    "spider": "🕷", "web2": "🕸",
    "dove": "🕊", "peace": "🕊", "calm": "🕊",
    "god": "🙏", "spirit": "👼",
    "skeleton": "🦴", "bone": "🦴",
    "candle": "🕯", "memory": "🕯", "respect2": "🕯",
    "coffin": "⚰️", "buried": "⚰️",
    "microbe": "🦠", "virus": "🦠", "malware": "🦠",
    "bomb": "💣", "danger": "💣", "exploit": "💣",
    "radioactive": "☢️", "nuke": "☢️", "toxic": "☢️",
    "knife": "🔪", "cut": "🔪", "slice": "🔪",
    "gun": "🔫", "attack": "🔫", "shot": "🔫",
    "axe": "🪓", "chop": "🪓",
    "crystal_ball": "🔮",
    "hourglass_done": "⌛",
}


# Names used by the button/panel layer. Anything whose character is not part of a
# pack still works - it simply renders as the plain emoji.
_UI_ALIASES: dict[str, str] = {
    "users": "👥", "group": "👥", "groups": "👥", "members": "👥", "community": "👥",
    "list": "📋", "queue": "📋", "menu": "📋", "index": "📋",
    "broom2": "🧹", "tidy": "🧹", "maintenance": "🧹",
    "handshake": "🤝", "deal": "🤝", "agree": "🤝",
    "satellite": "📡", "ping": "📡", "network": "📡", "online": "📡",
    "heart_pulse": "💗", "pulse": "💗", "health": "💗", "alive": "💗",
    "book": "📖", "docs": "📖", "guide": "📖", "readme": "📖", "manual": "📖",
    "folder": "📁", "files": "📁", "archive": "📁",
    "flask": "🧪", "lab": "🧪", "test": "🧪", "experiment": "🧪",
    "calendar": "📅", "date": "📅", "schedule": "📅", "daily": "📅",
    "target": "🎯", "goal": "🎯", "aim": "🎯", "focus": "🎯",
    "dice": "🎲", "random": "🎲", "luck": "🎲", "roll": "🎲",
    "puzzle": "🧩", "plugin": "🧩", "module": "🧩",
    "filter": "🧲", "magnet": "🧲", "trigger": "🧲", "filters": "🧲",
    "medal": "🏅", "award": "🏅", "achievement": "🏅",
    "camera": "📸", "screenshot": "📸", "photo": "📸",
    "speech": "💬", "chat": "💬", "message": "💬", "comment": "💬",
    "inbox": "📨", "mail": "📨", "dm": "📨",
    "stopwatch": "⏱", "duration": "⏱", "elapsed": "⏱",
    "battery": "🔋", "power": "🔋", "energy": "🔋",
    "magnifier": "🔎", "inspect": "🔎", "details": "🔎",
    "gamepad": "🎮", "arcade": "🎮", "games": "🎮",
    "receipt": "🧾", "invoice": "🧾", "report2": "🧾",
    "frame": "🖼", "image": "🖼", "art": "🖼",
    "clapper": "🎬", "video": "🎬", "movie": "🎬", "gif": "🎬",
    "pin_round": "📍", "location": "📍", "place": "📍",
    "stop2": "🛑", "halt": "🛑", "emergency": "🛑",
    "shield_check": "🛡", "trusted": "🛡", "verified2": "🛡",
}

# Build the registry: every semantic alias resolved to a character that exists in
# the packs (or plain if the character is not in any pack).
EMOJI: dict[str, Emoji] = {}
aliases: dict[str, str] = {}

for _name, _char in {**_UI_ALIASES, **_ALIASES}.items():
    _cid, _pack = _first(_char)
    EMOJI.setdefault(
        _name,
        Emoji(name=_name, char=_char, custom_id=_cid, pack=_pack),
    )
    aliases.setdefault(_name.lower(), _name)

# Add a friendly entry for every distinct character in the packs, so anything can
# be requested even without a semantic alias.
for _char, _ids in _BY_CHAR.items():
    _cid = _ids[0]
    _pack = _BY_ID.get(_cid, (None, None))[1]
    _key = f"char:{_char}"
    EMOJI.setdefault(_key, Emoji(name=_key, char=_char, custom_id=_cid, pack=_pack))

_CUSTOM_ENABLED = True


# ---------------------------------------------------------------------------
#  public API
# ---------------------------------------------------------------------------


def set_custom_enabled(enabled: bool) -> None:
    """Global switch - mirrors ``CUSTOM_EMOJI`` in the config.

    Set to ``False`` when the bot owner has no Telegram Premium: the bot then
    sends the plain fallback characters everywhere.
    """
    global _CUSTOM_ENABLED
    _CUSTOM_ENABLED = bool(enabled)


def custom_enabled() -> bool:
    return _CUSTOM_ENABLED


def names() -> tuple[str, ...]:
    """Every semantic name (excludes the auto-generated ``char:`` entries)."""
    return tuple(sorted(n for n in EMOJI if not n.startswith("char:")))


def total_custom_ids() -> int:
    """How many distinct custom emoji ids are available."""
    return count()


def pack_of(name_or_char: str) -> str | None:
    entry = resolve(name_or_char)
    return entry.pack if entry else None


def by_char(char_text: str, *, variant: int = 0) -> Emoji | None:
    """Look up an emoji by its character, choosing among its pack variants.

    ``variant`` is clamped, so ``by_char("🔥", variant=99)`` returns the last one.
    """
    text = str(char_text or "").strip()
    ids = _BY_CHAR.get(text)
    if not ids:
        return None
    index = max(0, min(int(variant), len(ids) - 1))
    cid = ids[index]
    pack = _BY_ID.get(cid, (None, None))[1]
    return Emoji(name=f"char:{text}", char=text, custom_id=cid, pack=pack)


def by_id(custom_id: str) -> Emoji | None:
    """Reverse lookup: build an emoji from a raw ``custom_emoji_id``."""
    info = _BY_ID.get(str(custom_id))
    if info is None:
        return None
    char, pack = info
    return Emoji(name=f"char:{char}", char=char, custom_id=str(custom_id), pack=pack)


def variants(char_text: str) -> tuple[str, ...]:
    """Every custom id available for a character (in pack order)."""
    return _BY_CHAR.get(str(char_text or "").strip(), ())


def resolve(name: str) -> Emoji | None:
    """Look up by semantic name, ``char:🔥`` key, alias, or raw emoji character."""
    key = str(name or "").strip()
    if not key:
        return None
    lowered = key.lower()
    if lowered in aliases:
        return EMOJI.get(aliases[lowered])
    if key.startswith("char:") and key in EMOJI:
        return EMOJI[key]
    if key in EMOJI:
        return EMOJI[key]
    return by_char(key)


def char(name: str, default: str = "") -> str:
    """The plain fallback character for ``name``.

    Unknown non-emoji names give ``default``; an emoji character passes through
    unchanged so ``char("🎯")`` works.
    """
    entry = resolve(name)
    if entry:
        return entry.char
    text = str(name or "")
    if text and ord(text[0]) > 0x2000:  # already an emoji/symbol
        return text
    return default


def emoji(name: str, *, custom: bool | None = None, default: str = "") -> str:
    """Render ``name`` as rich HTML: ``<tg-emoji>`` when possible, else plain.

    >>> emoji("location")
    '<tg-emoji emoji-id="5104989658350093220">📍</tg-emoji>'
    >>> set_custom_enabled(False); emoji("location")
    '📍'
    """
    entry = resolve(name)
    if entry is None:
        return char(name, default)
    use_custom = _CUSTOM_ENABLED if custom is None else custom
    return entry.html(custom=use_custom)


def search(query: str, *, limit: int = 40) -> list[Emoji]:
    """Find emoji by name/tag/character substring - powers the ``/emojis`` picker."""
    needle = str(query or "").strip().lower().lstrip(":")
    if not needle:
        return [EMOJI[name] for name in names()][:limit]
    hits: list[Emoji] = []
    seen: set[str] = set()
    for key, entry in EMOJI.items():
        if key.startswith("char:") and needle not in entry.char and needle not in entry.name:
            continue
        if needle in key.lower() or (entry.tags and any(needle in t for t in entry.tags)):
            if entry.name not in seen:
                seen.add(entry.name)
                hits.append(entry)
    return hits[:limit]


_EMOJI_IN_TEXT = re.compile(
    "|".join(re.escape(ch) for ch in sorted(_BY_CHAR, key=len, reverse=True) if ch)
) if _BY_CHAR else None


def upgrade_text(text: str, *, custom: bool | None = None) -> str:
    """Replace every known emoji character in ``text`` with its custom-emoji tag.

    Handy for making old plain-text strings premium-aware::

        upgrade_text("Bot is ✅ online")   # ✅ becomes <tg-emoji …>✅</tg-emoji>
    """
    if _EMOJI_IN_TEXT is None or (custom is False) or (custom is None and not _CUSTOM_ENABLED):
        return text
    return _EMOJI_IN_TEXT.sub(
        lambda m: emoji(m.group(0), custom=custom) or m.group(0), text
    )
