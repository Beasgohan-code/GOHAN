"""The guardian's live handlers.

These are the ones that run *without* an admin asking:

``chat_join_request``  decide before the person is in the group
``new_chat_members``   welcome, and start a captcha when enabled
``left_chat_member``   optional goodbye
``message`` (group 5)  the message guard - scam/flood/duplicate detection

The message guard runs in group 5, *after* command handlers (group 0-2) but
before the catch-all, so normal commands are never inspected.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, JOIN_TRANSITION
from aiogram.types import ChatJoinRequest, ChatMemberUpdated, Message, ReplyParameters

from ..config import Settings
from ..content import WELCOME_DEFAULT
from ..filters import IsAdmin, IsGroup
from ..logging_setup import get_logger
from ..rich import ui
from ..rich.sender import rich_send
from ..storage import Database
from .captcha import CaptchaManager
from .filters import DuplicateTracker, FloodTracker, Verdict, inspect_message, is_forward_from_channel

log = get_logger("guardian.events")

router = Router(name="guardian.events")

__all__ = ["router", "apply_templates", "format_welcome"]

#: shared trackers (module level so every chat is covered by one instance)
_floods = FloodTracker(window=8.0, limit=7)
_dupes = DuplicateTracker(window=30.0, limit=3)

#: default toggles for a chat that has never been configured
DEFAULTS: dict[str, Any] = {
    "guardian": True,
    "captcha": False,
    "antiraid": True,
    "antispam": True,
    "welcome": True,
    "goodbye": False,
    "delete_links": False,
    "delete_forwards": False,
    "warn_limit": 3,
}


def settings_of(data: dict[str, Any]) -> dict[str, Any]:
    """Chat settings with defaults filled in."""
    current = dict(DEFAULTS)
    current.update(data.get("chat_settings") or {})
    return current


def format_welcome(template: str, message: Message, member: Any) -> str:
    """Fill the ``{name} {chat} {id} {count}`` placeholders of a welcome text."""
    name = ui.esc(" ".join(filter(None, (member.first_name, member.last_name))) or "friend")
    return (
        template.replace("{name}", f"<b>{name}</b>")
        .replace("{chat}", ui.esc(getattr(message.chat, "title", "") or ""))
        .replace("{id}", str(member.id))
        .replace("{mention}", ui.mention(name, member.id))
        .replace("{count}", str(getattr(message.chat, "id", "")))
    )


def apply_templates(text: str, message: Message, member: Any) -> str:
    return format_welcome(text, message, member)


# ---------------------------------------------------------------------------
#  join requests - the earliest possible decision point
# ---------------------------------------------------------------------------


@router.chat_join_request()
async def on_join_request(
    request: ChatJoinRequest,
    bot: Bot,
    db: Database,
    settings: Settings,
    raid: Any = None,
    log_channel: Any = None,
    **_: Any,
) -> None:
    """Approve or decline a join request.

    Default is **approve**: a guardian that keeps real people waiting is worse
    than the spam it prevents. During a detected raid, requests are declined and
    counted instead.
    """
    chat_settings = await db.get_chat_settings(request.chat.id)
    merged = dict(DEFAULTS)
    merged.update(chat_settings)

    if not merged.get("guardian", True):
        return

    user = request.from_user
    state = None
    if raid is not None and merged.get("antiraid", True):
        state = raid.record_join(
            request.chat.id,
            user.id,
            had_username=bool(user.username),
            had_photo=bool(getattr(user, "photo", None)),
            is_premium=bool(user.is_premium),
        )

    locked = state is not None and getattr(state, "value", "normal") == "lockdown"
    if locked:
        try:
            await bot.decline_chat_join_request(request.chat.id, user.id)
            if raid is not None:
                raid.chat(request.chat.id).declines += 1
        except TelegramAPIError as exc:
            log.warning("decline failed: %s", exc)
        if log_channel is not None:
            await log_channel.event(
                "RaidDecline",
                ui.screen(
                    "join declined",
                    icon="shield",
                    subtitle=ui.esc(request.chat.title or str(request.chat.id)),
                    blocks_=[
                        ui.panel(
                            ui.kv("👤 ᴜꜱᴇʀ", ui.esc(user.full_name)),
                            ui.kv("🆔 ɪᴅ", f"<code>{user.id}</code>"),
                            ui.kv("📡 ꜱᴛᴀᴛᴇ", "ʀᴀɪᴅ ʟᴏᴄᴋᴅᴏᴡɴ ᴀᴄᴛɪᴠᴇ"),
                        )
                    ],
                ),
            )
        return

    try:
        await bot.approve_chat_join_request(request.chat.id, user.id)
    except TelegramAPIError as exc:
        log.debug("approve failed: %s", exc)


# ---------------------------------------------------------------------------
#  joins / leaves
# ---------------------------------------------------------------------------


@router.chat_member(JOIN_TRANSITION)
async def on_member_join(
    event: ChatMemberUpdated,
    bot: Bot,
    db: Database,
    settings: Settings,
    captcha: CaptchaManager | None = None,
    raid: Any = None,
    log_channel: Any = None,
    **_: Any,
) -> None:
    """Welcome a new member, and challenge them if captcha is on."""
    chat = event.chat
    user = event.new_chat_member.user
    if user.is_bot:
        return

    state = dict(DEFAULTS)
    state.update(await db.get_chat_settings(chat.id))
    if not state.get("guardian", True):
        return

    if raid is not None and state.get("antiraid", True):
        result = raid.record_join(
            chat.id,
            user.id,
            had_username=bool(user.username),
            had_photo=bool(getattr(user, "photo", None)),
            is_premium=bool(user.is_premium),
        )
        if getattr(result, "value", "normal") == "lockdown" and captcha is not None:
            # Under raid conditions everyone is challenged, no matter what the
            # normal captcha setting says.
            await captcha.challenge(bot, chat.id, user.id, user.full_name, chat_title=chat.title or "")
            if log_channel is not None:
                await log_channel.event(
                    "RaidLockdown",
                    ui.screen(
                        "raid detected",
                        icon="siren",
                        subtitle=ui.esc(chat.title or str(chat.id)),
                        blocks_=[
                            ui.kv_panel(
                                [
                                    ("📊 ᴊᴏɪɴꜱ", str(raid.joins_in_window(chat.id))),
                                    ("🎯 ꜱᴜꜱᴘɪᴄɪᴏɴ", f"{raid.suspicion(chat.id):.0%}"),
                                ],
                                icon="chart",
                            ),
                            ui.panel("ᴀᴜᴛᴏ-ᴍᴜᴛɪɴɢ ɴᴇᴡ ᴍᴇᴍʙᴇʀꜱ ᴜɴᴛɪʟ ᴛʜᴇʏ ᴠᴇʀɪꜰʏ"),
                        ],
                    ),
                )
            return

    if state.get("captcha") and captcha is not None:
        await captcha.challenge(bot, chat.id, user.id, user.full_name, chat_title=chat.title or "")
        return

    if not state.get("welcome", True):
        return

    template = str(state.get("welcome_text") or WELCOME_DEFAULT)
    body = ui.panel(
        format_welcome(template, event, user),
        icon="handshake",
    )
    await rich_send(bot, chat.id, body, fallback=True)
    try:
        await db.log_event("Join", chat_id=chat.id, user_id=user.id)
    except Exception:
        pass


@router.chat_member(F.chat_member.new_chat_member.status.in_({"left", "kicked"}))  # type: ignore[union-attr]
async def on_member_leave(
    event: ChatMemberUpdated,
    bot: Bot,
    db: Database,
    settings: Settings,
    **_: Any,
) -> None:
    """Optional goodbye message."""
    state = dict(DEFAULTS)
    state.update(await db.get_chat_settings(event.chat.id))
    if not state.get("goodbye"):
        return
    user = event.new_chat_member.user
    await rich_send(
        bot,
        event.chat.id,
        ui.panel(ui.italic(ui.caps(f"{user.full_name} left the group")), icon="moon"),
        fallback=True,
    )


# ---------------------------------------------------------------------------
#  the message guard
# ---------------------------------------------------------------------------

router.message.filter(IsGroup())


@router.message(F.new_chat_members)
async def on_new_chat_members(
    message: Message,
    bot: Bot,
    db: Database,
    settings: Settings,
    captcha: CaptchaManager | None = None,
    **_: Any,
) -> None:
    """Bots have no ``chat_member`` update in small groups, so handle the
    ``new_chat_members`` service message too."""
    state = dict(DEFAULTS)
    state.update(await db.get_chat_settings(message.chat.id))
    for member in message.new_chat_members or []:
        if member.is_bot:
            continue
        if state.get("captcha") and captcha is not None:
            await captcha.challenge(
                bot, message.chat.id, member.id, member.full_name, chat_title=message.chat.title or ""
            )
        elif state.get("welcome", True):
            template = str(state.get("welcome_text") or WELCOME_DEFAULT)
            await rich_send(
                bot,
                message.chat.id,
                ui.panel(format_welcome(template, message, member), icon="handshake"),
                fallback=True,
            )
    try:
        await message.delete()
    except TelegramAPIError:
        pass


@router.message(IsGroup(), F.text | F.caption, flags={"guard": True})
async def message_guard(
    message: Message,
    bot: Bot,
    db: Database,
    settings: Settings,
    log_channel: Any = None,
    raid: Any = None,
    **_: Any,
) -> None:
    """Inspect group messages and act on the verdict.

    Deliberately conservative:

    * trusted members (seen in the chat for a while) get half the link penalty,
    * only ``score >= 100`` is deleted outright,
    * ``60-99`` is deleted **only** when the author is new (under 3 messages),
    * everything suspicious is reported to the admins either way.
    """
    if message.from_user is None or message.from_user.is_bot:
        return

    state = dict(DEFAULTS)
    state.update(await db.get_chat_settings(message.chat.id))
    if not state.get("guardian", True) or not state.get("antispam", True):
        return

    # --- flood ------------------------------------------------------------
    flooding, count = _floods.hit(message.chat.id, message.from_user.id)
    if flooding:
        _floods.forget(message.chat.id, message.from_user.id)
        await _punish(
            bot, message, db, log_channel, reason=f"flood ({count} messages in 8s)", action="mute"
        )
        return

    # --- exact duplicates -------------------------------------------------
    if message.text and _dupes.check(message.chat.id, message.from_user.id, message.text):
        await _punish(bot, message, db, log_channel, reason="repeated identical messages", action="delete")
        return

    # --- content ----------------------------------------------------------
    user_record = await db.get_user(message.from_user.id)
    seen_count = 0 if user_record is None else user_record.start_count
    member = None
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    except TelegramAPIError:
        pass
    is_trusted = bool(
        member is not None
        and getattr(member, "status", "member") in ("member", "administrator", "creator", "restricted")
        and user_record is not None
        and (time.time() - user_record.first_seen) > 7 * 86400
    )

    verdict = inspect_message(
        message.text,
        caption=message.caption,
        from_channel=is_forward_from_channel(message),
        is_trusted=is_trusted,
    )
    if state.get("delete_links") and verdict.links.has_link:
        verdict.score += 60
        verdict.reasons.append("links not allowed here")
    if state.get("delete_forwards") and is_forward_from_channel(message):
        verdict.score += 60
        verdict.reasons.append("forwards not allowed here")

    if verdict.score < 60:
        return

    is_newcomer = seen_count < 3
    if verdict.should_delete or (verdict.should_flag and is_newcomer and not is_trusted):
        await _punish(
            bot,
            message,
            db,
            log_channel,
            reason=verdict.reason_text,
            action="delete" if is_newcomer or verdict.should_delete else "flag",
        )
    elif log_channel is not None:
        await log_channel.event(
            "Suspect",
            ui.screen(
                "suspicious message",
                icon="eye",
                subtitle=ui.esc(message.chat.title or str(message.chat.id)),
                blocks_=[
                    ui.kv_panel(
                        [
                            ("👤 ᴜꜱᴇʀ", ui.esc(message.from_user.full_name)),
                            ("🆔 ɪᴅ", f"<code>{message.from_user.id}</code>"),
                            ("📊 ꜱᴄᴏʀᴇ", f"{verdict.score} · {ui.esc(verdict.reason_text)}"),
                            ("🧾 ᴛʀᴜꜱᴛᴇᴅ", "yes" if is_trusted else "no"),
                        ]
                    ),
                    ui.panel(ui.esc((message.text or message.caption or "")[:300])),
                ],
            ),
        )


async def _punish(
    bot: Bot,
    message: Message,
    db: Database,
    log_channel: Any,
    *,
    reason: str,
    action: str,
) -> None:
    """Delete the message, warn the author, and tell the admins."""
    chat_id = message.chat.id
    user = message.from_user
    deleted = False
    try:
        await message.delete()
        deleted = True
    except TelegramAPIError as exc:
        log.debug("delete failed: %s", exc)

    warnings = await db.add_warning(chat_id, user.id, reason=f"auto: {reason}")
    try:
        await db.log_event("AutoGuard", chat_id=chat_id, user_id=user.id, data={"reason": reason})
    except Exception:
        pass

    if action == "mute":
        try:
            from .captcha import MUTED

            await bot.restrict_chat_member(chat_id, user.id, permissions=MUTED)
            reason += " · muted"
        except TelegramAPIError as exc:
            log.debug("auto-mute failed: %s", exc)

    if log_channel is not None:
        await log_channel.event(
            "AutoGuard",
            ui.screen(
                "guardian action",
                icon="shield",
                subtitle=ui.esc(message.chat.title or str(chat_id)),
                blocks_=[
                    ui.kv_panel(
                        [
                            ("👤 ᴜꜱᴇʀ", ui.esc(user.full_name)),
                            ("🆔 ɪᴅ", f"<code>{user.id}</code>"),
                            ("📝 ᴡʜʏ", ui.esc(reason)),
                            ("🗑 ᴅᴇʟᴇᴛᴇᴅ", "yes" if deleted else "no"),
                            ("⚠️ ᴡᴀʀɴɪɴɢꜱ", str(warnings)),
                        ],
                        icon="warning",
                    ),
                    ui.panel(ui.italic(ui.esc((message.text or message.caption or "(media)")[:280]))),
                ],
            ),
        )


# ---------------------------------------------------------------------------
#  captcha callback + rules/welcome administration
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("cap:"))
async def on_captcha_press(callback: Any, bot: Bot, captcha: CaptchaManager | None = None, **_: Any) -> None:
    """The "I'm human" button."""
    if captcha is None:
        await callback.answer("captcha is not running", show_alert=True)
        return
    token = (callback.data or "").split(":", 1)[-1]
    challenge = captcha.by_token(token)
    if challenge is None:
        await callback.answer("this challenge expired", show_alert=True)
        return
    if callback.from_user.id != challenge.user_id:
        await callback.answer("that button is not for you 🙂", show_alert=True)
        return
    ok = await captcha.solve(bot, challenge)
    await callback.answer("verified - welcome!" if ok else "could not verify, tell an admin", show_alert=not ok)


@router.message(Command("rules"), IsGroup())
async def cmd_rules(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    """Show or set the group rules."""
    text = (command.args or "").strip()
    if text and message.from_user:
        from ..filters import is_admin

        if await is_admin(bot, message.chat.id, message.from_user.id, settings):
            await db.set_chat_setting(message.chat.id, "rules", text[:1500])
            await rich_send(bot, message.chat.id, ui.panel(ui.ok("rules updated"), icon="check"))
            return
    rules = await db.get_chat_settings(message.chat.id)
    body = rules.get("rules")
    await rich_send(
        bot,
        message.chat.id,
        ui.screen(
            "rules",
            icon="book",
            subtitle=message.chat.title or "",
            blocks_=[ui.panel(ui.esc(body)) if body else ui.italic("no rules set yet - an admin can add them with /rules <text>")],
        ),
    )


@router.message(Command("welcome"), IsGroup())
async def cmd_welcome(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    """``/welcome <text>`` - set the welcome message (``{name}``, ``{chat}``, ``{id}``)."""
    if not await IsAdmin()(message, bot=bot, settings=settings):
        await rich_send(bot, message.chat.id, ui.panel(ui.no("admins only"), icon="lock"))
        return
    text = (command.args or "").strip()
    if not text:
        current = dict(DEFAULTS)
        current.update(await db.get_chat_settings(message.chat.id))
        await rich_send(
            bot,
            message.chat.id,
            ui.screen(
                "welcome message",
                icon="handshake",
                blocks_=[
                    ui.panel(ui.esc(current.get("welcome_text") or WELCOME_DEFAULT)),
                    ui.nav_hint()
                    if hasattr(ui, "nav_hint")
                    else ui.italic("placeholders: {name} {chat} {id} {mention}"),
                ],
            ),
        )
        return
    await db.set_chat_setting(message.chat.id, "welcome_text", text[:600])
    await rich_send(
        bot, message.chat.id, ui.panel(ui.ok("welcome message saved"), icon="check")
    )


# ---------------------------------------------------------------------------
#  filters: trigger -> reply (see gohan.handlers.filters for management)
# ---------------------------------------------------------------------------

#: Filters run on their own router, *before* the message guard, because aiogram
#: stops at the first matching handler: the guard matches every text message and
#: would otherwise swallow the triggers.
trigger_router = Router(name="guardian.filters")

#: small cache so a busy chat does not hit sqlite for every message
_FILTER_TTL = 20.0
_filter_cache: dict[int, tuple[float, dict[str, Any]]] = {}


async def _filters_for(db: Database, chat_id: int) -> dict[str, Any]:
    stamp, cached = _filter_cache.get(chat_id, (0.0, {}))
    if time.time() - stamp < _FILTER_TTL and cached:
        return cached
    rows = await db.list_filters(chat_id)
    table = {row["trigger"]: row for row in rows}
    _filter_cache[chat_id] = (time.time(), table)
    return table


@trigger_router.message(F.text | F.caption)
async def filter_engine(message: Message, bot: Bot, db: Database) -> None:
    """Answer a message when it contains a saved filter trigger."""
    if message.from_user is not None and message.from_user.is_bot:
        return
    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/"):
        return

    filters = await _filters_for(db, message.chat.id)
    if not filters:
        return

    lowered = text.lower()
    words = set(re.findall(r"[\w#-]{2,}", lowered))
    hit = next(
        (
            trigger
            for trigger in filters
            if (trigger in words) or (" " in trigger and trigger in lowered)
        ),
        None,
    )
    if hit is None:
        return

    row = filters[hit]
    kind = row["kind"] or "text"
    try:
        if kind == "text":
            await rich_send(
                bot,
                message.chat.id,
                ui.panel(ui.esc(row["reply"] or "")),
                reply_parameters=ReplyParameters(message_id=message.message_id),
            )
        else:
            sender = {
                "photo": bot.send_photo,
                "video": bot.send_video,
                "animation": bot.send_animation,
                "sticker": bot.send_sticker,
                "voice": bot.send_voice,
            }.get(kind)
            if sender is None or not row["file_id"]:
                return
            kwargs: dict[str, Any] = {"reply_parameters": ReplyParameters(message_id=message.message_id)}
            if kind != "sticker" and row["reply"]:
                kwargs["caption"] = row["reply"][:1000]
            await sender(message.chat.id, row["file_id"], **kwargs)
    except Exception as exc:
        log.debug("filter reply failed: %s", exc)
        return

    try:
        await db.bump_filter_uses(message.chat.id, hit)
        _filter_cache.pop(message.chat.id, None)
    except Exception:
        pass


def invalidate_filters(chat_id: int) -> None:
    """Called by the management handlers after an edit."""
    _filter_cache.pop(chat_id, None)


__all__ += ["DEFAULTS", "invalidate_filters", "settings_of", "trigger_router"]
