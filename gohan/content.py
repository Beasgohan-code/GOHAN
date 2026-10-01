"""All user-visible copy, in one place.

Keeping strings together means the bot can be re-worded (or translated) without
touching handler logic, and it guarantees the house style is consistent: every
title is small caps, every body sits in a blockquote panel or a table.

Fun-action tables also live here: the flavour text and the animation source for
each ``/hug``-style command.
"""

from __future__ import annotations

from typing import Final

from .rich import ui

__all__ = [
    "AI_HELP",
    "FILTERS_HELP",
    "FUN_ACTIONS",
    "GAMES_HELP",
    "GUARDIAN_HELP",
    "HELP_SECTIONS",
    "REACTIONS",
    "QUIZ_QUESTIONS",
    "SCAM_PATTERNS",
    "SPAM_PHRASES",
    "WELCOME_DEFAULT",
    "action_caption",
    "help_screen",
]

# ---------------------------------------------------------------------------
#  help
# ---------------------------------------------------------------------------

HELP_SECTIONS: Final[dict[str, dict[str, object]]] = {
    "guardian": {
        "title": "guardian",
        "icon": "shield",
        "blurb": "keeps the group clean - joins, spam, raids, warnings",
        "commands": [
            ("/guardian", "this panel - what is on and what is off"),
            ("/captcha on|off", "new members must tap a button before they can talk"),
            ("/antiraid on|off", "auto-lockdown when a raid is detected"),
            ("/antispam on|off", "block scam patterns, links, forwards, floods"),
            ("/welcome <text>", "welcome message ({name}, {chat}, {id})"),
            ("/rules", "show (or set) the group rules"),
        ],
    },
    "moderation": {
        "title": "moderation",
        "icon": "hammer",
        "blurb": "day-to-day cleanup for admins",
        "commands": [
            ("/warn [reason]", "warn a user (reply) - 3 warnings = ban"),
            ("/warns", "list warnings (reply to a user)"),
            ("/unwarn", "clear every warning from a user"),
            ("/mute [1h|1d|perm]", "restrict a user"),
            ("/unmute", "restore a user"),
            ("/kick", "remove and let them rejoin"),
            ("/ban [reason]", "remove permanently"),
            ("/unban", "lift a ban by id or username"),
            ("/purge [n]", "delete the last N messages (or a quoted range)"),
            ("/lockdown", "freeze the chat instantly"),
            ("/reports", "see who has been reported"),
        ],
    },
    "games": {
        "title": "games",
        "icon": "game",
        "blurb": "challenges with a per-chat leaderboard",
        "commands": [
            ("/quiz", "multiple choice, 20 s per question"),
            ("/guess", "guess the number I am thinking of"),
            ("/chain", "word chain - each word starts with the last letter"),
            ("/dice [@user]", "roll against someone (real Telegram dice)"),
            ("/slot", "spin the machine"),
            ("/top [game]", "leaderboard"),
            ("/me", "your scores"),
        ],
    },
    "fun": {
        "title": "fun",
        "icon": "sparkles",
        "blurb": "eighteen ways to be affectionate, rude or dramatic",
        "commands": [
            ("/hug", "affection (reply or @mention to target someone)"),
            ("/kiss /cuddle /pat", "more affection"),
            ("/slap /bonk /poke", "the opposite of affection"),
            ("/dance /cry /laugh /blush", "self-expression"),
            ("/highfive /handshake /wink", "friendly gestures"),
            ("/bite /tickle /snuggle /boop /nom", "the rest of the family"),
            ("/actions", "the whole list in one panel"),
        ],
    },
    "ai": {
        "title": "assistant",
        "icon": "robot",
        "blurb": "answers stream in live, then land as a rich message",
        "commands": [
            ("/ask <question>", "one-off question (streamed answer)"),
            ("/ai on|off", "let me answer every message in this chat"),
            ("/ai prompt <text>", "set my personality for this chat"),
            ("/ai reset", "forget this chat's conversation"),
            ("/summary", "summarise the last messages in this chat"),
            ("/translate <lang> <text>", "quick translation"),
        ],
    },
    "notes": {
        "title": "notes",
        "icon": "memo",
        "blurb": "little snippets a chat can reuse",
        "commands": [
            ("/save <name> <text>", "store a note (admins)"),
            ("/notes", "list every note in this chat"),
            ("#name", "type it to print the note"),
        ],
    },
    "filters": {
        "title": "filters",
        "icon": "magnet",
        "blurb": "automatic replies to keywords - admins manage, owner can lock down",
        "commands": [
            ("/filter <trigger> <reply>", "add or replace a filter"),
            ("/filter <trigger> (reply)", "reply to media to save it as the answer"),
            ("/stop <trigger>", "delete one filter"),
            ("/stopall", "delete every filter (asks first)"),
            ("/filters", "the panel: list, search, delete"),
            ("/filtermode admin|owner", "who may manage here (bot owner only)"),
        ],
    },
    "anime": {
        "title": "anime",
        "icon": "clapper",
        "blurb": "powered by AniList - no api key needed",
        "commands": [
            ("/anime <title>", "rich card: score, studio, synopsis, next episode"),
            ("/trending", "what is hot right now, with number buttons"),
            ("/character <name>", "character card with artwork"),
        ],
    },
    "music": {
        "title": "music",
        "icon": "headphones",
        "blurb": "search YouTube and deliver the audio",
        "commands": [
            ("/music <query>", "search - tap a number to get the file"),
            ("/download <query>", "fetch and send the best match"),
        ],
    },
    "owner": {
        "title": "owner",
        "icon": "crown",
        "blurb": "only for the bot owner",
        "commands": [
            ("/owner", "the control panel - buttons for everything"),
            ("/groups", "every chat I am in"),
            ("/restart confirm", "restart the process safely"),
            ("/shutdown confirm", "stop the bot (the owner is told)"),
            ("/backup", "download a sqlite backup"),
            ("/watchdog [clean|run]", "watchdog report or force a sweep"),
            ("/report", "daily report now"),
            ("/status", "the panel with live counters"),
            ("/stats", "tables, games and event counts"),
            ("/broadcast <text>", "send to every known user"),
            ("/maintenance on|off", "pause the bot for regular users"),
            ("/emojis [query]", "browse the custom-emoji registry"),
            ("/backup", "sqlite backup file"),
        ],
    },
}


