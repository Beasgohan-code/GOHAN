"""AI commands: ``/ask``, ``/ai``, ``/summary``, ``/translate``.

The interesting part is :func:`cmd_ask` - the answer streams into a live draft
and then lands as a rich message. See :mod:`gohan.ai.chat` for the pipeline and
:mod:`gohan.rich.draft` for the Telegram side.
"""

from __future__ import annotations

import time

from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message, ReplyParameters

from ..ai.chat import AIConversation, ConversationStore, DEFAULT_PERSONALITY
from ..ai.llm import LLMError
from ..filters import IsAdmin, IsGroup, RateLimit
from ..logging_setup import get_logger
from ..rich import ui
from ..rich.sender import rich_reply, rich_send
from ..storage import Database

log = get_logger("handlers.ai")

router = Router(name="ai")

#: per-user cooldown: model calls cost money, so one ask every few seconds
ask_limit = RateLimit(4.0, message="give me a moment to finish 🙂")
translate_limit = RateLimit(4.0, message="one translation at a time 🙂")

__all__ = ["router"]


@router.message(Command(commands=["ask", "ai_ask"]), ask_limit)
async def cmd_ask(
    message: Message,
    command: CommandObject,
    bot: Bot,
    ai: AIConversation,
    db: Database,
) -> None:
    """``/ask <question>`` - a streamed, richly formatted answer."""
    question = (command.args or "").strip()
    if not question and message.reply_to_message is not None:
        question = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
    if not question:
        await rich_reply(
            message,
            ui.screen(
                "ask me something",
                icon="robot",
                blocks_=[
                    ui.panel(
                        ui.kv("ᴜꜱᴀɢᴇ", "<code>/ask why is the sky blue?</code>"),
                        ui.kv("ᴛɪᴘ", "ᴏʀ ʀᴇᴘʟʏ ᴛᴏ ᴀ ᴍᴇꜱꜱᴀɢᴇ ᴡɪᴛʜ /ᴀꜱᴋ"),
                    ),
                ],
            ),
        )
        return

    if len(question) > 2000:
        question = question[:2000]

    try:
        from ..ai.chat import build_buttons

        await ai.answer(
            message.chat.id,
            question,
            user=message.from_user,
            stream=True,
            reply_markup=None,
        )
    except LLMError:
        pass  # answer() already told the user
    except Exception as exc:
        log.exception("ask failed: %s", exc)
        await rich_reply(
            message,
            ui.panel(ui.no("I could not answer that. the owner has been told."), icon="warning"),
        )


@router.message(Command("ai"), IsGroup())
async def cmd_ai(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    store: ConversationStore,
    settings: Any,
    ai: AIConversation,
) -> None:
    """``/ai on|off|prompt <text>|reset`` - control the assistant in this chat."""
    if not await IsAdmin()(message, bot=bot, settings=settings):
        await rich_reply(message, ui.panel(ui.no("admins only"), icon="lock"))
        return

    args = (command.args or "").strip()
    action, _, rest = args.partition(" ")
    action = action.lower()

    if action in ("on", "off"):
        chat_settings = await db.get_chat_settings(message.chat.id)
        ai_settings = dict(chat_settings.get("ai") or {})
        ai_settings["enabled"] = action == "on"
        chat_settings["ai"] = ai_settings
        await db.update_chat_settings(message.chat.id, chat_settings)
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(
                ui.ok("assistant <b>on</b> - I will answer messages here")
                if action == "on"
                else ui.no("assistant <b>off</b>"),
                icon="robot",
            ),
        )
        return

    if action == "prompt":
        if not rest.strip():
            current = await store.personality(db, message.chat.id)
            await rich_send(
                bot,
                message.chat.id,
                ui.screen(
                    "personality",
                    icon="robot",
                    blocks_=[ui.panel(ui.esc(current)), ui.italic("set a new one: /ai prompt <text>")],
                ),
            )
            return
        await store.set_personality(db, message.chat.id, rest.strip())
        await rich_send(bot, message.chat.id, ui.panel(ui.ok("personality updated"), icon="check"))
        return

    if action in ("reset", "forget"):
        dropped = store.forget(message.chat.id)
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(ui.ok(f"forgot {dropped} remembered messages"), icon="broom"),
        )
        return

    chat_settings = await db.get_chat_settings(message.chat.id)
    ai_settings = chat_settings.get("ai") or {}
    enabled = bool(ai_settings.get("enabled"))
    prompt = str(ai_settings.get("prompt") or DEFAULT_PERSONALITY)
    await rich_send(
        bot,
        message.chat.id,
        ui.screen(
            "assistant",
            icon="robot",
            subtitle=message.chat.title or "",
            blocks_=[
                ui.kv_panel(
                    [
                        ("🤖 ꜱᴛᴀᴛᴇ", ui.badge(enabled, yes="on", no_="off")),
                        ("🧠 ᴍᴏᴅᴇʟ", ui.esc(getattr(ai.llm, "model", ai.llm.name))),
                        ("💭 ᴍᴇᴍᴏʀʏ", f"{len(store.recent(message.chat.id))} messages"),
                    ],
                    icon="robot",
                ),
                ui.details("personality", ui.esc(prompt), icon="memo"),
                ui.table(
                    ["ᴄᴏᴍᴍᴀɴᴅ", "ᴡʜᴀᴛ ɪᴛ ᴅᴏᴇꜱ"],
                    [
                        ("/ai on", "answer every message here"),
                        ("/ai off", "stop answering"),
                        ("/ai prompt <text>", "change my personality"),
                        ("/ai reset", "forget the conversation"),
                    ],
                ),
            ],
        )
        + "\n\n"
        + ui.action_bar(
            [
                ("Turn on", "ai:on"),
                ("Turn off", "ai:off"),
                ("Forget", "ai:reset"),
            ],
            per_row=3,
        ),
    )


