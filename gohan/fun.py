"""Fun commands: hug, kiss, and sixteen more.

Each action sends an animation with a short caption, and falls back to a styled
rich card when no animation is available (offline, API down, or the owner has not
taught the bot a GIF yet).

Animations come from two places, in priority order:

1. ``/setgif <action>`` *(reply to an animation)* - the owner teaches the bot a
   media ``file_id``, stored per action in the database. This is how a group
   makes the bot "its own".
2. `nekos.best <https://nekos.best>`_ - a free, key-less API. The returned file
   is cached as a ``file_id`` after the first send, so repeat uses cost nothing.

Everything is one small :class:`AnimationProvider` so the whole feature can be
disabled with one config flag.
"""

from __future__ import annotations

import random
from typing import Any

import aiohttp
from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, ReplyParameters
from aiogram.utils.chat_action import ChatActionSender

from .config import Settings
from .content import FUN_ACTIONS
from .filters import IsGroup, is_admin
from .logging_setup import get_logger
from .rich import ui
from .rich.sender import rich_send
from .storage import Database

log = get_logger("fun")

router = Router(name="fun")

__all__ = ["AnimationProvider", "router"]

NEKOS_BASE = "https://nekos.best/api/v2"
_CACHE_PREFIX = "anim:"


class AnimationProvider:
    """Fetches (and remembers) one animation URL per action."""

    def __init__(self, db: Database, *, enabled: bool = True, timeout: float = 8.0) -> None:
        self.db = db
        self.enabled = enabled
        self.timeout = timeout
        self._memory: dict[str, str] = {}

    async def taught(self, action: str) -> str | None:
        """A media file_id the owner set with ``/setgif``."""
        return await self.db.get_kv(f"{_CACHE_PREFIX}taught:{action}")

    async def teach(self, action: str, file_id: str) -> None:
        await self.db.set_kv(f"{_CACHE_PREFIX}taught:{action}", file_id)

    async def fetch(self, action: str) -> str | None:
        """Return something sendable: a file_id (best) or an https URL."""
        if not self.enabled:
            return None

        if action in self._memory:
            return self._memory[action]

        cached = await self.db.get_kv(f"{_CACHE_PREFIX}{action}")
        if cached:
            self._memory[action] = cached
            return cached

        category = FUN_ACTIONS.get(action, {}).get("api")
        if not category:
            return None
        url = f"{NEKOS_BASE}/{category}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=self.timeout)
                ) as response:
                    if response.status != 200:
                        log.debug("nekos.best %s -> HTTP %s", category, response.status)
                        return None
                    payload = await response.json()
            results = payload.get("results") or []
            if not results:
                return None
            media = results[0].get("url")
            if media:
                self._memory[action] = media
                return media
        except Exception as exc:
            log.debug("animation fetch failed for %s: %s", action, exc)
        return None

    async def remember_file_id(self, action: str, file_id: str) -> None:
        """Called after a successful send so the next one is instant."""
        self._memory[action] = file_id
        try:
            await self.db.set_kv(f"{_CACHE_PREFIX}{action}", file_id)
        except Exception:
            pass


def _display(user: Any) -> str:
    name = ui.esc(" ".join(filter(None, (user.first_name, user.last_name))) or str(user.id))
    return ui.mention(f"<b>{name}</b>", user.id) if not user.is_bot else f"<b>{name}</b>"


