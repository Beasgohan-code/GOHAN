"""A thin, failure-tolerant wrapper around a Kurigram client.

Principles:

* **Never break the bot.** Every method catches its exceptions and returns a
  degraded value; a missing session or a revoked key must not stop moderation.
* **Start lazily and in the background.** Login can take seconds and may need a
  human (SMS code), so it never blocks startup.
* **Say what mode it is in.** :class:`MTProtoStatus` is exposed on the status page
  and in ``/status``, so "why is download limited to 20 MB?" always has an answer.

Modes (``MTPROTO_MODE``):

``user``  a user session - full history, everything
``bot``   a *bot* session (bot token) - still 2 GB transfers, no message history
``auto``  use a saved user session if there is one, else a bot session, else off
``off``   disabled
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..config import Settings
from ..logging_setup import get_logger

log = get_logger("mtproto")

__all__ = ["CannotLogin", "MTProtoBridge", "MTProtoStatus", "build_bridge"]


class LoginMode(str, Enum):
    USER = "user"
    BOT = "bot"
    OFF = "off"
    PENDING = "pending"


@dataclass
class MTProtoStatus:
    """Everything the rest of the bot needs to know about the MTProto side."""

    mode: LoginMode = LoginMode.OFF
    connected: bool = False
    username: str | None = None
    user_id: int | None = None
    reason: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.connected and self.mode in (LoginMode.USER, LoginMode.BOT)

    @property
    def has_history(self) -> bool:
        """Message history (and therefore cross-chat reputation) needs a user session."""
        return self.available and self.mode is LoginMode.USER

    @property
    def max_transfer_mb(self) -> int:
        return 2000 if self.available else 20

    def describe(self) -> str:
        if not self.available:
            return f"off ({self.reason})" if self.reason else "off"
        who = f"@{self.username}" if self.username else str(self.user_id or "?")
        return f"{self.mode.value} · {who} · up to {self.max_transfer_mb} MB"


class CannotLogin(RuntimeError):
    """Raised by :meth:`MTProtoBridge.login` when the client cannot start."""


class MTProtoBridge:
    """Owns the Kurigram client and exposes the handful of operations we need."""

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        self.status = MTProtoStatus()
        self._client = client
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[Any] | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def client(self) -> Any:
        return self._client

    @property
    def available(self) -> bool:
        return self.status.available

    def _resolve_mode(self) -> LoginMode:
        mode = self.settings.mtproto_mode
        if mode == "off" or not self.settings.has_api_credentials:
            return LoginMode.OFF
        if mode == "user":
            return LoginMode.USER
        if mode == "bot":
            return LoginMode.BOT
        # auto: prefer an existing user session, else the bot session
        if self.settings.user_session_file.exists():
            return LoginMode.USER
        if self.settings.bot_token is not None:
            return LoginMode.BOT
        return LoginMode.OFF

    def build_client(self, mode: LoginMode | None = None) -> Any:
        """Create a Kurigram client for ``mode`` (does not connect)."""
        from pyrogram import Client

        mode = mode or self._resolve_mode()
        if mode is LoginMode.OFF:
            raise CannotLogin("MTProto is disabled or API_ID/API_HASH are missing")
        if not self.settings.has_api_credentials:
            raise CannotLogin("API_ID/API_HASH are not configured")

        common: dict[str, Any] = {
            "api_id": self.settings.api_id,
            "api_hash": self.settings.api_hash.get_secret_value(),  # type: ignore[union-attr]
            "app_version": "GOHAN 0.1.0",
            "device_model": "GOHAN",
        }
        if mode is LoginMode.USER:
            session = str(self.settings.user_session_file.with_suffix(""))
            common["phone_number"] = self.settings.phone
        else:
            token = self.settings.bot_token
            if token is None:
                raise CannotLogin("BOT_TOKEN is required for a bot-mode MTProto session")
            session = str(self.settings.bot_session_file.with_suffix(""))
            common["bot_token"] = token.get_secret_value()
        return Client(session, **common)

    async def login(self, *, interactive: bool = False) -> MTProtoStatus:
        """Connect, choosing the best available mode. Never raises for auto/off.

        A **user** session that does not exist yet cannot be created without a
        human (SMS code), so unless ``interactive`` is set the bridge falls back
        to the bot session instead of hanging on an input prompt.
        """
        async with self._lock:
            mode = self._resolve_mode()
            if mode is LoginMode.OFF:
                self.status = MTProtoStatus(
                    mode=LoginMode.OFF,
                    reason="disabled"
                    if self.settings.mtproto_mode == "off"
                    else "no API_ID/API_HASH",
                )
                log.info("MTProto disabled (%s)", self.status.reason)
                return self.status

            if mode is LoginMode.USER and not interactive and not self.settings.user_session_file.exists():
                if self.settings.bot_token is not None:
                    log.info(
                        "no user session yet - using the bot session "
                        "(run `python -m gohan --login` for full history features)"
                    )
                    mode = LoginMode.BOT
                else:
                    self.status = MTProtoStatus(
                        mode=LoginMode.PENDING,
                        reason="no user session - run `python -m gohan --login`",
                    )
                    return self.status

            self.settings.ensure_dirs()
            try:
                client = self.build_client(mode)
            except CannotLogin as exc:
                self.status = MTProtoStatus(mode=LoginMode.OFF, reason=str(exc))
                log.warning("MTProto unavailable: %s", exc)
                return self.status

            try:
                await client.start()
                me = await client.get_me()
            except Exception as exc:
                message = str(exc)[:200]
                self.status = MTProtoStatus(mode=LoginMode.PENDING, reason=message)
                self.status.errors.append(message)
                log.warning("MTProto login failed: %s", message)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return self.status

            self._client = client
            self.status = MTProtoStatus(
                mode=mode,
                connected=True,
                username=getattr(me, "username", None),
                user_id=getattr(me, "id", None),
                reason="",
            )
            log.info("MTProto ready: %s", self.status.describe())
            return self.status

    def start_background(self) -> None:
        """Login in the background so startup is never blocked by the network."""
        if not self.settings.mtproto_configured:
            self.status = MTProtoStatus(
                mode=LoginMode.OFF,
                reason="disabled"
                if self.settings.mtproto_mode == "off"
                else "no API_ID/API_HASH",
            )
            return
        self._task = asyncio.get_running_loop().create_task(self.login())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._client is not None:
            try:
                await self._client.stop()
            except Exception:
                pass

    # -- capabilities --------------------------------------------------------

    async def check_connection(self) -> bool:
        """Used by the watchdog."""
        if self._client is None:
            return not self.settings.mtproto_configured
        try:
            await self._client.get_me()
            self.status.connected = True
            return True
        except Exception as exc:
            self.status.connected = False
            log.debug("MTProto ping failed: %s", exc)
            return False

    async def profile(self, user_id: int) -> dict[str, Any]:
        """Basic profile - works in bot mode too."""
        if not self.available:
            return {}
        try:
            user = await self._client.get_users(user_id)
            return {
                "id": getattr(user, "id", user_id),
                "username": getattr(user, "username", None),
                "first_name": getattr(user, "first_name", None),
                "last_name": getattr(user, "last_name", None),
                "is_bot": bool(getattr(user, "is_bot", False)),
                "is_premium": bool(getattr(user, "is_premium", False)),
                "is_scam": bool(getattr(user, "is_scam", False)),
                "is_fake": bool(getattr(user, "is_fake", False)),
                "status": str(getattr(user, "status", "")),
            }
        except Exception as exc:
            log.debug("get_users failed: %s", exc)
            return {}

    async def reputation(self, user_id: int, *, limit: int = 40) -> dict[str, Any]:
        """Look for the user's messages across *any* common chat.

        This is the guardian's best weapon against a returning spammer: it works
        even in chats the bot is not in, as long as the session account can see
        them. Only available with a user session.
        """
        if not self.status.has_history:
            return {"supported": False, "reason": "needs a user session"}
        try:
            dialogs = await self._client.get_dialogs(limit=40)
        except Exception as exc:
            return {"supported": False, "reason": str(exc)[:120]}

        found: list[dict[str, Any]] = []
        chat_count = 0
        for dialog in dialogs:
            chat = getattr(dialog, "chat", None)
            if chat is None or getattr(chat, "type", None) not in ("group", "supergroup"):
                continue
            chat_count += 1
            try:
                async for message in self._client.get_chat_history(chat.id, limit=limit):
                    sender = getattr(message, "from_user", None)
                    if sender is not None and getattr(sender, "id", None) == user_id:
                        text = (getattr(message, "text", "") or getattr(message, "caption", "") or "")[:120]
                        found.append(
                            {
                                "chat": getattr(chat, "title", str(chat.id)),
                                "chat_id": chat.id,
                                "date": str(getattr(message, "date", "")),
                                "text": text,
                            }
                        )
                        break  # one hit per chat is enough
            except Exception:
                continue
            if len(found) >= 8:
                break
        return {
            "supported": True,
            "chats_scanned": chat_count,
            "hits": found,
            "verdict": self._reputation_verdict(found),
        }

    @staticmethod
    def _reputation_verdict(hits: list[dict[str, Any]]) -> str:
        if not hits:
            return "no history found"
        if len(hits) >= 4:
            return "active in many chats - likely a real user"
        return "seen in a few chats"

    async def purge_user_messages(self, chat_id: int, user_id: int, *, limit: int = 1000) -> int:
        """Delete a user's messages from a chat - beyond the Bot API's 48 h window."""
        if not self.available:
            return 0
        deleted = 0
        try:
            async for message in self._client.get_chat_history(chat_id, limit=limit):
                sender = getattr(message, "from_user", None)
                if sender is not None and getattr(sender, "id", None) == user_id:
                    try:
                        await message.delete()
                        deleted += 1
                    except Exception:
                        continue
        except Exception as exc:
            log.debug("purge failed: %s", exc)
        return deleted

    async def download(self, message_link: str | None = None, *, message: Any = None) -> Path | None:
        """Download media from a link or message, up to the 2 GB MTProto ceiling."""
        if not self.available:
            return None
        target = Path("data/downloads")
        target.mkdir(parents=True, exist_ok=True)
        try:
            media = message or await self._resolve_link(message_link)
            if media is None:
                return None
            path = await self._client.download_media(
                media,
                file_name=f"{target}/",
                progress=self._progress,
            )
            return Path(path) if path else None
        except Exception as exc:
            log.warning("download failed: %s", exc)
            return None

    async def _resolve_link(self, link: str | None) -> Any:
        if not link:
            return None
        try:
            return await self._client.get_messages(link)
        except Exception as exc:
            log.debug("cannot resolve %s: %s", link, exc)
            return None

    def _progress(self, current: int, total: int) -> None:  # pragma: no cover - logging only
        if total and current % (8 * 1024 * 1024) < 1024:
            log.debug("download %s%%", int(current / total * 100))

    async def dialog_stats(self) -> dict[str, Any]:
        """Summary used by ``/status``."""
        if not self.status.has_history:
            return {}
        try:
            dialogs = await self._client.get_dialogs(limit=200)
            groups = sum(
                1
                for d in dialogs
                if getattr(getattr(d, "chat", None), "type", None) in ("group", "supergroup")
            )
            return {"dialogs": len(dialogs), "groups": groups}
        except Exception:
            return {}


def build_bridge(settings: Settings) -> MTProtoBridge:
    return MTProtoBridge(settings)
