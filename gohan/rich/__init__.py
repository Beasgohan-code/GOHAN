"""Rich Messages for GOHAN.

Two backends, one output format:

* the bot's **aiogram** client sends rich messages through
  ``Bot.send_rich_message`` (:mod:`gohan.rich.sender`), and
* the **Kurigram** MTProto client through `richgram
  <https://github.com/Badmunda05/richgram>`_.

Both speak the same HTML dialect, so anything built here renders identically on
either path.

Quick start::

    from gohan.rich import ui, emoji

    await rich_send(bot, chat_id, ui.screen(
        "GOHAN",
        ui.kv_panel([("Status", ui.ok("online")), ("Version", "0.1.0")], icon="robot"),
    ))
"""

from __future__ import annotations

from . import emoji, text, ui
from ._richgram import HAS_RICHGRAM, RICH_AVAILABLE
from .text import (
    apply_style,
    bold_serif,
    double_struck,
    fullwidth,
    monospace,
    sanitize_display_name,
    small_caps,
    small_caps_keep_emoji,
)

__all__ = [
    "HAS_RICHGRAM",
    "RICH_AVAILABLE",
    "apply_style",
    "bold_serif",
    "double_struck",
    "emoji",
    "fullwidth",
    "monospace",
    "sanitize_display_name",
    "small_caps",
    "small_caps_keep_emoji",
    "text",
    "ui",
]
