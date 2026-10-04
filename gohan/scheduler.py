"""Background scheduler: publishes due posts every few seconds."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from aiogram import Bot

from .mtproto import MtpEngine
from .publisher import publish
from .storage import Storage

log = logging.getLogger("gohan.scheduler")


class Scheduler:
    def __init__(self, bot: Bot, storage: Storage, mtp: MtpEngine) -> None:
        self.bot = bot
        self.storage = storage
        self.mtp = mtp
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.create_task(self._loop(), name="gohan-scheduler")

    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")

    async def tick(self) -> None:
        for post in await self.storage.due_posts(time.time()):
            payload = json.loads(post["data"])
            try:
                message_id, engine = await publish(
                    self.bot,
                    self.mtp,
                    post["chat_id"],
                    payload["text"],
                    payload.get("mode", "auto"),
                    payload.get("buttons", []),
                    payload.get("options", {}),
                )
            except Exception as exc:  # noqa: BLE001
                await self.storage.finish_post(post["id"], "failed", error=str(exc)[:400])
                log.warning("scheduled post %s failed: %r", post["id"], exc)
                await self._notify(post["user_id"], post["id"], False, str(exc))
            else:
                await self.storage.finish_post(post["id"], "done", message_id=message_id)
                log.info(
                    "scheduled post %s published via %s (msg %s)",
                    post["id"],
                    engine,
                    message_id,
                )
                await self._notify(post["user_id"], post["id"], True, engine)

    async def _notify(self, user_id: int, post_id: int, ok: bool, detail: str) -> None:
        try:
            if ok:
                text = f"✅ Scheduled post #{post_id} published ({detail})."
            else:
                text = f"❌ Scheduled post #{post_id} failed: {detail[:300]}"
            await self.bot.send_message(user_id, text)
        except Exception:  # owner may have blocked the bot — never crash the loop
            log.debug("could not notify user %s", user_id, exc_info=True)