def help_screen(tag: str | None = None, *, is_owner: bool = False) -> str:
    """``/help`` - either one section or the index."""
    if tag and tag in HELP_SECTIONS:
        section = HELP_SECTIONS[tag]
        rows = [(f"<code>{cmd}</code>", ui.esc(desc)) for cmd, desc in section["commands"]]  # type: ignore[index]
        return ui.screen(
            section["title"],  # type: ignore[arg-type]
            icon=section["icon"],  # type: ignore[arg-type]
            subtitle=section["blurb"],  # type: ignore[arg-type]
            blocks_=[
                ui.table(["ᴄᴏᴍᴍᴀɴᴅ", "ᴡʜᴀᴛ ɪᴛ ᴅᴏᴇꜱ"], rows, raw=True),
                ui.footer_text("tap /help for the index"),
            ],
        )

    sections = [k for k in HELP_SECTIONS if k != "owner"] + (["owner"] if is_owner else [])
    rows = [
        (f"/help {key}", str(HELP_SECTIONS[key]["blurb"])) for key in sections
    ]
    return ui.screen(
        "help",
        icon="book",
        subtitle=f"{len(HELP_SECTIONS)} sections · every command is documented",
        blocks_=[
            ui.table(["ꜱᴇᴄᴛɪᴏɴ", "ᴡʜᴀᴛ ɪᴛ ᴅᴏᴇꜱ"], rows),
            ui.kv_panel(
                [
                    ("🛡 ɢᴜᴀʀᴅɪᴀɴ", "auto - just add me as an admin"),
                    ("🔮 ᴀɪ ᴄʜᴀᴛ", "streamed, rich formatted answers"),
                    ("🎬 ᴀɴɪᴍᴇ", "anilist cards and trending"),
                    ("🎧 ᴍᴜꜱɪᴄ", "search and download audio"),
                    ("🧲 ꜰɪʟᴛᴇʀꜱ", "keyword auto-replies"),
                    ("🎮 ɢᴀᴍᴇꜱ", "leaderboards per group"),
                ],
                icon="sparkles",
            ),
            ui.actions(
                [
                    ui.button("Guardian", callback="help:guardian", icon="shield"),
                    ui.button("Games", callback="help:games", icon="game"),
                ],
                [
                    ui.button("Anime", callback="help:anime", icon="clapper"),
                    ui.button("Music", callback="help:music", icon="headphones"),
                ],
                [
                    ui.button("Filters", callback="help:filters", icon="magnet"),
                    ui.button("Assistant", callback="help:ai", icon="robot"),
                ],
                [ui.button("Add to group", url="https://t.me/", style="link", icon="plus")],
            ),
        ],
    )


