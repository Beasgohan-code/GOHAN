"""Small-caps / stylised text.

Telegram has no small-caps font, so the trick used by every well-dressed bot is
to swap letters for their Unicode small-capital look-alikes::

    "gohan"  ->  "ɢᴏʜᴀɴ"

That is what :func:`small_caps` does. A few other styles are provided for
headers and accents. All of them are *lossy* transforms - never use them for
data the bot has to parse back (file names, tokens, commands…), only for
decoration.

Everything degrades gracefully: characters without a mapping are passed
through untouched, so mixed text (``/start``, ``ID 123``, emoji) stays readable.
"""

from __future__ import annotations

import re

__all__ = [
    "SMALL_CAPS",
    "apply_style",
    "bold_serif",
    "double_struck",
    "fullwidth",
    "monospace",
    "sanitize_display_name",
    "small_caps",
    "small_caps_keep_emoji",
    "styles",
    "title_case_small",
]

# --- the small caps alphabet ------------------------------------------------
# 'q' has no true small capital in Unicode; ǫ (o with ogonek) is the conventional
# stand-in and is what most Telegram bots use.
SMALL_CAPS: dict[str, str] = {
    "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ", "g": "ɢ",
    "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ",
    "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ", "s": "ꜱ", "t": "ᴛ", "u": "ᴜ",
    "v": "ᴠ", "w": "ᴡ", "x": "x", "y": "ʏ", "z": "ᴢ",
    # Uppercase maps to the same small-capital glyphs: there is no separate
    # uppercase form, and "GOHAN" should render as "ɢᴏʜᴀɴ" just like "gohan".
    "A": "ᴀ", "B": "ʙ", "C": "ᴄ", "D": "ᴅ", "E": "ᴇ", "F": "ꜰ", "G": "ɢ",
    "H": "ʜ", "I": "ɪ", "J": "ᴊ", "K": "ᴋ", "L": "ʟ", "M": "ᴍ", "N": "ɴ",
    "O": "ᴏ", "P": "ᴘ", "Q": "ǫ", "R": "ʀ", "S": "ꜱ", "T": "ᴛ", "U": "ᴜ",
    "V": "ᴠ", "W": "ᴡ", "X": "x", "Y": "ʏ", "Z": "ᴢ",
}

_MATH_BOLD = {
    "a": "𝐚", "b": "𝐛", "c": "𝐜", "d": "𝐝", "e": "𝐞", "f": "𝐟", "g": "𝐠",
    "h": "𝐡", "i": "𝐢", "j": "𝐣", "k": "𝐤", "l": "𝐥", "m": "𝐦", "n": "𝐧",
    "o": "𝐨", "p": "𝐩", "q": "𝐪", "r": "𝐫", "s": "𝐬", "t": "𝐭", "u": "𝐮",
    "v": "𝐯", "w": "𝐰", "x": "𝐱", "y": "𝐲", "z": "𝐳",
    "A": "𝐀", "B": "𝐁", "C": "𝐂", "D": "𝐃", "E": "𝐄", "F": "𝐅", "G": "𝐆",
    "H": "𝐇", "I": "𝐈", "J": "𝐉", "K": "𝐊", "L": "𝐋", "M": "𝐌", "N": "𝐍",
    "O": "𝐎", "P": "𝐏", "Q": "𝐐", "R": "𝐑", "S": "𝐒", "T": "𝐓", "U": "𝐔",
    "V": "𝐕", "W": "𝐖", "X": "𝐗", "Y": "𝐘", "Z": "𝐙",
}
_DOUBLE_STRUCK = {
    "a": "𝕒", "b": "𝕓", "c": "𝕔", "d": "𝕕", "e": "𝕖", "f": "𝕗", "g": "𝕘",
    "h": "𝕙", "i": "𝕚", "j": "𝕛", "k": "𝕜", "l": "𝕝", "m": "𝕞", "n": "𝕟",
    "o": "𝕠", "p": "𝕡", "q": "𝕢", "r": "𝕣", "s": "𝕤", "t": "𝕥", "u": "𝕦",
    "v": "𝕧", "w": "𝕨", "x": "𝕩", "y": "𝕪", "z": "𝕫",
    "A": "𝔸", "B": "𝔹", "C": "ℂ", "D": "𝔻", "E": "𝔼", "F": "𝔽", "G": "𝔾",
    "H": "ℍ", "I": "𝕀", "J": "𝕁", "K": "𝕂", "L": "𝕃", "M": "𝕄", "N": "ℕ",
    "O": "𝕆", "P": "ℙ", "Q": "ℚ", "R": "ℝ", "S": "𝕊", "T": "𝕋", "U": "𝕌",
    "V": "𝕍", "W": "𝕎", "X": "𝕏", "Y": "𝕐", "Z": "ℤ",
    "0": "𝟘", "1": "𝟙", "2": "𝟚", "3": "𝟛", "4": "𝟜", "5": "𝟝", "6": "𝟞",
    "7": "𝟟", "8": "𝟠", "9": "𝟡",
}
_MONOSPACE = {
    "a": "𝚊", "b": "𝚋", "c": "𝚌", "d": "𝚍", "e": "𝚎", "f": "𝚏", "g": "𝚐",
    "h": "𝚑", "i": "𝚒", "j": "𝚓", "k": "𝚔", "l": "𝚕", "m": "𝚖", "n": "𝚗",
    "o": "𝚘", "p": "𝚙", "q": "𝚚", "r": "𝚛", "s": "𝚜", "t": "𝚝", "u": "𝚞",
    "v": "𝚟", "w": "𝚠", "x": "𝚡", "y": "𝚢", "z": "𝚣",
    "A": "𝙰", "B": "𝙱", "C": "𝙲", "D": "𝙳", "E": "𝙴", "F": "𝙵", "G": "𝙶",
    "H": "𝙷", "I": "𝙸", "J": "𝙹", "K": "𝙺", "L": "𝙻", "M": "𝙼", "N": "𝙽",
    "O": "𝙾", "P": "𝙿", "Q": "𝚀", "R": "𝚁", "S": "𝚂", "T": "𝚃", "U": "𝚄",
    "V": "𝚅", "W": "𝚆", "X": "𝚇", "Y": "𝚈", "Z": "𝚉",
    "0": "𝟶", "1": "𝟷", "2": "𝟸", "3": "𝟹", "4": "𝟺", "5": "𝟻", "6": "𝟼",
    "7": "𝟽", "8": "𝟾", "9": "𝟿",
}