async def toggle_chat_ai(db: Database, chat_id: int, value: bool | None = None) -> bool:
    """Flip (or set) the assistant for one chat. Returns the new state."""
    chat_settings = await db.get_chat_settings(chat_id)
    ai_settings = dict(chat_settings.get("ai") or {})
    enabled = (not bool(ai_settings.get("enabled"))) if value is None else bool(value)
    ai_settings["enabled"] = enabled
    chat_settings["ai"] = ai_settings
    await db.update_chat_settings(chat_id, chat_settings)
    return enabled


@router.callback_query(F.data.startswith("ai:"))
async def on_ai_callback(callback: CallbackQuery, bot: Bot, db: Database, store: ConversationStore) -> None:
    """Buttons from ``/ai`` (and from an answer)."""
    action = (callback.data or "").split(":", 1)[-1]
    if callback.message is None:
        await callback.answer()
        return
    chat_id = callback.message.chat.id

    if action in ("on", "off"):
        chat_settings = await db.get_chat_settings(chat_id)
        ai_settings = dict(chat_settings.get("ai") or {})
        ai_settings["enabled"] = action == "on"
        chat_settings["ai"] = ai_settings
        await db.update_chat_settings(chat_id, chat_settings)
        await callback.answer(f"assistant {action}")
        return

    if action == "reset":
        dropped = store.forget(chat_id)
        await callback.answer(f"forgot {dropped} messages")
        return

    if action == "forget":
        dropped = store.forget(chat_id)
        await callback.answer(f"forgot {dropped} messages")
        return

    if action == "again":
        await callback.answer("use /ask to ask again 🙂", show_alert=False)
        return

    await callback.answer()


@router.message(Command("summary"), IsGroup())
async def cmd_summary(
    message: Message,
    command: CommandObject,
    bot: Bot,
    ai: AIConversation,
    db: Database,
) -> None:
    """``/summary [n]`` - summarise the last *n* messages of this chat.

    With MTProto enabled the history comes from Telegram; otherwise the bot can
    only use what it has seen, which is stated plainly in the result.
    """
    from ..ai.chat import markdown_to_rich

    count = 50
    if command.args and command.args.strip().isdigit():
        count = min(max(int(command.args.strip()), 10), 200)

    transcript = ""
    mtproto = getattr(bot, "_gohan_mtproto", None)
    if mtproto is not None and getattr(mtproto, "status", None) and mtproto.status.has_history:
        try:
            lines = []
            async for msg in mtproto.client.get_chat_history(message.chat.id, limit=count):
                author = getattr(getattr(msg, "from_user", None), "first_name", "?")
                text = (getattr(msg, "text", "") or getattr(msg, "caption", "") or "").strip()
                if text:
                    lines.append(f"{author}: {text}")
            transcript = "\n".join(reversed(lines))
        except Exception as exc:
            log.debug("history fetch failed: %s", exc)

    if not transcript:
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(
                ui.no("I cannot read this chat's history")
                + "\n\n"
                + ui.italic(
                    "a user session gives me history - set up MTProto and run "
                    "<code>python -m gohan --login</code>"
                ),
                icon="satellite",
            ),
            reply_parameters=ReplyParameters(message_id=message.message_id),
        )
        return

    thinking = await rich_send(
        bot, message.chat.id, ui.panel(ui.italic("reading the last messages…"), icon="robot")
    )
    try:
        raw = await ai.summarise(message.chat.id, transcript)
        html = markdown_to_rich(raw)
        from ..rich.sender import rich_edit

        if thinking is not None:
            await rich_edit(
                bot, chat_id=message.chat.id, message_id=thinking.message_id, html=html
            )
        else:
            await rich_send(bot, message.chat.id, html)
    except LLMError as exc:
        await rich_send(bot, message.chat.id, ui.panel(ui.no(ui.esc(str(exc))), icon="warning"))