def _target_of(message: Message, args: str | None) -> Any | None:
    """Who the action is aimed at: the reply, then a mention, then nobody."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    if message.entities:
        for entity in message.entities:
            if entity.type == "text_mention" and entity.user:
                return entity.user
            if entity.type == "mention":
                handle = message.text[entity.offset : entity.offset + entity.length].lstrip("@")
                # we only know the username here; the display name is filled later
                class _Ghost:
                    id = 0

                    def __init__(self, username: str) -> None:
                        self.username = username
                        self.first_name = f"@{username}"
                        self.last_name = None
                        self.is_bot = False

                return _Ghost(handle)
    return None


def _caption(action: str, sender: str, target: str | None, *, seed: int) -> str:
    from .content import action_caption

    text = action_caption(action, sender, target, seed=seed)
    return ui.panel(ui.italic(ui.esc(text)), icon=FUN_ACTIONS[action]["emoji"])  # type: ignore[arg-type]


async def _send_animation(
    bot: Bot,
    message: Message,
    action: str,
    caption: str,
    provider: AnimationProvider,
    *,
    reply_to: int | None,
) -> bool:
    """Try animation, then sticker-free rich card. Returns True when media sent."""
    media = await provider.fetch(action)
    if not media:
        return False
    try:
        sent = await bot.send_animation(
            message.chat.id,
            media,
            caption=caption,
            reply_parameters=ReplyParameters(message_id=reply_to) if reply_to else None,
        )
    except TelegramAPIError as exc:
        log.debug("send_animation failed (%s), falling back to a card", exc)
        return False
    file_id = getattr(getattr(sent, "animation", None), "file_id", None)
    if file_id:
        await provider.remember_file_id(action, file_id)
    return True


async def _run_action(
    action: str,
    message: Message,
    args: str | None,
    bot: Bot,
    provider: AnimationProvider,
) -> None:
    spec = FUN_ACTIONS[action]
    sender = _display(message.from_user)
    target_user = _target_of(message, args)
    target = _display(target_user) if target_user is not None else None
    seed = random.randrange(0, 997)

    caption = _caption(action, sender if target is None else sender, target, seed=seed)

    async with ChatActionSender(bot=bot, chat_id=message.chat.id, action=ChatAction.TYPING):
        sent = await _send_animation(
            bot,
            message,
            action,
            caption,
            provider,
            reply_to=message.message_id,
        )
    if sent:
        return

    # No animation available: a rich card instead, so the command still feels good.
    await rich_send(
        bot,
        message.chat.id,
        caption
        + "\n\n"
        + ui.actions(
            [
                ui.button("Again", callback=f"fun:{action}", style="primary", icon="refresh"),
                ui.button("All actions", callback="fun:list", style="link", icon="sparkles"),
            ]
        ),
        reply_parameters=ReplyParameters(message_id=message.message_id),
    )


def _make_handler(action: str):
    async def handler(
        message: Message, command: CommandObject, bot: Bot, provider: AnimationProvider
    ) -> None:
        await _run_action(action, message, command.args, bot, provider)

    handler.__name__ = f"cmd_{action}"
    handler.__doc__ = f"``/{action}`` - {FUN_ACTIONS[action]['verb']} someone."
    return handler


# register one command per action
for _action in FUN_ACTIONS:
    router.message.register(_make_handler(_action), Command(_action))


@router.message(Command(commands=["actions", "fun"]))
async def cmd_actions(message: Message, bot: Bot) -> None:
    """Every fun action, in one panel."""
    rows = []
    for name, spec in FUN_ACTIONS.items():
        rows.append((f"/{name}", ui.esc(str(spec["verb"]))))
    await rich_send(
        bot,
        message.chat.id,
        ui.screen(
            "fun",
            icon="sparkles",
            subtitle=f"{len(FUN_ACTIONS)} actions · reply to someone to aim them",
            blocks_=[
                ui.table(["ᴄᴏᴍᴍᴀɴᴅ", "ᴡʜᴀᴛ ɪᴛ ᴅᴏᴇꜱ"], rows),
                ui.panel(ui.italic("or tap a button below - it picks a random target line")),
            ],
        )
        + "\n\n"
        + ui.action_bar(
            [(f"{name}", f"fun:{name}") for name in list(FUN_ACTIONS)[:8]],
            per_row=4,
        ),
    )


@router.callback_query(F.data.startswith("fun:"))
async def on_fun_callback(callback: Any, bot: Bot, provider: AnimationProvider) -> None:
    """Re-roll a fun action from its button."""
    action = (callback.data or "").split(":", 1)[-1]
    if action == "list" or action not in FUN_ACTIONS:
        await callback.answer("use /actions for the full list")
        return
    if callback.message is None:
        await callback.answer()
        return
    sender = _display(callback.from_user)
    body = _caption(action, sender, None, seed=random.randrange(0, 997))
    try:
        await rich_send(
            bot,
            callback.message.chat.id,  # type: ignore[union-attr]
            body,
            reply_parameters=ReplyParameters(message_id=callback.message.message_id),  # type: ignore[union-attr]
        )
        await callback.answer()
    except Exception as exc:
        await callback.answer(f"could not send: {exc}", show_alert=True)


@router.message(Command("setgif"), IsGroup())
async def cmd_setgif(
    message: Message, command: CommandObject, bot: Bot, provider: AnimationProvider, settings: Any = None
) -> None:
    """``/setgif <action>`` *(as a reply to a GIF)* - teach the bot its animation."""
    if message.from_user is None:
        return
    if settings is not None and not await is_admin(bot, message.chat.id, message.from_user.id, settings):
        await rich_send(bot, message.chat.id, ui.panel(ui.no("admins only"), icon="lock"))
        return

    action = (command.args or "").strip().lower()
    if action not in FUN_ACTIONS:
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(
                ui.no(f"unknown action. try one of: {', '.join(list(FUN_ACTIONS)[:8])} …"),
                icon="question",
            ),
        )
        return

    replied = message.reply_to_message
    media = None
    if replied is not None:
        media = replied.animation or replied.video or replied.sticker or replied.document
    if media is None or not getattr(media, "file_id", None):
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(ui.no("reply to a GIF, video or sticker with /setgif"), icon="question"),
        )
        return

    await provider.teach(action, media.file_id)
    await rich_send(
        bot,
        message.chat.id,
        ui.panel(
            ui.ok(f"<b>/{action}</b> now uses this animation"),
            icon="check",
        ),
    )


@router.message(Command("gifoff"), IsGroup())
async def cmd_gifoff(message: Message, bot: Bot, provider: AnimationProvider, settings: Any = None) -> None:
    """Turn animations on or off for this bot instance."""
    if message.from_user is not None and settings is not None:
        if not await is_admin(bot, message.chat.id, message.from_user.id, settings):
            await rich_send(bot, message.chat.id, ui.panel(ui.no("owners only"), icon="lock"))
            return
    provider.enabled = not provider.enabled
    await rich_send(
        bot,
        message.chat.id,
        ui.panel(ui.ok(f"animations {'on' if provider.enabled else 'off'}"), icon="sparkles"),
    )