_STYLES: dict[str, dict[str, str]] = {
    "smallcaps": SMALL_CAPS,
    "bold": _MATH_BOLD,
    "double": _DOUBLE_STRUCK,
    "mono": _MONOSPACE,
}

styles = tuple(_STYLES)

#: Emoji / symbol ranges we must never try to "style" (it would break them).
_KEEP_RE = re.compile(
    "["
    "\U0001f000-\U0001ffff"  # emoji, pictographs, symbols
    "\U00002190-\U000021ff"  # arrows
    "\U00002460-\U000024ff"  # enclosed alphanumerics (ⓐ ① …)
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U0001f3fb-\U0001f3ff"  # skin tones
    "\u200d"                 # zero width joiner
    "]+"
)


def apply_style(text: str, style: str = "smallcaps") -> str:
    """Map every mapped character of ``text`` into ``style``.

    Unknown characters (emoji, punctuation, digits for ``smallcaps``) are copied
    through unchanged.

    >>> apply_style("gohan 2")
    'ɢᴏʜᴀɴ 2'
    """
    table = _STYLES.get(style, SMALL_CAPS)
    return "".join(table.get(ch, ch) for ch in str(text))


def small_caps(text: object) -> str:
    """``ɢᴏʜᴀɴ``-style rendering of ``text``. The house style for menus/titles."""
    return apply_style(str(text), "smallcaps")


def small_caps_keep_emoji(text: object) -> str:
    """Like :func:`small_caps` but never touches emoji sequences.

    Useful when a string mixes words and emoji and the emoji contains letters
    (flag sequences, keycaps) that would otherwise be mangled.
    """
    out: list[str] = []
    last = 0
    for match in _KEEP_RE.finditer(str(text)):
        out.append(apply_style(str(text)[last : match.start()], "smallcaps"))
        out.append(match.group(0))
        last = match.end()
    out.append(apply_style(str(text)[last:], "smallcaps"))
    return "".join(out)


def title_case_small(text: object) -> str:
    """Small caps with the first letter of each word kept visually distinct."""
    return small_caps(str(text))


def bold_serif(text: object) -> str:
    """𝐁𝐨𝐥𝐝 𝐬𝐞𝐫𝐢𝐟 - good for numbers that must stand out."""
    return apply_style(str(text), "bold")


def double_struck(text: object) -> str:
    """𝔻𝕠𝕦𝕓𝕝𝕖 𝕤𝕥𝕣𝕦𝕔𝕜 - decorative headers."""
    return apply_style(str(text), "double")


def monospace(text: object) -> str:
    """𝙼𝚘𝚗𝚘𝚜𝚙𝚊𝚌𝚎 letters (not real code formatting)."""
    return apply_style(str(text), "mono")


def fullwidth(text: object) -> str:
    """Ｆｕｌｌｗｉｄｔｈ － wide, spaced-out look."""
    out = []
    for ch in str(text):
        code = ord(ch)
        if 0x21 <= code <= 0x7E:
            out.append(chr(code + 0xFEE0))
        elif ch == " ":
            out.append("\u3000")
        else:
            out.append(ch)
    return "".join(out)


def sanitize_display_name(value: object, max_len: int = 64) -> str:
    """Make a Telegram display name safe to interpolate into styled text.

    Strips control characters and bidirectional overrides (which are used to
    spoof other users in logs / scoreboards) and clamps the length.
    """
    text = str(value or "").strip()
    text = "".join(
        ch
        for ch in text
        if ch == "\n" or (ord(ch) >= 32 and not (0x202A <= ord(ch) <= 0x202E) and ord(ch) != 0x200F)
    )
    text = text.replace("\n", " ")
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text or "unknown"
