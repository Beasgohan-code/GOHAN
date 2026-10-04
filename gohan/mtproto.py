"""Kurigram (MTProto) engine — optional power-ups next to the Bot API.

Kurigram installs as a drop-in ``pyrogram`` namespace:

    from pyrogram import Client   # <- this IS Kurigram
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Config, cfg as default_cfg

log = logging.getLogger("gohan.mtp")


class MtpEngine:
    """Thin wrapper around a Kurigram client (bot identity or user session)."""

    def __init__(self, config: Config | None = None) -> None:
        self.cfg = config or default_cfg
        self.client: Any = None
        self.disabled_reason: str = "not started"
        self.who: str | None = None

    @property
    def enabled(self) -> bool:
        return self.client is not None

    # ----------------------------------------------------------- lifecycle
    async def start(self) -> bool:
        if not self.cfg.mtproto_configured:
            self.disabled_reason = (
                "API_ID / API_HASH not set — optional, Bot API side works without them"
            )
            return False
        try:
            from pyrogram import Client
        except Exception as exc:  # pragma: no cover
            self.disabled_reason = f"Kurigram import failed: {exc}"
            return False

        if self.cfg.session_string:
            auth: dict[str, Any] = {"session_string": self.cfg.session_string}
        elif self.cfg.bot_token:
            auth = {"bot_token": self.cfg.bot_token}
        else:
            self.disabled_reason = "set SESSION_STRING (user account) or BOT_TOKEN"
            return False

        try:
            client = Client(
                "gohan",
                api_id=self.cfg.api_id,
                api_hash=self.cfg.api_hash,
                in_memory=True,
                app_version="GOHAN/1.0",
                device_model="GOHAN publisher",
                **auth,
            )
            await client.start()
            me = await client.get_me()
        except Exception as exc:
            self.disabled_reason = f"MTProto start failed: {exc}"[:180]
            log.warning("Kurigram not started: %s", self.disabled_reason)
            return False

        self.client = client
        self.who = f"{'bot' if me.is_bot else 'user'} {me.first_name} (id {me.id})"
        self.disabled_reason = ""
        log.info("Kurigram engine ready as %s", self.who)
        return True

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.stop()
            except Exception:  # pragma: no cover
                log.exception("Kurigram stop failed")
            self.client = None

    # ------------------------------------------------------------- lookups
    async def chat_info(self, chat_id: int | str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        chat = await self.client.get_chat(chat_id)
        chat_type = getattr(chat.type, "value", str(chat.type))
        info: dict[str, Any] = {
            "id": chat.id,
            "type": chat_type,
            "title": getattr(chat, "title", None)
            or " ".join(
                filter(
                    None,
                    [getattr(chat, "first_name", None), getattr(chat, "last_name", None)],
                )
            ),
            "username": getattr(chat, "username", None),
            "members": None,
        }
        try:
            info["members"] = await self.client.get_chat_members_count(chat.id)
        except Exception:
            log.debug("get_chat_members_count unavailable for %s", chat_id)
        return info

    async def latest_post(self, chat_id: int | str) -> Any | None:
        """Newest message of a chat — Bot API cannot read history, MTProto can."""
        if not self.enabled:
            return None
        history = self.client.get_chat_history(chat_id, limit=1)
        async for message in history:
            return message
        return None

    # ------------------------------------------------------------- fallback
    async def send_rich(
        self,
        chat_id: int | str,
        mode: str,
        text: str,
        options: dict[str, Any],
        buttons: list[dict[str, str]],
    ) -> int | None:
        """Last-resort publish via Kurigram's send_rich_message."""
        if not self.enabled:
            return None
        from pyrogram import types as pt

        irm = (
            pt.InputRichMessage(html=text)
            if mode == "html"
            else pt.InputRichMessage(markdown=text)
        )
        reply_markup = None
        if buttons:
            rows = [
                [
                    pt.InlineKeyboardButton(text=b["label"][:64], url=b["url"])
                    for b in buttons[i : i + 2]
                ]
                for i in range(0, len(buttons), 2)
            ]
            reply_markup = pt.InlineKeyboardMarkup(inline_keyboard=rows)

        message = await self.client.send_rich_message(
            chat_id,
            irm,
            disable_notification=bool(options.get("silent")) or None,
            protect_content=bool(options.get("protect")) or None,
            reply_markup=reply_markup,
        )
        return getattr(message, "id", None)
