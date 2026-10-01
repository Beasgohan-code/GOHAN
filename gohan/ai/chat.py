"""Conversations: memory, per-chat personalities, and the streaming flow.

:class:`ConversationStore` keeps the last few exchanges per chat in memory (and
the personality/system prompt in the database, because that is worth persisting).
:class:`AIConversation` ties an :class:`~gohan.ai.llm.LLMClient` to a
:class:`~gohan.rich.draft.RichDraft`, so an answer visibly types itself out and
then becomes a permanent rich message.

The pipeline for one answer:

1. build messages = system prompt + recent history + the question,
2. open a draft (Telegram shows "thinking…"),
3. stream tokens into the draft, throttled,
4. markdown → rich HTML conversion happens *incrementally*, so the live draft is
   already formatted while it grows,
5. finish with ``sendRichMessage`` (persist) and optionally buttons.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from ..logging_setup import get_logger
from ..rich import ui
from ..rich.draft import RichDraft
from ..rich.sender import rich_send
from ..storage import Database
from .llm import ChatMessage, LLMClient, LLMError

log = get_logger("ai.chat")

__all__ = ["AIConversation", "ConversationStore", "markdown_to_rich"]

DEFAULT_PERSONALITY = (
    "You are GOHAN, a helpful Telegram assistant inside a group chat. "
    "Be concise and friendly. Answer in the user's language. "
    "Use short paragraphs; when a list or a comparison genuinely helps, use one."
)


@dataclass
class ConversationStore:
    """Recent exchanges per chat, plus the personality per chat."""

    max_turns: int = 8
    history: dict[int, deque[ChatMessage]] = field(default_factory=lambda: defaultdict(deque))

    def remember(self, chat_id: int, role: str, content: str) -> None:
        bucket = self.history[chat_id]
        bucket.append(ChatMessage(role=role, content=content))
        while len(bucket) > self.max_turns * 2:
            bucket.popleft()

    def recent(self, chat_id: int) -> list[ChatMessage]:
        return list(self.history[chat_id])

    def forget(self, chat_id: int) -> int:
        return len(self.history.pop(chat_id, ()))

    def trim(self, keep_chats: int = 500) -> int:
        """Drop the least recently used chats (called by the watchdog)."""
        if len(self.history) <= keep_chats:
            return 0
        dropped = 0
        for chat_id in list(self.history)[: len(self.history) - keep_chats]:
            self.history.pop(chat_id, None)
            dropped += 1
        return dropped

    async def personality(self, db: Database, chat_id: int) -> str:
        settings = await db.get_chat_settings(chat_id)
        ai = settings.get("ai") or {}
        return str(ai.get("prompt") or DEFAULT_PERSONALITY)

    async def set_personality(self, db: Database, chat_id: int, prompt: str | None) -> None:
        settings = await db.get_chat_settings(chat_id)
        ai = dict(settings.get("ai") or {})
        if prompt:
            ai["prompt"] = prompt[:1500]
        else:
            ai.pop("prompt", None)
        settings["ai"] = ai
        await db.update_chat_settings(chat_id, settings)


# ---------------------------------------------------------------------------
#  markdown -> rich HTML
# ---------------------------------------------------------------------------
# LLMs emit Markdown; Telegram rich messages prefer HTML. This converts the
# common subset and *escapes everything else*, so model output can never inject
# markup. It is deliberately small: fenced code, headings, bold/italic/code,
# inline links, blockquotes, and pipe tables.

_FENCE_RE = re.compile(r"```(\w+)?\n(.*?)```", re.S)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__")
_ITALIC_RE = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)|(?<![_\w])_([^_\n]+)_(?!_)")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")


def markdown_to_rich(text: str) -> str:
    """Convert a markdown answer into rich-message HTML (safe by construction)."""
    if not text:
        return ""

    placeholders: list[str] = []

    def stash(html: str) -> str:
        placeholders.append(html)
        return f"\x00{len(placeholders) - 1}\x00"

    # 1. fenced code blocks first - their contents must not be touched again
    def fence(match: re.Match[str]) -> str:
        language = match.group(1) or None
        body = match.group(2).rstrip("\n")
        return stash(ui.pre(body, language))

    text = _FENCE_RE.sub(fence, text)

    # 2. escape what is left
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 3. inline code, then links, then emphasis
    text = _INLINE_CODE_RE.sub(lambda m: stash(f"<code>{m.group(1)}</code>"), text)
    text = _LINK_RE.sub(lambda m: stash(f'<a href="{m.group(2)}">{m.group(1)}</a>'), text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)

    # 4. block structure: headings, tables, lists, quotes, paragraphs
    lines = text.split("\n")
    out: list[str] = []
    bullets: list[str] = []
    numbers: list[str] = []
    table_rows: list[list[str]] = []

    def flush_bullets() -> None:
        if bullets:
            out.append(ui.list_unordered(bullets))
            bullets.clear()

    def flush_numbers() -> None:
        if numbers:
            out.append(ui.list_ordered(numbers))
            numbers.clear()

    def flush_table() -> None:
        if not table_rows:
            return
        header = None
        body = table_rows
        if len(table_rows) >= 2 and all(set(c) <= set("-: ") for c in table_rows[1]):
            header, body = table_rows[0], table_rows[2:]
        elif table_rows:
            header, body = table_rows[0], table_rows[1:]
        out.append(
            ui.table(
                [cell.strip() for cell in header] if header else None,
                [[cell.strip() for cell in row] for row in body],
                caps_headers=False,
            )
        )
        table_rows.clear()

    for raw_line in lines:
        line = raw_line.rstrip()

        if _TABLE_ROW_RE.match(line):
            flush_bullets()
            flush_numbers()
            cells = [c for c in _TABLE_ROW_RE.match(line).group(1).split("|")]  # type: ignore[union-attr]
            table_rows.append(cells)
            continue
        flush_table()

        if not line.strip():
            flush_bullets()
            flush_numbers()
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush_bullets()
            flush_numbers()
            out.append(ui.h(heading.group(2), level=len(heading.group(1)), caps_text=False))
            continue

        if line.strip() in ("---", "***", "___"):
            flush_bullets()
            flush_numbers()
            out.append(ui.divider())
            continue

        # the text is already escaped, so a blockquote marker is "&gt;"
        quote = re.match(r"^\s*&gt;\s?(.*)$", line)
        if quote:
            flush_bullets()
            flush_numbers()
            out.append(ui.quote(quote.group(1)))
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            flush_numbers()
            bullets.append(bullet.group(1))
            continue

        ordered = _ORDERED_RE.match(line)
        if ordered:
            flush_bullets()
            numbers.append(ordered.group(1))
            continue

        flush_bullets()
        flush_numbers()
        # A line holding nothing but a placeholder (a fenced code block, say) is
        # already a block element - wrapping it in <p> would be invalid.
        if re.fullmatch(r"\x00\d+\x00", line.strip()):
            out.append(line.strip())
        else:
            out.append(f"<p>{line}</p>")

    flush_bullets()
    flush_numbers()
    flush_table()

    result = "\n".join(part for part in out if part)

    # 5. restore the stashed code/links
    for index, html in enumerate(placeholders):
        result = result.replace(f"\x00{index}\x00", html)
    return result


# ---------------------------------------------------------------------------
#  the streaming conversation
# ---------------------------------------------------------------------------


@dataclass
class AIConversation:
    """One question → streamed rich answer."""

    bot: Bot
    llm: LLMClient
    store: ConversationStore
    db: Database
    settings: Any

    async def answer(
        self,
        chat_id: int,
        question: str,
        *,
        user: Any = None,
        stream: bool = True,
        extra_system: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        max_tokens: int = 900,
    ) -> str:
        """Ask the model and deliver the answer. Returns the raw model text.

        Streaming is only attempted in private chats (Telegram's restriction on
        drafts); groups get the answer as a single rich message, which is what
        they want anyway.
        """
        personality = extra_system or await self.store.personality(self.db, chat_id)
        messages: list[ChatMessage] = [ChatMessage("system", personality)]
        messages.extend(self.store.recent(chat_id))
        if user is not None:
            display = getattr(user, "full_name", None) or getattr(user, "first_name", "user")
            messages.append(ChatMessage("user", f"[{display}]: {question}"))
        else:
            messages.append(ChatMessage("user", question))

        collected: list[str] = []
        is_private = chat_id > 0
        draft: RichDraft | None = None
        if stream and is_private:
            draft = RichDraft(
                self.bot,
                chat_id,
                interval=self.settings.draft_interval,
                can_stop=True,
                keep_on_stop=True,
            )

        try:
            if draft is not None:
                await draft.__aenter__()
                async for chunk in self.llm.stream(messages, max_tokens=max_tokens):
                    collected.append(chunk)
                    if draft.stopped:
                        break
                    # Only re-render the tail while streaming: converting the whole
                    # buffer on every token would be O(n²).
                    rendered = markdown_to_rich("".join(collected))
                    await draft.update(rendered)
            else:
                if stream:
                    from aiogram.enums import ChatAction
                    from aiogram.utils.chat_action import ChatActionSender

                    async with ChatActionSender(
                        bot=self.bot, chat_id=chat_id, action=ChatAction.TYPING
                    ):
                        async for chunk in self.llm.stream(messages, max_tokens=max_tokens):
                            collected.append(chunk)
                else:
                    async for chunk in self.llm.stream(messages, max_tokens=max_tokens):
                        collected.append(chunk)
        except LLMError as exc:
            log.warning("LLM failed: %s", exc)
            message = ui.panel(
                ui.no(f"the model is unavailable right now\n<i>{ui.esc(str(exc))[:200]}</i>"),
                icon="warning",
            )
            if draft is not None:
                await draft.finish(message, validate=False)
            else:
                await rich_send(self.bot, chat_id, message)
            raise

        raw = "".join(collected)
        html = markdown_to_rich(raw)
        if reply_markup is not None:
            body = html + self._footer(raw)
        else:
            body = html + self._footer(raw)

        if draft is not None:
            await draft.finish(body, reply_markup=reply_markup)
        else:
            await rich_send(self.bot, chat_id, body, reply_markup=reply_markup)

        self.store.remember(chat_id, "user", question)
        self.store.remember(chat_id, "assistant", raw[:2000])
        try:
            await self.db.log_event("AI", chat_id=chat_id, user_id=getattr(user, "id", None),
                                    data={"chars": len(raw), "request": question[:120]})
        except Exception:
            pass
        return raw

    def _footer(self, raw: str) -> str:
        """Token/character note plus the model name."""
        model = getattr(self.llm, "model", self.llm.name)
        return "\n\n" + ui.footer_text(f"{len(raw)} chars · {model}", caps_text=False)

    async def summarise(self, chat_id: int, transcript: str, *, limit: int = 1200) -> str:
        """Summarise a block of chat text (used by ``/summary``)."""
        messages = [
            ChatMessage(
                "system",
                "Summarise the Telegram conversation the user provides. "
                "Output a short heading, then 3-6 bullet points, then a one-line conclusion. "
                "Be neutral and do not invent facts.",
            ),
            ChatMessage("user", transcript[:6000]),
        ]
        return await self.llm.complete(messages, max_tokens=limit)


def build_buttons(question: str) -> InlineKeyboardMarkup | None:
    """A small keyboard offered under an answer (coloured, per house style)."""
    from ..rich.sender import keyboard

    return keyboard(
        [
            [
                {"text": "🔄 Ask again", "callback_data": "ai:again", "style": "primary"},
                {"text": "🧹 Forget", "callback_data": "ai:forget", "style": "danger"},
            ]
        ]
    )


__all__ += ["DEFAULT_PERSONALITY", "build_buttons"]