GUARDIAN_HELP = help_screen("guardian")
GAMES_HELP = help_screen("games")
AI_HELP = help_screen("ai")
FILTERS_HELP = help_screen("filters")

WELCOME_DEFAULT: Final[str] = (
    "👋 ᴡᴇʟᴄᴏᴍᴇ {name} to {chat}!\n"
    "read the rules with /rules · /help shows everything I can do."
)

# ---------------------------------------------------------------------------
#  fun actions
# ---------------------------------------------------------------------------
# ``api`` is the nekos.best category; ``None`` means "card only" (no animation).
# ``emoji`` is an emoji-registry name, so the bot uses custom emoji when allowed.
# ``lines`` are picked at random and may use {from} and {to}.

FUN_ACTIONS: Final[dict[str, dict[str, object]]] = {
    "hug": {
        "api": "hug", "emoji": "heart", "self": "🫂 {from} hugs everyone in the room",
        "lines": ["{from} wraps {to} in a warm hug 🤗", "{from} hugs {to} tightly 💞",
                  "{to} gets a big bear hug from {from} 🐻"],
        "verb": "hugged",
    },
    "kiss": {
        "api": "kiss", "emoji": "kiss", "self": "💋 {from} blows a kiss to the chat",
        "lines": ["{from} kisses {to} 😘", "{from} steals a kiss from {to} 💋",
                  "{to} didn't see that kiss coming from {from} 💞"],
        "verb": "kissed",
    },
    "cuddle": {
        "api": "cuddle", "emoji": "heart_purple", "self": "🫶 {from} cuddles a pillow",
        "lines": ["{from} cuddles up with {to} 🥰", "{from} and {to} are cuddling 💜"],
        "verb": "cuddled",
    },
    "pat": {
        "api": "pat", "emoji": "handshake", "self": "🫳 {from} pats the air",
        "lines": ["{from} pats {to} on the head 🥹", "{to} gets gentle headpats from {from} 🫳"],
        "verb": "patted",
    },
    "slap": {
        "api": "slap", "emoji": "fire", "self": "🖐 {from} slaps the void",
        "lines": ["{from} slaps {to} with a trout 🐟", "{to} got slapped into next week by {from} 🖐"],
        "verb": "slapped",
    },
    "bonk": {
        "api": "bonk", "emoji": "hammer", "self": "🔨 {from} bonks themselves",
        "lines": ["{from} bonks {to} 🔨", "bonk! {to} was hit by {from} 🔨"],
        "verb": "bonked",
    },
    "poke": {
        "api": "poke", "emoji": "pointer", "self": "👉 {from} pokes the air",
        "lines": ["{from} pokes {to} 👉", "{to} keeps getting poked by {from} 👉"],
        "verb": "poked",
    },
    "highfive": {
        "api": "highfive", "emoji": "handshake", "self": "🙌 {from} high-fives the air",
        "lines": ["{from} high-fives {to} 🙌", "clean high-five between {from} and {to} ✋"],
        "verb": "high-fived",
    },
    "handshake": {
        "api": "handshake", "emoji": "handshake", "self": "🤝 {from} shakes hands with the void",
        "lines": ["{from} shakes hands with {to} 🤝", "{from} and {to} agree on something 🤝"],
        "verb": "shook hands with",
    },
    "tickle": {
        "api": "tickle", "emoji": "sparkles", "self": "🪶 {from} tickles themselves",
        "lines": ["{from} tickles {to} 🪶", "{to} can't stop laughing, thanks to {from} 😂"],
        "verb": "tickled",
    },
    "bite": {
        "api": "bite", "emoji": "fork", "self": "😬 {from} bites their own hand",
        "lines": ["{from} bites {to} 😬", "{to} was bitten by {from} 🧛"],
        "verb": "bit",
    },
    "snuggle": {
        "api": "snuggle", "emoji": "heart", "self": "🧸 {from} snuggles a blanket",
        "lines": ["{from} snuggles up against {to} 🧸", "{from} and {to} are snuggling 🥰"],
        "verb": "snuggled",
    },
    "boop": {
        "api": "poke", "emoji": "pointer", "self": "👆 {from} boops themselves",
        "lines": ["{from} boops {to}'s nose 👆", "{to} got booped by {from} 👆"],
        "verb": "booped",
    },
    "nom": {
        "api": "nom", "emoji": "fork", "self": "🍽 {from} nom noms",
        "lines": ["{from} nom noms on {to} 🍽", "{to} is being eaten by {from} 😋"],
        "verb": "nommed",
    },
    "dance": {
        "api": "dance", "emoji": "music", "self": "💃 {from} dances alone (still counts)",
        "lines": ["{from} dances with {to} 💃", "{from} and {to} are tearing up the dance floor 🕺"],
        "verb": "danced with",
    },
    "cry": {
        "api": "cry", "emoji": "drop", "self": "😭 {from} cries in the corner",
        "lines": ["{from} cries on {to}'s shoulder 😭", "{to} comforts {from} 🥲"],
        "verb": "cried on",
    },
    "laugh": {
        "api": "laugh", "emoji": "laugh", "self": "😂 {from} laughs at nothing",
        "lines": ["{from} laughs at {to} 😂", "{from} and {to} can't stop laughing 🤣"],
        "verb": "laughed at",
    },
    "blush": {
        "api": "blush", "emoji": "heart_fire", "self": "😳 {from} blushes",
        "lines": ["{from} blushes because of {to} 😳", "{to} made {from} blush 🥰"],
        "verb": "blushed at",
    },
    "wink": {
        "api": "wink", "emoji": "wink", "self": "😉 {from} winks at the chat",
        "lines": ["{from} winks at {to} 😉", "{to} received a wink from {from} 😏"],
        "verb": "winked at",
    },
}

