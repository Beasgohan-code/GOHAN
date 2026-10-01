"""Button colours and automatic palette assignment.

Bot API 9.4 added coloured inline buttons, and Rich Messages 10.3 accept the
same ``style`` values plus ``link``::

    primary  blue
    success  green
    danger   red
    link     the accent colour, only valid together with callback data

GOHAN's house rule is that **no button is ever left unstyled**. Instead of
hand-picking a colour for every button, call :func:`auto_style` (or use
:func:`cycle` / the ``colors_for`` helper) and let the palette decide, so a row
of buttons always looks deliberate.

:func:`semantic_style` picks a colour that matches the action's meaning, which is
what most screens actually want: destructive actions red, confirmations green,
navigation blue.
"""

from __future__ import annotations

from itertools import cycle as _cycle
from typing import Iterable, Iterator

__all__ = [
    "ALL_STYLES",
    "DANGER",
    "LINK",
    "PRIMARY",
    "SUCCESS",
    "auto_style",
    "colors_for",
    "cycle",
    "normalize",
    "semantic_style",
]

PRIMARY = "primary"
SUCCESS = "success"
DANGER = "danger"
LINK = "link"

#: Every colour Telegram supports, in a pleasant order for cycling.
ALL_STYLES: tuple[str, ...] = (PRIMARY, SUCCESS, DANGER, LINK)

#: Words that should make a button red / green / blue, checked in order.
_SEMANTICS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "delete", "remove", "ban", "kick", "block", "stop", "cancel", "reset",
            "purge", "wipe", "clear", "unban", "unmute", "unwarn", "leave",
            "disable", "close", "reject", "deny", "report", "abort", "off",
        ),
        DANGER,
    ),
    (
        (
            "save", "confirm", "yes", "ok", "approve", "accept", "enable", "start",
            "run", "play", "join", "subscribe", "verify", "done", "add", "create",
            "new", "send", "submit", "post", "publish", "allow", "on", "buy",
        ),
        SUCCESS,
    ),
    (
        ("docs", "help", "guide", "readme", "source", "github", "website", "open", "visit"),
        LINK,
    ),
)


def normalize(style: str | None) -> str | None:
    """Return a valid style, or ``None`` if the value is unknown/empty."""
    if not style:
        return None
    value = str(style).strip().lower()
    return value if value in ALL_STYLES else None


def semantic_style(label: str, *, callback: str | None = None, style: str | None = None) -> str:
    """Choose a colour from the button's meaning.

    An explicit ``style`` always wins. Otherwise the label and callback data are
    scanned for keywords (``delete`` -> danger, ``save`` -> success, …) and the
    result falls back to ``primary``.

    >>> semantic_style("🗑 Delete")
    'danger'
    >>> semantic_style("✅ Save")
    'success'
    >>> semantic_style("Open docs")
    'link'
    """
    explicit = normalize(style)
    if explicit:
        return explicit
    haystack = f"{label} {callback or ''}".lower()
    for words, colour in _SEMANTICS:
        if any(word in haystack for word in words):
            return colour
    return PRIMARY


def cycle(styles: Iterable[str] | None = None) -> Iterator[str]:
    """An infinite iterator over the colour palette.

    >>> from itertools import islice
    >>> list(islice(cycle(), 5))
    ['primary', 'success', 'danger', 'link', 'primary']
    """
    pool = tuple(normalize(s) or PRIMARY for s in styles) if styles else ALL_STYLES
    return _cycle(pool or ALL_STYLES)


def colors_for(count: int, *, styles: Iterable[str] | None = None) -> list[str]:
    """``count`` colours, cycling through the palette."""
    pool = [normalize(s) or PRIMARY for s in styles] if styles else list(ALL_STYLES)
    if not pool:
        pool = list(ALL_STYLES)
    return [pool[i % len(pool)] for i in range(max(0, count))]


def auto_style(
    label: str,
    *,
    callback: str | None = None,
    url: str | None = None,
    style: str | None = None,
    index: int | None = None,
) -> str:
    """The colour for one button.

    ``index`` (its position in a row) is used when the label carries no meaning,
    so a grid of generic buttons still comes out multicoloured.
    """
    explicit = normalize(style)
    if explicit:
        return explicit
    if url:
        return LINK
    colour = semantic_style(label, callback=callback)
    if colour == PRIMARY and index is not None:
        # Nothing meaningful in the label: cycle so rows stay colourful.
        return ALL_STYLES[index % len(ALL_STYLES)]
    return colour
