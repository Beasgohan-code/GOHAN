"""Live draft streaming with sendRichMessageDraft + stop button (10.1/10.3)."""

from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Bot

log = logging.getLogger("gohan.stream")

DRAFT_ID = 424242  # same id => Telegram animates draft changes
TASKS: dict[tuple[int, int], asyncio.Task] = {}


def _paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return parts or [text.strip() or "…"]


async def _run(bot: Bot, chat_id: int, text: str, mode: str) -> None:
    from .rich import build_rich, resolve_mode

    key = (chat_id, DRAFT_ID)
    resolved = resolve_mode(text, mode)
    try:
        # 0) thinking placeholder (rich block from Bot API 10.2)
        from aiogram.types import InputRichBlockThinking, InputRichMessage

        await bot.send_rich_message_draft(
            chat_id=chat_id,
            draft_id=DRAFT_ID,
            rich_message=InputRichMessage(
                blocks=[InputRichBlockThinking(text="composing rich post…")]
            ),
            can_stop=True,
            keep_on_stop=True,
        )
        await asyncio.sleep(0.9)

        # 1) progressively larger valid prefixes of the real document
        acc: list[str] = []
        for part in _paragraphs(text):
            acc.append(part)
            await bot.send_rich_message_draft(
                chat_id=chat_id,
                draft_id=DRAFT_ID,
                rich_message=build_rich("\n\n".join(acc), resolved),
                can_stop=True,
                keep_on_stop=True,
            )
            await asyncio.sleep(0.85)

        # 2) final message — sending it clears the draft server-side
        await bot.send_rich_message(
            chat_id=chat_id, rich_message=build_rich(text, resolved)
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("stream failed in chat %s", chat_id)
    finally:
        if TASKS.get(key) is asyncio.current_task():
            TASKS.pop(key, None)


def start_stream(bot: Bot, chat_id: int, text: str, mode: str) -> asyncio.Task:
    key = (chat_id, DRAFT_ID)
    old = TASKS.get(key)
    if old is not None and not old.done():
        old.cancel()
    task = asyncio.create_task(_run(bot, chat_id, text, mode))
    TASKS[key] = task
    return task


def stop_stream(chat_id: int, draft_id: int | None = None) -> bool:
    key = (chat_id, draft_id or DRAFT_ID)
    task = TASKS.pop(key, None)
    if task is not None and not task.done():
        task.cancel()
        return True
    return False