REACTIONS: Final[dict[str, tuple[str, ...]]] = {
    "hug": ("🫂", "🤗", "💞"),
    "kiss": ("💋", "😘", "💞"),
    "slap": ("🐟", "🖐", "💥"),
    "dance": ("💃", "🕺", "🎶"),
    "cry": ("😭", "🥲", "💧"),
    "laugh": ("😂", "🤣", "😆"),
}


def action_caption(action: str, sender: str, target: str | None, *, seed: int = 0) -> str:
    """Pick and format a random line for a fun action."""
    spec = FUN_ACTIONS.get(action)
    if not spec:
        return f"{sender} {action}s"
    if target is None:
        return str(spec["self"]).format(from_=sender)
    lines = spec["lines"]  # type: ignore[assignment]
    text = lines[seed % len(lines)] if seed else lines[0]
    return str(text).format(from_=sender, to=target)


# ---------------------------------------------------------------------------
#  quiz bank (kept general, no copyrighted content)
# ---------------------------------------------------------------------------

QUIZ_QUESTIONS: Final[tuple[tuple[str, tuple[str, ...], int], ...]] = (
    ("Which planet is closest to the Sun?", ("Mercury", "Venus", "Earth", "Mars"), 0),
    ("What is the largest ocean on Earth?", ("Atlantic", "Indian", "Pacific", "Arctic"), 2),
    ("How many bits are in a byte?", ("4", "8", "16", "32"), 1),
    ("Which language does the Telegram Bot API natively speak?", ("JSON over HTTPS", "SOAP", "gRPC", "XML-RPC"), 0),
    ("What does CPU stand for?", ("Central Process Unit", "Central Processing Unit", "Computer Personal Unit", "Control Processing Unit"), 1),
    ("Which is the longest river in the world?", ("Amazon", "Nile", "Yangtze", "Mississippi"), 1),
    ("Which element has the symbol 'Au'?", ("Silver", "Gold", "Aluminium", "Argon"), 1),
    ("In what year did Telegram launch?", ("2011", "2013", "2015", "2009"), 1),
    ("What is 12 * 12?", ("124", "132", "144", "154"), 2),
    ("Which country has the most time zones?", ("Russia", "USA", "France", "China"), 2),
    ("What does HTTP stand for?", ("HyperText Transfer Protocol", "High Transfer Text Protocol", "HyperText Transmission Process", "Host Transfer Type Protocol"), 0),
    ("Which gas do plants absorb?", ("Oxygen", "Nitrogen", "Carbon dioxide", "Helium"), 2),
    ("How many players are on a football pitch per team?", ("9", "10", "11", "12"), 2),
    ("Which is the smallest prime number?", ("0", "1", "2", "3"), 2),
    ("What is the capital of Japan?", ("Osaka", "Tokyo", "Kyoto", "Nagoya"), 1),
    ("Who wrote 'Romeo and Juliet'?", ("Dickens", "Shakespeare", "Tolstoy", "Homer"), 1),
    ("What does 'AI' stand for?", ("Automatic Input", "Artificial Intelligence", "Advanced Interface", "Applied Informatics"), 1),
    ("Which is the hardest natural substance?", ("Iron", "Diamond", "Quartz", "Gold"), 1),
    ("How many sides does a hexagon have?", ("5", "6", "7", "8"), 1),
    ("Which planet is known as the Red Planet?", ("Venus", "Mars", "Jupiter", "Saturn"), 1),
)