@router.message(Command("translate"), translate_limit)
async def cmd_translate(
    message: Message,
    command: CommandObject,
    bot: Bot,
    ai: AIConversation,
) -> None:
    """``/translate <language> <text>`` (or reply with ``/translate <language>``)."""
    from ..ai.chat import markdown_to_rich
    from ..ai.llm import ChatMessage

    args = (command.args or "").strip()
    language, _, text = args.partition(" ")
    if not text and message.reply_to_message is not None:
        text = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()
    if not language or not text:
        await rich_reply(
            message, ui.panel("usage: <code>/translate malayalam hello there</code>", icon="globe")
        )
        return

    prompt = [
        ChatMessage(
            "system",
            "You are a translator. Reply with the translation only, nothing else. "
            "Keep formatting and tone. If the text is already in the target language, say so briefly.",
        ),
        ChatMessage("user", f"Translate to {language}:\n\n{text[:3000]}"),
    ]
    try:
        raw = await ai.llm.complete(prompt, max_tokens=800)
    except LLMError as exc:
        await rich_reply(message, ui.panel(ui.no(ui.esc(str(exc))), icon="warning"))
        return
    await rich_reply(
        message,
        ui.screen(
            "translation",
            icon="globe",
            subtitle=f"→ {ui.esc(language)}",
            blocks_=[ui.panel(markdown_to_rich(raw)), ui.footer_text("machine translation", caps_text=False)],
        ),
    )


class WantsAnswer(BaseFilter):
    """Match only when this chat has the assistant on *and* the message wants it.

    This has to be a filter rather than a check inside the handler: in aiogram a
    matching handler consumes the update, so an "always match, sometimes answer"
    handler silently starves every router that comes after it (the guardian and
    the filter engine both live there).
    """

    #: the bot's own username, cached - asking Telegram on every message is rude
    _username: str | None = None
    _fetched_at: float = 0.0

    async def _me(self, bot: Bot | None) -> str | None:
        if bot is None:
            return None
        if self._username is None or time.monotonic() - self._fetched_at > 600:
            try:
                me = await bot.get_me()
                type(self)._username = me.username or ""
                type(self)._fetched_at = time.monotonic()
            except Exception:
                return None
        return type(self)._username

    async def __call__(
        self, message: Message, db: Database | None = None, bot: Bot | None = None, **_: Any
    ) -> bool:
        if db is None or message.from_user is None or message.from_user.is_bot:
            return False
        chat_settings = await db.get_chat_settings(message.chat.id)
        if not (chat_settings.get("ai") or {}).get("enabled"):
            return False

        text = (message.text or "").strip()
        if len(text) < 3:
            return False

        username = await self._me(bot)
        if username and f"@{username}".lower() in text.lower():
            return True
        replied = message.reply_to_message
        if replied is not None and replied.from_user is not None and bot is not None:
            try:
                me = await bot.get_me()
                if replied.from_user.id == me.id:
                    return True
            except Exception:
                pass
        return text.endswith("?") and len(text) > 12


@router.message(IsGroup(), F.text, WantsAnswer())
async def ai_auto_reply(
    message: Message,
    bot: Bot,
    store: ConversationStore,
    ai: AIConversation,
) -> None:
    """Answer a message when ``/ai on`` is set for this chat.

    ``WantsAnswer`` guarantees this only runs on a mention, a reply to the bot or
    a question mark - and, just as importantly, that it only *consumes* those.
    """
    if message.from_user is None:
        return
    username = WantsAnswer._username or ""
    question = (message.text or "").strip()
    if username:
        question = question.replace(f"@{username}", "").strip()
    try:
        await ai.answer(message.chat.id, question, user=message.from_user, stream=False)
    except LLMError:
        pass
    except Exception as exc:
        log.debug("auto reply failed: %s", exc)
