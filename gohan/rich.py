"""richsend — Bot API 10.x Rich Message (sendRichMessage) helpers.

Single place that knows how to turn post text into ``InputRichMessage``
payloads, how to shape the composer keyboard and which canned cards the
bot sends (welcome / help / status / 10.1–10.3 showcase).
"""

from __future__ import annotations

import html as _html
import re
from typing import Iterable

from aiogram.types import (
    DisabledButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from .storage import Draft

AUTO = "auto"
HTML = "html"
MARKDOWN = "markdown"
MODES = (AUTO, HTML, MARKDOWN)

# --------------------------------------------------------------------- modes

_HTML_HINT = re.compile(
    r"</?(?:b|i|u|s|a|code|pre|blockquote|h[1-6]|strong|em|span|p|br|ul|ol|li"
    r"|table|thead|tbody|tr|td|th|details|summary|del|ins|tg-spoiler|tg-emoji)\b",
    re.IGNORECASE,
)


def resolve_mode(text: str, mode: str | None) -> str:
    """Turn the composer's ``auto`` mode into a concrete html/markdown."""
    if mode in (HTML, MARKDOWN):
        return mode
    return HTML if _HTML_HINT.search(text[:2000]) else MARKDOWN


def build_rich(text: str, mode: str | None = AUTO):
    """Build ``InputRichMessage`` from post source (html or markdown flavour)."""
    from aiogram.types import InputRichMessage

    resolved = resolve_mode(text, mode)
    if resolved == HTML:
        return InputRichMessage(html=text)
    return InputRichMessage(markdown=text)


def strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(text)


def escape(text: str) -> str:
    return _html.escape(text, quote=False)


# ---------------------------------------------------------------- keyboards

def url_keyboard(buttons: Iterable[dict[str, str]]) -> InlineKeyboardMarkup | None:
    """Classic URL inline keyboard for the *published* post (reply_markup)."""
    buttons = list(buttons)
    if not buttons:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for b in buttons:
        row.append(InlineKeyboardButton(text=b["label"][:64], url=b["url"]))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _toggle(label: str, on: bool, data: str, on_word: str = "on", off_word: str = "off") -> InlineKeyboardButton:
    state = on_word if on else off_word
    return InlineKeyboardButton(text=f"{label}: {state}", callback_data=data)


def draft_keyboard(draft: Draft) -> InlineKeyboardMarkup:
    """Composer control panel attached to the live preview message."""
    o = draft.options
    b = InlineKeyboardButton
    rows: list[list[InlineKeyboardButton]] = [
        [b(text="📤 Send now", callback_data="g:send"),
         b(text="📅 Schedule", callback_data="g:sch"),
         b(text="🧪 Stream", callback_data="g:stream")],
        [b(text=f"🔀 Mode: {draft.mode}", callback_data="g:mode"),
         _toggle("🔗 Link", bool(o.get("link_preview")), "g:lp"),
         _toggle("✨ Effect", bool(o.get("effect")), "g:fx")],
        [_toggle("🔒 Protect", bool(o.get("protect")), "g:prot"),
         _toggle("🔇 Silent", bool(o.get("silent")), "g:sil"),
         _toggle("💰 Paid", bool(o.get("paid")), "g:paid")],
        [b(text=f"🔘 Buttons ({len(draft.buttons)})", callback_data="g:btns"),
         b(text="✏️ Replace text", callback_data="g:rep"),
         b(text="👁 Re-preview", callback_data="g:pv")],
        [b(text="🗑 Discard", callback_data="g:del"),
         b(text="ℹ️ Snapshot", callback_data="g:st")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def demo_keyboard() -> InlineKeyboardMarkup:
    """Showcase keyboard: URL button + 10.3 disabled button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📘 Bot API docs", url="https://core.telegram.org/bots/api"
                ),
                InlineKeyboardButton(
                    text="🚫 Disabled (10.3)",
                    callback_data="g:noop",
                    disabled=DisabledButton(),
                ),
            ],
            [InlineKeyboardButton(text="👌 It works", callback_data="g:noop")],
        ]
    )


def welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📘 Help", callback_data="g:hlp"),
                InlineKeyboardButton(text="✨ Rich demo", callback_data="g:demo"),
            ],
            [InlineKeyboardButton(text="🆕 New post", callback_data="g:new")],
        ]
    )


# -------------------------------------------------------------------- cards

WELCOME_HTML = (
    "🍚 <b>GOHAN</b> — rich channel publisher\n"
    "Hi {name}! I turn plain text into <b>Bot API 10.x Rich Messages</b> "
    "and deliver them to your channels.\n\n"
    "<b>Stack</b>: aiogram {aiogram_ver} (Bot API 10.3) + Kurigram {kurigram_ver} "
    "(MTProto)\n\n"
    "<blockquote expandable><b>Rich Messages</b> give you headings, tables, "
    "collapsible details, expandable quotes, checklists, code blocks with "
    "language, math, media and <i>buttons inside the message</i> — all via "
    "<code>sendRichMessage</code>, streamed live with "
    "<code>sendRichMessageDraft</code>.</blockquote>\n\n"
    "Type your first post (or tap Help 👇)"
)

HELP_HTML = (
    "🍚 <b>GOHAN commands</b>\n\n"
    "<b>Composer</b>\n"
    "• <code>/new</code> — start a fresh draft, then type/paste your post\n"
    "• <code>/preview</code> — render the draft as a Rich Message\n"
    "• <code>/stream</code> — live-stream the draft ("
    "<code>sendRichMessageDraft</code> + stop button)\n"
    "• <code>/cancel</code> — discard the current draft\n"
    "• <code>/effect &lt;id&gt;</code> — set a message effect "
    "(<code>/effect off</code> disables)\n\n"
    "<b>Scheduling</b>\n"
    "• <code>/schedule 90</code> — publish in 90 minutes "
    "(or use the 📅 button)\n"
    "• <code>/queue</code> — list pending posts\n\n"
    "<b>Channels</b>\n"
    "• <code>/addchannel @channel</code> — register (bot must be admin "
    "with post rights)\n"
    "• <code>/channels</code> — registered channels\n"
    "• <code>/chinfo [@channel]</code> — chat details (Bot API + MTProto)\n"
    "• <code>/latest [@channel]</code> — last channel post "
    "(Kurigram/MTProto only)\n\n"
    "<b>Meta</b>\n"
    "• <code>/richdemo</code> — the 10.1–10.3 feature zoo\n"
    "• <code>/status</code> — engines, database, queue\n\n"
    "<blockquote expandable>Paste HTML or Markdown as your draft — the "
    "composer auto-detects it. The 🔀 button cycles auto → html → markdown "
    "and re-renders the preview in place via "
    "<code>editMessageText(rich_message=…)</code>.</blockquote>"
)

def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✨ Try the demo", callback_data="g:demo"),
             InlineKeyboardButton(text="🆕 New post", callback_data="g:new")],
            [InlineKeyboardButton(text="📡 Channels", callback_data="g:chref")],
        ]
    )


# ------------------------------------------------------- showcase document

def demo_document():
    """A /richdemo payload exercising every fan-new block type."""
    from aiogram.types import (
        DisabledButton,
        InputRichBlockDetails,
        InputRichBlockDivider,
        InputRichBlockExpandableBlockQuotation,
        InputRichBlockButtons,
        InputRichBlockList,
        InputRichBlockListItem,
        InputRichBlockMathematicalExpression,
        InputRichBlockParagraph,
        InputRichBlockPreformatted,
        InputRichBlockSectionHeading,
        InputRichBlockTable,
        InputRichBlockThinking,
        InputRichMessage,
        RichBlockTableCell,
        RichMessageButton,
        RichTextBold,
    )

    def cell(text: str, header: bool = False) -> RichBlockTableCell:
        return RichBlockTableCell(
            text=text, is_header=header, align="left", valign="middle"
        )

    table = InputRichBlockTable(
        is_striped=True,
        is_bordered=True,
        is_compact=True,
        caption="Bot API releases behind this bot",
        cells=[
            [cell("Release", True), cell("Date", True), cell("Highlights", True)],
            [cell("10.1"), cell("Jun 11, 2026"), cell("sendRichMessage, tables, math")],
            [cell("10.2"), cell("Jul 14, 2026"), cell("rich hardening")],
            [cell("10.3"), cell("Aug 24, 2026"), cell("rich buttons, disabled buttons, drafts")],
        ],
    )

    return InputRichMessage(
        blocks=[
            InputRichBlockSectionHeading(text="🍚 GOHAN rich demo", size=2),
            InputRichBlockParagraph(
                text=[
                    "Every block below is typed by ",
                    RichTextBold(text="aiogram 3.31"),
                    " and delivered by ",
                    RichTextBold(text="sendRichMessage"),
                    ".",
                ]
            ),
            InputRichBlockDivider(),
            table,
            InputRichBlockList(
                items=[
                    InputRichBlockListItem(
                        blocks=[InputRichBlockParagraph(text="Headings, dividers, tables")],
                        has_checkbox=True,
                        is_checked=True,
                    ),
                    InputRichBlockListItem(
                        blocks=[InputRichBlockParagraph(text="Checklists with live checkboxes")],
                        has_checkbox=True,
                        is_checked=True,
                    ),
                    InputRichBlockListItem(
                        blocks=[InputRichBlockParagraph(text="Streaming drafts + stop button")],
                        has_checkbox=True,
                        is_checked=False,
                    ),
                ]
            ),
            InputRichBlockDetails(
                summary=" collapsible details (click me)",
                is_open=False,
                blocks=[
                    InputRichBlockParagraph(
                        text="Hidden detail blocks, expandable quotations and "
                        "language-tagged code all ship in Bot API 10.1+."
                    ),
                    InputRichBlockPreformatted(
                        language="python",
                        text="await bot.send_rich_message(\n"
                        "    chat_id=chat_id,\n"
                        "    rich_message=InputRichMessage(html=post),\n"
                        ")",
                    ),
                ],
            ),
            InputRichBlockExpandableBlockQuotation(
                text="Expandable block quotation — collapsed by default, "
                "expands in place. Perfect for long footnotes.",
                credit="— Bot API 10.1",
            ),
            InputRichBlockMathematicalExpression(
                expression=r"\text{posts/day} = \frac{\text{ideas}}{\text{fear}}"
            ),
            InputRichBlockThinking(text="thinking about the next post…"),
            InputRichBlockButtons(
                buttons=[
                    RichMessageButton(
                        text="📘 core.telegram.org/bots/api",
                        url="https://core.telegram.org/bots/api",
                    ),
                    RichMessageButton(
                        text="🔘 callback (10.3)",
                        callback_data="g:noop",
                    ),
                    RichMessageButton(
                        text="🚫 disabled (10.3)",
                        disabled=DisabledButton(),
                    ),
                ]
            ),
        ]
    )
