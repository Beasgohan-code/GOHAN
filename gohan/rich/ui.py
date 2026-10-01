"""GOHAN's visual language.

One place that decides how the bot looks, so every screen agrees with every
other screen:

* **Titles** are headings, rendered in small caps: ``ɢᴜᴀʀᴅɪᴀɴ``.
* **Data** lives in ``<blockquote>`` panels, or in tables when it is tabular.
* **Labels** inside a panel are small caps + bold, values stay normal so IDs,
  names and numbers stay copyable and readable.
* **Emoji** come from the custom-emoji registry (:mod:`gohan.rich.emoji`), so
  they upgrade to Telegram's animated custom emoji for free when the bot owner
  has Premium, and fall back to plain emoji otherwise.

Everything here returns an **HTML string** suitable for a rich message. Compose
freely, then hand the result to :func:`gohan.rich.sender.rich_send` (aiogram) or
``richgram.rich_send`` (Kurigram).

Example::

    from gohan.rich import ui

    body = ui.screen(
        "Guardian",
        icon="shield",
        blocks=[
            ui.kv_panel([
                ("Status", ui.ok("active")),
                ("Members", "1 204"),
                ("Warnings", "3"),
            ]),
            ui.rule(),
            ui.footer_text("ᴀɪsᴀᴄ + ᴋᴜʀɪɢʀᴀᴍ"),
        ],
    )
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from . import emoji as _emoji
from . import palette as _palette
from ._richgram import (
    rich_anchor,
    rich_button,
    rich_button_row,
    rich_code,
    rich_details,
    rich_esc,
    rich_footer,
    rich_heading,
    rich_hr,
    rich_list,
    rich_math,
    rich_note,
    rich_pre,
    rich_pull_quote,
    rich_table,
    rich_to_plain,
)
from .text import small_caps, small_caps_keep_emoji

__all__ = [
    "action_bar",
    "actions",
    "anchor",
    "badge",
    "bold",
    "button",
    "caps",
    "code",
    "details",
    "divider",
    "e",
    "esc",
    "footer_text",
    "h",
    "hr",
    "italic",
    "kv",
    "kv_panel",
    "link",
    "list_ordered",
    "list_unordered",
    "math",
    "mention",
    "muted",
    "nav_bar",
    "ok",
    "panel",
    "plain",
    "pre",
    "pull_quote",
    "quote",
    "screen",
    "scoreboard",
    "spoiler",
    "strike",
    "table",
    "underline",
]

# ---------------------------------------------------------------------------
#  escaping + inline primitives
# ---------------------------------------------------------------------------


def esc(value: Any) -> str:
    """HTML-escape untrusted text (user names, file names, log lines)."""
    return rich_esc(value)


def caps(value: Any) -> str:
    """Small caps for display text. Escapes first, so it is safe on user input."""
    return small_caps(rich_esc(value))


def caps_keep_emoji(value: Any) -> str:
    """Small caps that leaves emoji sequences alone."""
    return small_caps_keep_emoji(rich_esc(value))


def e(name: str, *, custom: bool | None = None) -> str:
    """A (custom) emoji by registry name, e.g. ``e("guardian")``."""
    return _emoji.emoji(name, custom=custom)


def bold(text: Any) -> str:
    return f"<b>{text}</b>"


def muted(text: Any) -> str:
    """Small caps + escapes - the standard way to render a label/de-emphasised text."""
    return caps(text)


def italic(text: Any) -> str:
    return f"<i>{text}</i>"


def underline(text: Any) -> str:
    return f"<u>{text}</u>"


def strike(text: Any) -> str:
    return f"<s>{text}</s>"


def spoiler(text: Any) -> str:
    return f"<tg-spoiler>{text}</tg-spoiler>"


def marked(text: Any) -> str:
    return f"<mark>{text}</mark>"


def code(value: Any) -> str:
    return rich_code(value)


def pre(value: Any, language: str | None = None) -> str:
    return rich_pre(value, language)


def math(expression: str) -> str:
    return rich_math(expression)


def link(text: Any, url: str) -> str:
    return f'<a href="{rich_esc(url)}">{text}</a>'


def mention(text: Any, user_id: int | str) -> str:
    """A clickable user mention that works even when the user has no username."""
    return link(text, f"tg://user?id={user_id}")


def plain(html_text: str) -> str:
    """Strip the markup back to readable text (used by the plain-text fallback)."""
    return rich_to_plain(html_text)


# ---------------------------------------------------------------------------
#  blocks
# ---------------------------------------------------------------------------


def h(text: Any, level: int = 1, *, icon: str | None = None, caps_text: bool = True) -> str:
    """A heading. ``icon`` is an emoji-registry name prepended to the title."""
    label = caps(text) if caps_text else esc(text)
    if icon:
        return rich_heading(f"{e(icon)} {label}", level=level)
    return rich_heading(label, level=level)


def panel(*lines: Any, expandable: bool = False, icon: str | None = None) -> str:
    """A ``<blockquote>`` panel - the default container for data."""
    body = "\n".join(str(line) for line in lines if line not in (None, ""))
    if icon:
        body = f"{e(icon)}\n{body}"
    return rich_note(body, expandable=expandable)


def kv(key: Any, value: Any) -> str:
    """One ``label: value`` line, label in small caps + bold."""
    return f"{bold(caps(key))}: {value}"


def kv_panel(
    pairs: Iterable[tuple[Any, Any]],
    *,
    icon: str | None = None,
    expandable: bool = False,
    sep: str = "\n",
) -> str:
    """A panel of ``label: value`` lines."""
    lines = [kv(key, value) for key, value in pairs]
    body = sep.join(lines)
    if icon:
        body = f"{e(icon)}\n{body}"
    return rich_note(body, expandable=expandable)


def table(
    headers: Sequence[Any] | None,
    rows: Iterable[Sequence[Any]],
    *,
    border: int = 1,
    caps_headers: bool = True,
    raw: bool = False,
) -> str:
    """A real Telegram table.

    Cells are HTML-escaped for safety. Pass ``raw=True`` when they already
    contain markup (``<code>``, ``ui.esc(user_text)`` …) - table cells accept
    inline formatting, so links, code and bold all work inside them.
    """
    head = None
    if headers:
        head = [caps(cell) if caps_headers else (cell if raw else esc(cell)) for cell in headers]
    body = [[(cell if raw else esc(cell)) for cell in row] for row in rows]
    return rich_table(head, body, border=border)


def divider() -> str:
    return rich_hr()


hr = divider


def footer_text(text: Any, *, icon: str | None = None, caps_text: bool = True) -> str:
    label = caps(text) if caps_text else esc(text)
    return rich_footer(f"{e(icon)} {label}" if icon else label)


def quote(text: Any, author: Any | None = None) -> str:
    """A pull quote, optionally credited to ``author``."""
    body = str(text)
    if author:
        body += f"<cite>{esc(author)}</cite>"
    return rich_pull_quote(body)


def details(summary: Any, body: Any, *, open: bool = False, icon: str | None = None) -> str:
    """Collapsible ``<details>`` block - great for changelogs and raw payloads."""
    label = caps(summary)
    if icon:
        label = f"{e(icon)} {label}"
    return rich_details(label, str(body), open=open)


def list_unordered(items: Iterable[Any]) -> str:
    return rich_list([str(item) for item in items], ordered=False)


def list_ordered(items: Iterable[Any]) -> str:
    return rich_list([str(item) for item in items], ordered=True)


def anchor(name: str) -> str:
    return rich_anchor(name)


def screen(
    title: Any,
    *blocks: Any,
    blocks_: Iterable[Any] | None = None,
    icon: str | None = None,
    level: int = 1,
    subtitle: Any | None = None,
) -> str:
    """Title + subtitle + blocks, joined with blank lines.

    Both ``screen("x", b1, b2)`` and ``screen("x", blocks=[b1, b2])`` work.
    """
    parts: list[str] = [h(title, level=level, icon=icon)]
    if subtitle:
        parts.append(italic(caps(subtitle)))
    if blocks_ is not None:
        parts.extend(str(b) for b in blocks_)
    parts.extend(str(b) for b in blocks if b not in (None, ""))
    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
#  buttons (rich button rows live *inside* the message body)
# ---------------------------------------------------------------------------


def button(
    label: Any,
    *,
    callback: str | None = None,
    url: str | None = None,
    style: str | None = None,
    icon: str | None = None,
    data: str | None = None,
    index: int | None = None,
) -> str:
    """A rich-message button.

    **Every button gets a colour** - never a default grey one. If ``style`` is
    omitted the palette picks one from the label's meaning
    (:func:`gohan.rich.palette.semantic_style`), falling back to cycling.

    ``style`` is one of ``primary`` (blue), ``success`` (green), ``danger``
    (red) or ``link`` (accent, callback buttons only).
    """
    text = str(label)
    if icon:
        text = f"{e(icon)} {text}"
    colour = _palette.auto_style(
        text, callback=callback or data, url=url, style=style, index=index
    )
    if callback or data:
        return rich_button(text, callback_data=callback or data, style=colour)
    return rich_button(text, url=url, style=colour)


def actions(*rows: Iterable[str] | str, align: str | None = None) -> str:
    """One or more button rows of already-built buttons.

    ``actions([b1, b2], [b3])`` or ``actions(b1, b2)`` (single row).
    """
    if not rows:
        return ""
    if all(isinstance(row, str) for row in rows):
        return rich_button_row(*rows, align=align)  # type: ignore[arg-type]
    out: list[str] = []
    for row in rows:
        if isinstance(row, str):
            out.append(rich_button_row(row, align=align))
        else:
            out.append(rich_button_row(*row, align=align))
    return "".join(out)


def action_bar(
    items: Iterable[tuple[str, str] | tuple[str, str, str] | dict[str, Any]],
    *,
    per_row: int = 2,
    align: str | None = None,
    icons: Iterable[str] | None = None,
) -> str:
    """Build a coloured button grid from simple tuples.

    Accepts ``(label, callback)``, ``(label, callback, style)`` or dicts with the
    keys understood by :func:`button`. Colours are assigned automatically, so no
    button is ever left the client default::

        ui.action_bar([
            ("Save", "cfg:save"),                 # -> success (keyword)
            ("Delete", "cfg:del"),                # -> danger  (keyword)
            ("Open docs", "cfg:docs"),            # -> link
        ])
    """
    icon_list = list(icons) if icons is not None else []
    built: list[str] = []
    for position, item in enumerate(items):
        icon = icon_list[position] if position < len(icon_list) else None
        if isinstance(item, dict):
            built.append(button(index=position, icon=icon, **item))
        elif len(item) == 3:  # type: ignore[arg-type]
            label, target, style = item  # type: ignore[misc]
            built.append(button(label, callback=target, style=style, icon=icon, index=position))
        else:
            label, target = item  # type: ignore[misc]
            built.append(button(label, callback=target, icon=icon, index=position))

    rows: list[list[str]] = []
    step = max(1, int(per_row))
    for start in range(0, len(built), step):
        rows.append(built[start : start + step])
    return actions(*rows, align=align)


def nav_bar(
    *items: tuple[str, str],
    back: tuple[str, str] | None = None,
    close: tuple[str, str] | None = None,
    align: str | None = None,
) -> str:
    """Navigation footer: the given items, then Back (blue) and Close (red)."""
    row = [button(label, callback=target, style=_palette.PRIMARY, icon=None) for label, target in items]
    tail: list[str] = []
    if back:
        tail.append(button(back[0], callback=back[1], style=_palette.PRIMARY))
    if close:
        tail.append(button(close[0], callback=close[1], style=_palette.DANGER))
    rows = [row] if row else []
    if tail:
        rows.append(tail)
    return actions(*rows, align=align) if rows else ""


# ---------------------------------------------------------------------------
#  small status helpers used all over the bot
# ---------------------------------------------------------------------------


def ok(text: Any) -> str:
    """✅ green-ish 'yes'."""
    return f"{e('check')} {text}"


def no(text: Any) -> str:
    """❌ red-ish 'no'."""
    return f"{e('cross')} {text}"


def badge(value: bool, *, yes: str = "on", no_: str = "off") -> str:
    """``✅ ᴏɴ`` / ``❌ ᴏꜰꜰ`` in small caps."""
    return f"{e('check' if value else 'cross')} {caps(yes if value else no_)}"


def scoreboard(
    title: Any,
    headers: Sequence[Any],
    rows: Iterable[Sequence[Any]],
    *,
    icon: str | None = None,
    note: Any | None = None,
) -> str:
    """A game/leaderboard screen: heading + table + optional footer note."""
    parts = [h(title, level=2, icon=icon or "trophy"), table(headers, rows)]
    if note:
        parts.append(footer_text(note, caps_text=False))
    return "\n\n".join(parts)


def uptime(seconds: float) -> str:
    """``3ᴅ 4ʜ 12ᴍ`` - small caps, compact."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        text = f"{days}d {hours}h {minutes}m"
    elif hours:
        text = f"{hours}h {minutes}m"
    else:
        text = f"{minutes}m {secs}s"
    return caps(text)


def bytes_human(size: float) -> str:
    """``1.4 ᴍʙ``."""
    step = 1024.0
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            return caps(f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} {unit}")
        value /= step
    return caps(f"{value:.1f} TB")