# ---------------------------------------------------------------------------
#  guardian heuristics
# ---------------------------------------------------------------------------
# Deliberately conservative patterns: they run on every message, so a false
# positive is worse than a missed scam. Anything uncertain is reported to admins
# instead of deleted.

SCAM_PATTERNS: Final[tuple[str, ...]] = (
    r"\b(?:free|bonus|gift)\s+(?:crypto|bitcoin|btc|usdt|eth)\b",
    r"\b(?:investment|trading)\s+(?:bot|platform|signal)",
    r"\bdoubl(?:e|ing)\s+your\s+(?:money|btc|usdt)",
    r"\bguaranteed\s+(?:profit|returns?|income)",
    r"\b(?:join|click)\s+(?:this\s+)?(?:link|channel)\s+to\s+(?:claim|win|get)",
    r"\bwithdraw(?:al)?\s+(?:limit|fee|blocked)",
    r"\bairdrop\s+(?:live|now|claim)",
    r"\bseed\s+phrase\b|\bprivate\s+key\b|\bconnect\s+(?:your\s+)?wallet\b",
    r"\b(?:pump|signal)\s+group\b|\bvip\s+signals?\b",
    r"\btelegram\s+premium\s+free\b|\bfree\s+stars?\b",
    r"\bwork\s+from\s+home\s+\$?\d+|earn\s+\$\d+\s+(?:daily|hourly|per day)",
    r"\b(?:hack|cloned?)\s+(?:account|card|whatsapp)\b",
    r"\bonly\s+\d+\s+spots?\s+left\b.*\b(?:invest|join)\b",
)

SPAM_PHRASES: Final[tuple[str, ...]] = (
    "buy followers", "increase your followers", "crypto signals",
    "trading academy", "loan offer", "instant loan", "casino bonus",
    "1xbet", "mostbet", "aviator predictor", "melbet", "betting id",
    "escort service", "sex chat", "dating now",
)

#: Forwarded-source usernames that usually mean trouble in a clean group.
SUSPICIOUS_FORWARD_SOURCES: Final[tuple[str, ...]] = ()
