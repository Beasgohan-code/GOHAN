"""New-member screening.

Two entry points:

* **Join requests** (``ChatJoinRequest``) - the bot decides *before* the person
  is in the group. Default: approve, because the alternative (pending forever)
  loses real members. During a raid: decline.
* **New members** (``new_chat_members``) - the classic captcha: the member is
  muted on arrival and gets a button; tapping it within
  ``captcha_timeout`` restores their permissions. Otherwise they are kicked.

The pending set is deliberately in-memory: a restart simply drops pending
challenges, and the watchdog re-mutes anyone still restricted (see
:meth:`CaptchaManager.sweep`).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions

from ..logging_setup import get_logger
from ..rich import ui

log = get_logger("guardian.captcha")

__all__ = ["CaptchaManager", "PendingChallenge", "MUTED"]

#: Permissions for a member awaiting captcha: read everything, send nothing.
MUTED = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
    can_manage_topics=False,
)

#: What a verified member gets back.
UNMUTED = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)


@dataclass(slots=True)
class PendingChallenge:
    """One member waiting to prove they are human."""

    chat_id: int
    user_id: int
    name: str
    token: str
    created_at: float
    message_id: int | None = None
    attempts: int = 0
    extra: dict[str, object] = field(default_factory=dict)

    def expired(self, timeout: float) -> bool:
        return (time.monotonic() - self.created_at) > timeout


class CaptchaManager:
    """Tracks pending challenges and applies the mute/unmute."""

    def __init__(self, *, timeout: float = 120.0, max_pending: int = 500) -> None:
        self.timeout = timeout
        self.max_pending = max_pending
        self._pending: dict[tuple[int, int], PendingChallenge] = {}

    # -- bookkeeping ---------------------------------------------------------

    @property
    def count(self) -> int:
        return len(self._pending)

    def get(self, chat_id: int, user_id: int) -> PendingChallenge | None:
        return self._pending.get((chat_id, user_id))

    def by_token(self, token: str) -> PendingChallenge | None:
        for challenge in self._pending.values():
            if challenge.token == token:
                return challenge
        return None

    def drop(self, chat_id: int, user_id: int) -> PendingChallenge | None:
        return self._pending.pop((chat_id, user_id), None)

    def _remember(self, challenge: PendingChallenge) -> None:
        if len(self._pending) > self.max_pending:
            # Drop the oldest half rather than refusing new members.
            oldest = sorted(self._pending.items(), key=lambda kv: kv[1].created_at)
            for key, _ in oldest[: len(oldest) // 2]:
                self._pending.pop(key, None)
        self._pending[(challenge.chat_id, challenge.user_id)] = challenge

    # -- the flow ------------------------------------------------------------

    async def challenge(
        self,
        bot: Bot,
        chat_id: int,
        user_id: int,
        name: str,
        *,
        chat_title: str = "",
    ) -> PendingChallenge:
        """Mute a new member and send them the button."""
        from ..rich.sender import rich_send

        token = secrets.token_urlsafe(9)
        challenge = PendingChallenge(
            chat_id=chat_id,
            user_id=user_id,
            name=name,
            token=token,
            created_at=time.monotonic(),
        )
        try:
            await bot.restrict_chat_member(chat_id, user_id, permissions=MUTED)
        except TelegramAPIError as exc:
            log.warning("cannot mute %s in %s: %s", user_id, chat_id, exc)
            return challenge  # nothing more we can do; do not block the join

        body = ui.screen(
            "welcome",
            icon="handshake",
            subtitle=f"{name} · verify to start talking",
            blocks_=[
                ui.panel(
                    ui.kv("ᴡʜʏ", "ᴛʜɪꜱ ᴋᴇᴇᴘꜱ ʙᴏᴛꜱ ᴀɴᴅ ꜱᴄᴀᴍᴍᴇʀꜱ ᴏᴜᴛ"),
                    ui.kv("ʜᴏᴡ", f"ᴛᴀᴘ ᴛʜᴇ ʙᴜᴛᴛᴏɴ · {int(self.timeout)}ꜱ ᴛᴏ ᴠᴇʀɪꜰʏ"),
                ),
                ui.actions([ui.button("I'm human", callback=f"cap:{token}", style="success", icon="check")]),
                ui.footer_text("otherwise you will be removed - just /start me and try again"),
            ],
        )
        try:
            message = await rich_send(bot, chat_id, body, fallback=True)
            challenge.message_id = getattr(message, "message_id", None)
        except TelegramAPIError as exc:
            log.warning("captcha message failed in %s: %s", chat_id, exc)

        self._remember(challenge)
        return challenge

    async def solve(self, bot: Bot, challenge: PendingChallenge) -> bool:
        """Restore the member's permissions and clean up the prompt."""
        try:
            await bot.restrict_chat_member(
                challenge.chat_id, challenge.user_id, permissions=UNMUTED
            )
        except TelegramAPIError as exc:
            log.warning("cannot unmute %s: %s", challenge.user_id, exc)
            return False
        self.drop(challenge.chat_id, challenge.user_id)
        await self._cleanup(bot, challenge)
        return True

    async def fail(self, bot: Bot, challenge: PendingChallenge) -> None:
        """Remove a member who never solved it."""
        self.drop(challenge.chat_id, challenge.user_id)
        try:
            await bot.ban_chat_member(challenge.chat_id, challenge.user_id)
            await bot.unban_chat_member(
                challenge.chat_id, challenge.user_id, only_if_banned=True
            )
        except TelegramAPIError as exc:
            log.debug("cannot remove %s: %s", challenge.user_id, exc)
        await self._cleanup(bot, challenge)

    async def sweep(self, bot: Bot) -> int:
        """Expire timed-out challenges. Called by the watchdog."""
        expired = [c for c in self._pending.values() if c.expired(self.timeout)]
        for challenge in expired:
            log.info("captcha expired for %s in %s", challenge.user_id, challenge.chat_id)
            await self.fail(bot, challenge)
        return len(expired)

    @staticmethod
    async def _cleanup(bot: Bot, challenge: PendingChallenge) -> None:
        if challenge.message_id:
            try:
                await bot.delete_message(challenge.chat_id, challenge.message_id)
            except TelegramAPIError:
                pass
