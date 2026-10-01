"""Coloured **inline** keyboards (classic ``reply_markup`` buttons).

Rich messages carry buttons inside the body (see :func:`gohan.rich.ui.button`);
this module is the other half of the story - real ``InlineKeyboardMarkup``
buttons for screens that need per-chat state, confirmation flows and lists.

Two Bot API 9.4+ features are used on every button:

* ``style``  - ``primary`` (blue), ``success`` (green), ``danger`` (red), ``link``
* ``icon_custom_emoji_id`` - the icon on the button face, taken from the
  :mod:`gohan.rich.emoji` registry (600 ids), so buttons match the rich text.

House rule: **no button is ever left unstyled.**
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import emoji as registry
from .palette import DANGER, LINK, PRIMARY, SUCCESS, normalize, semantic_style

__all__ = [
    "CONFIRM_NO",
    "CONFIRM_YES",
    "button",
    "confirm",
    "grid",
    "keyboard",
    "row",
    "with_back",
]

CONFIRM_YES = "ui:confirm"
CONFIRM_NO = "ui:cancel"


def _icon_id(icon: str | None) -> str | None:
    """Turn a registry name (``"check"``) into a custom emoji id, if we have one."""
    if not icon:
        return None
    if icon.isdigit():
        return icon
    entry = registry.resolve(icon)
    return entry.custom_id if entry is not None else None


def button(
    label: Any,
    *,
    callback: str | None = None,
    url: str | None = None,
    style: str | None = None,
    icon: str | None = None,
    disabled: bool = False,
) -> InlineKeyboardButton:
    """One coloured inline button.

    ``callback`` for callback data, ``url`` for links (styled ``link`` by
    default). The colour is chosen from the label's meaning when ``style`` is
    omitted, so the keyboard always looks intentional.
    """
    text = str(label)
    chosen = normalize(style) or semantic_style(text, callback=callback, style=style)
    if url and not style:
        chosen = LINK
    return InlineKeyboardButton(
        text=text,
        callback_data=callback if callback is not None else None,
        url=url,
        style=chosen,
        icon_custom_emoji_id=_icon_id(icon),
        disabled=disabled or None,
    )


def row(*buttons: InlineKeyboardButton) -> list[InlineKeyboardButton]:
    return list(buttons)


def keyboard(*rows: Sequence[InlineKeyboardButton] | InlineKeyboardButton) -> InlineKeyboardMarkup:
    """Build a markup from rows (single buttons become their own row)."""
    built: list[list[InlineKeyboardButton]] = []
    for item in rows:
        if isinstance(item, InlineKeyboardButton):
            built.append([item])
        else:
            built.append(list(item))
    return InlineKeyboardMarkup(inline_keyboard=built)


def grid(
    items: Iterable[tuple[str, str] | tuple[str, str, str] | dict[str, Any]],
    *,
    per_row: int = 2,
    icons: Iterable[str] | None = None,
) -> InlineKeyboardMarkup:
    """``[(label, callback), …]`` (or dicts) laid out in rows of ``per_row``."""
    icon_list = list(icons) if icons is not None else []
    built: list[InlineKeyboardButton] = []
    for position, item in enumerate(items):
        icon = icon_list[position] if position < len(icon_list) else None
        if isinstance(item, dict):
            payload = dict(item)
            payload.setdefault("icon", icon)
            built.append(button(**payload))
        elif len(item) == 3:  # type: ignore[arg-type]
            label, target, style = item  # type: ignore[misc]
            if target.startswith(("http://", "https://", "tg://")):
                built.append(button(label, url=target, style=style, icon=icon))
            else:
                built.append(button(label, callback=target, style=style, icon=icon))
        else:
            label, target = item  # type: ignore[misc]
            if target.startswith(("http://", "https://", "tg://")):
                built.append(button(label, url=target, icon=icon))
            else:
                built.append(button(label, callback=target, icon=icon))

    rows = [built[i : i + max(1, per_row)] for i in range(0, len(built), max(1, per_row))]
    return keyboard(*rows)


def confirm(
    yes: str,
    *,
    no: str = CONFIRM_NO,
    yes_label: str = "Yes, do it",
    no_label: str = "Cancel",
    yes_icon: str = "check",
    no_icon: str = "cross",
) -> InlineKeyboardMarkup:
    """A green/red confirmation pair - used before anything destructive."""
    return keyboard(
        [button(yes_label, callback=yes, style=SUCCESS, icon=yes_icon)],
        [button(no_label, callback=no, style=DANGER, icon=no_icon)],
    )


def with_back(
    rows: Sequence[Sequence[InlineKeyboardButton]],
    *,
    back: str | None = None,
    close: str | None = None,
    home: str | None = None,
) -> InlineKeyboardMarkup:
    """Append a navigation footer to an existing keyboard."""
    tail: list[InlineKeyboardButton] = []
    if home:
        tail.append(button("Home", callback=home, style=PRIMARY, icon="house"))
    if back:
        tail.append(button("Back", callback=back, style=PRIMARY, icon="arrow_left"))
    if close:
        tail.append(button("Close", callback=close, style=DANGER, icon="cross"))
    return keyboard(*rows, *([tail] if tail else []))


def toggle_row(
    pairs: Iterable[tuple[str, bool, str]],
    *,
    per_row: int = 2,
) -> InlineKeyboardMarkup:
    """``[(label, enabled, callback_prefix)]`` -> green/red toggle buttons."""
    items: list[tuple[str, str, str]] = []
    for label, enabled, prefix in pairs:
        items.append((("✓ " if enabled else "✕ ") + label, f"{prefix}{label.lower()}", SUCCESS if enabled else DANGER))
    return grid(items, per_row=per_row)
