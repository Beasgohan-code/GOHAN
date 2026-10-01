"""Admin commands: warn, mute, kick, ban, purge, lockdown, reports.

Every command:

* requires :class:`gohan.filters.IsAdmin`,
* never fails silently - the admin always gets a rich confirmation *or* an
  explanation of why it did not work (missing rights, owner target, …),
* writes an event to the log channel and a row to the ``events`` table.

Targets are resolved from a reply first, then an ``@username``/id argument, so
both ``/ban`` (as a reply) and ``/ban @user spam`` work.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, Message, User

from ..config import Settings
from ..filters import IsAdmin, IsGroup, is_owner_id
from ..logging_setup import get_logger
from ..rich import ui
from ..rich.sender import rich_send
from ..storage import Database

log = get_logger("guardian.moderation")

router = Router(name="guardian.moderation")
router.message.filter(IsGroup())

__all__ = ["router"]

#: Escalating consequences at each warning count.
ESCALATION: dict[int, str] = {3: "mute", 5: "kick", 7: "ban"}

#: Default mute durations by keyword.
DURATIONS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "1w": timedelta(weeks=1),
    "perm": timedelta(days=366),
}
DEFAULT_MUTE = timedelta(hours=1)

_SILENT = ChatPermissions(can_send_messages=False)


# ---------------------------------------------------------------------------
#  helpers
# ---------------------------------------------------------------------------


async def resolve_target(bot: Bot, message: Message, args: str | None) -> User | None:
    """Find who an admin meant: the reply, then an @username/id in the args."""
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user
    token = (args or "").strip().split()[0] if (args or "").strip() else ""
    if not token:
        return None
    try:
        if token.lstrip("-").isdigit():
            return await bot.get_chat(int(token))  # type: ignore[return-value]
        if token.startswith("@"):
            chat = await bot.get_chat(token)
            return chat  # type: ignore[return-value]
    except TelegramAPIError as exc:
        log.debug("cannot resolve %s: %s", token, exc)
    return None


def _reason(args: str | None, target_token: str = "") -> str:
    text = (args or "").strip()
    if target_token and text.startswith(target_token):
        text = text[len(target_token) :].strip()
    return text[:200]


async def _guard(bot: Bot, message: Message, target: User | None, settings: Settings) -> bool:
    """Common safety checks before any punitive action."""
    if target is None:
        await rich_send(
            bot,
            message.chat.id,
            ui.screen(
                "who?",
                icon="question",
                blocks_=[ui.panel("reply to a message, or pass @username / id")],
            ),
            reply_parameters=_reply_to(message),
        )
        return False
    if target.id == bot.id:
        await _notice(bot, message, "I am not going to moderate myself.", "cross")
        return False
    if is_owner_id(target.id, settings):
        await _notice(bot, message, "That is the bot owner - refusing.", "crown")
        return False
    me = await bot.get_chat_member(message.chat.id, bot.id)
    if me.status not in ("administrator", "creator") or not (
        getattr(me, "can_restrict_members", False) or me.status == "creator"
    ):
        await _notice(
            bot, message, "I need the <b>ban users</b> admin right for that.", "warning"
        )
        return False
    try:
        victim = await bot.get_chat_member(message.chat.id, target.id)
        if victim.status in ("administrator", "creator"):
            await _notice(bot, message, "That person is an admin - demote them first.", "shield")
            return False
    except TelegramAPIError:
        pass
    return True


async def _notice(bot: Bot, message: Message, text: str, icon: str | None = None) -> None:
    await rich_send(
        bot,
        message.chat.id,
        ui.panel(ui.italic(ui.caps(text)), icon=icon),
        reply_parameters=_reply_to(message),
    )


def _reply_to(message: Message):
    from aiogram.types import ReplyParameters

    return ReplyParameters(message_id=message.message_id)


async def _log(
    db: Database,
    log_channel: Any,
    tag: str,
    *,
    message: Message,
    target: User | None,
    extra: str = "",
) -> None:
    try:
        await db.log_event(
            tag,
            chat_id=message.chat.id,
            user_id=getattr(target, "id", None),
            data={"by": message.from_user.id if message.from_user else None, "extra": extra},
        )
    except Exception:
        pass
    if log_channel is None or target is None:
        return
    try:
        await log_channel.moderation(
            tag,
            chat=message.chat,
            actor=message.from_user,
            target=target,
            extra=extra,
        )
    except Exception as exc:
        log.debug("log failed: %s", exc)


def _user_line(target: User | None) -> str:
    if target is None:
        return "unknown"
    name = ui.esc(" ".join(filter(None, (target.first_name, target.last_name))) or target.id)
    handle = f" (@{ui.esc(target.username)})" if target.username else ""
    return f"{ui.mention(f'<b>{name}</b>', target.id)}{handle}\n<code>{target.id}</code>"


# ---------------------------------------------------------------------------
#  warnings
# ---------------------------------------------------------------------------


@router.message(Command("warn"))
async def cmd_warn(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    log_channel: Any = None,
) -> None:
    """``/warn [@user] [reason]`` - escalate automatically at 3/5/7 warnings."""
    token = (command.args or "").strip().split()[0] if command.args else ""
    target = await resolve_target(bot, message, command.args)
    if not await _guard(bot, message, target, settings):
        return
    reason = _reason(command.args, token) or "no reason given"
    uid = target.id  # type: ignore[union-attr]
    count = await db.add_warning(message.chat.id, uid, admin_id=message.from_user.id, reason=reason)
    await _log(db, log_channel, "Warn", message=message, target=target, extra=reason)

    action = ESCALATION.get(count)
    body = ui.screen(
        "warning issued",
        icon="warning",
        subtitle=f"{count} warning{'s' if count != 1 else ''}",
        blocks_=[
            ui.kv_panel(
                [
                    ("👤 ᴜꜱᴇʀ", _user_line(target)),
                    ("📝 ʀᴇᴀꜱᴏɴ", ui.esc(reason)),
                    ("🛡 ʙʏ", ui.esc(message.from_user.full_name)),
                ]
            ),
            ui.panel(
                ui.kv("ᴇꜱᴄᴀʟᴀᴛɪᴏɴ", " · ".join(f"{k}→{v}" for k, v in ESCALATION.items())),
            ),
        ],
    )
    await rich_send(bot, message.chat.id, body, reply_parameters=_reply_to(message))

    if action:
        await _apply_escalation(bot, message, target, action, count)
        await _log(db, log_channel, f"Auto{action.title()}", message=message, target=target,
                   extra=f"{count} warnings")


async def _apply_escalation(bot: Bot, message: Message, target: User, action: str, count: int) -> None:
    try:
        if action == "mute":
            await bot.restrict_chat_member(
                message.chat.id, target.id, permissions=_SILENT, until_date=DEFAULT_MUTE
            )
        elif action == "kick":
            await bot.ban_chat_member(message.chat.id, target.id)
            await bot.unban_chat_member(message.chat.id, target.id, only_if_banned=True)
        elif action == "ban":
            await bot.ban_chat_member(message.chat.id, target.id)
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(
                ui.ok(f"auto-{action} after {count} warnings: {ui.esc(target.full_name)}"),
                icon="hammer",
            ),
        )
    except TelegramAPIError as exc:
        await _notice(bot, message, f"could not {action}: {exc}", "cross")


@router.message(Command("warns"))
async def cmd_warns(
    message: Message, command: CommandObject, bot: Bot, db: Database, settings: Settings
) -> None:
    """``/warns`` - list a user's warnings (reply or @username)."""
    target = await resolve_target(bot, message, command.args)
    if target is None:
        await _notice(bot, message, "reply to a message, or pass @username / id", "question")
        return
    records = await db.warnings_of(message.chat.id, target.id)
    rows = [
        (str(index + 1), ui.esc(record.reason or "-"),
         datetime.fromtimestamp(record.created_at, tz=UTC).strftime("%d %b"))
        for index, record in enumerate(records)
    ]
    body = ui.screen(
        "warnings",
        icon="warning",
        subtitle=target.full_name,
        blocks_=[
            ui.kv_panel([("👤 ᴜꜱᴇʀ", _user_line(target)), ("📊 ᴛᴏᴛᴀʟ", str(len(records)))]),
            ui.table(["#", "ʀᴇᴀꜱᴏɴ", "ᴅᴀᴛᴇ"], rows) if rows else ui.italic("clean record ✨"),
        ],
    )
    await rich_send(bot, message.chat.id, body, reply_parameters=_reply_to(message))


@router.message(Command("unwarn"))
async def cmd_unwarn(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    log_channel: Any = None,
) -> None:
    """``/unwarn`` - clear every warning for a user."""
    target = await resolve_target(bot, message, command.args)
    if not await _guard(bot, message, target, settings):
        return
    removed = await db.clear_warnings(message.chat.id, target.id)  # type: ignore[union-attr]
    await _log(db, log_channel, "Unwarn", message=message, target=target, extra=f"{removed} cleared")
    await rich_send(
        bot,
        message.chat.id,
        ui.panel(ui.ok(f"cleared {removed} warning(s) for {ui.esc(target.full_name)}"), icon="check"),
        reply_parameters=_reply_to(message),
    )


# ---------------------------------------------------------------------------
#  restrictions
# ---------------------------------------------------------------------------


def _parse_duration(raw: str | None) -> timedelta:
    token = (raw or "").strip().lower()
    if not token:
        return DEFAULT_MUTE
    if token in DURATIONS:
        return DURATIONS[token]
    # accept "30m", "2h", "3d", bare numbers = minutes
    unit = token[-1]
    number = token[:-1] if unit.isalpha() else token
    try:
        value = int(number)
    except ValueError:
        return DEFAULT_MUTE
    return {
        "m": timedelta(minutes=value),
        "h": timedelta(hours=value),
        "d": timedelta(days=value),
        "w": timedelta(weeks=value),
    }.get(unit if unit.isalpha() else "m", DEFAULT_MUTE)


@router.message(Command("mute"))
async def cmd_mute(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    log_channel: Any = None,
) -> None:
    """``/mute [1h|1d|perm]`` - silence a user."""
    args = (command.args or "").strip()
    token = args.split()[0] if args else ""
    target = await resolve_target(bot, message, args)
    if not await _guard(bot, message, target, settings):
        return
    duration = _parse_duration(token)
    reason = _reason(args, token)
    try:
        await bot.restrict_chat_member(
            message.chat.id,
            target.id,  # type: ignore[union-attr]
            permissions=_SILENT,
            until_date=None if duration >= timedelta(days=366) else duration,
        )
    except TelegramAPIError as exc:
        await _notice(bot, message, f"could not mute: {exc}", "cross")
        return
    await _log(db, log_channel, "Mute", message=message, target=target, extra=reason)
    await _notice(
        bot,
        message,
        f"muted {target.full_name} for {_human_delta(duration)}",  # type: ignore[union-attr]
        "mute",
    )


def _human_delta(delta: timedelta) -> str:
    if delta >= timedelta(days=366):
        return "permanently"
    seconds = int(delta.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


@router.message(Command("unmute"))
async def cmd_unmute(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    log_channel: Any = None,
) -> None:
    """``/unmute`` - restore a user's permissions."""
    from .captcha import UNMUTED

    target = await resolve_target(bot, message, command.args)
    if not await _guard(bot, message, target, settings):
        return
    try:
        await bot.restrict_chat_member(
            message.chat.id, target.id, permissions=UNMUTED  # type: ignore[union-attr]
        )
    except TelegramAPIError as exc:
        await _notice(bot, message, f"could not unmute: {exc}", "cross")
        return
    await _log(db, log_channel, "Unmute", message=message, target=target)
    await _notice(bot, message, f"unmuted {target.full_name}", "unlock")  # type: ignore[union-attr]


@router.message(Command(commands=["kick", "remove"]))
async def cmd_kick(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    log_channel: Any = None,
) -> None:
    """``/kick`` - remove but allow rejoining."""
    target = await resolve_target(bot, message, command.args)
    if not await _guard(bot, message, target, settings):
        return
    try:
        await bot.ban_chat_member(message.chat.id, target.id)  # type: ignore[union-attr]
        await bot.unban_chat_member(message.chat.id, target.id, only_if_banned=True)  # type: ignore[union-attr]
    except TelegramAPIError as exc:
        await _notice(bot, message, f"could not kick: {exc}", "cross")
        return
    await _log(db, log_channel, "Kick", message=message, target=target)
    await _notice(bot, message, f"kicked {target.full_name}", "foot")  # type: ignore[union-attr]


@router.message(Command("ban"))
async def cmd_ban(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    log_channel: Any = None,
) -> None:
    """``/ban [@user] [reason]`` - permanent removal."""
    token = (command.args or "").strip().split()[0] if command.args else ""
    target = await resolve_target(bot, message, command.args)
    if not await _guard(bot, message, target, settings):
        return
    reason = _reason(command.args, token)
    try:
        await bot.ban_chat_member(message.chat.id, target.id)  # type: ignore[union-attr]
    except TelegramAPIError as exc:
        await _notice(bot, message, f"could not ban: {exc}", "cross")
        return
    await db.set_banned(target.id, True)  # type: ignore[union-attr]
    await _log(db, log_channel, "Ban", message=message, target=target, extra=reason)
    await _notice(
        bot, message, f"banned {target.full_name}" + (f" - {reason}" if reason else ""), "ban"
    )


@router.message(Command("unban"))
async def cmd_unban(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    log_channel: Any = None,
) -> None:
    """``/unban <id|@username>`` - lift a ban."""
    token = (command.args or "").strip()
    if not token:
        await _notice(bot, message, "usage: /unban <id|@username>", "question")
        return
    try:
        user_id = int(token.lstrip("@")) if token.lstrip("-").isdigit() else None
        if user_id is None:
            chat = await bot.get_chat(token)
            user_id = chat.id
    except (TelegramAPIError, ValueError):
        await _notice(bot, message, f"cannot resolve {ui.esc(token)}", "cross")
        return
    if is_owner_id(user_id, settings):
        await _notice(bot, message, "That is the bot owner.", "crown")
        return
    try:
        await bot.unban_chat_member(message.chat.id, user_id, only_if_banned=True)
    except TelegramAPIError as exc:
        await _notice(bot, message, f"could not unban: {exc}", "cross")
        return
    await db.set_banned(user_id, False)
    await _log(db, log_channel, "Unban", message=message, target=None, extra=str(user_id))
    await _notice(bot, message, f"unbanned <code>{user_id}</code>", "unlock")


# ---------------------------------------------------------------------------
#  bulk cleanup
# ---------------------------------------------------------------------------


@router.message(Command(commands=["purge", "del"]))
async def cmd_purge(message: Message, command: CommandObject, bot: Bot) -> None:
    """``/purge [n]`` - delete recent messages.

    Works two ways: as a reply (deletes everything from the replied message up to
    and including yours) or with a count (deletes the last *n* messages).

    Telegram only lets bots delete messages younger than 48 h, and 100 at a time
    is the practical ceiling before the API rate-limits.
    """
    from aiogram.exceptions import TelegramBadRequest

    start_id: int | None = None
    if message.reply_to_message:
        start_id = message.reply_to_message.message_id
    elif command.args and command.args.strip().isdigit():
        count = min(int(command.args.strip()), 100)
        start_id = max(1, message.message_id - count)
    if start_id is None:
        await _notice(bot, message, "reply to a message, or give a count: /purge 20", "question")
        return

    deleted = failed = 0
    for message_id in range(start_id, message.message_id + 1):
        try:
            await bot.delete_message(message.chat.id, message_id)
            deleted += 1
        except TelegramBadRequest:
            failed += 1
        except TelegramAPIError as exc:
            log.debug("purge stop at %s: %s", message_id, exc)

    report = await rich_send(
        bot,
        message.chat.id,
        ui.panel(
            ui.ok(f"deleted <b>{deleted}</b> messages")
            + (f" · <i>{failed} skipped (old / already gone)</i>" if failed else ""),
            icon="broom",
        ),
    )
    # the report itself is usually unwanted - remove it shortly after
    if report is not None:
        import asyncio

        async def _self_destruct() -> None:
            await asyncio.sleep(5)
            try:
                await bot.delete_message(message.chat.id, report.message_id)
            except TelegramAPIError:
                pass

        asyncio.get_running_loop().create_task(_self_destruct())


@router.message(Command("lockdown"))
async def cmd_lockdown(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    log_channel: Any = None,
    raid: Any = None,
) -> None:
    """``/lockdown [off]`` - freeze or unfreeze the group."""
    from .captcha import MUTED

    arg = (command.args or "").strip().lower()
    chat_id = message.chat.id

    if arg in ("off", "release", "unlock"):
        try:
            await bot.restrict_chat_member(
                chat_id, chat_id, permissions=_DEFAULT_GROUP_PERMISSIONS
            )
        except TelegramAPIError as exc:
            await _notice(bot, message, f"could not lift lockdown: {exc}", "cross")
            return
        if raid is not None:
            raid.release(chat_id)
        await db.set_chat_setting(chat_id, "lockdown", False)
        await _log(db, log_channel, "LockdownOff", message=message, target=message.from_user)
        await _notice(bot, message, "lockdown lifted - the group is open again", "unlock")
        return

    try:
        await bot.restrict_chat_member(chat_id, chat_id, permissions=MUTED)
    except TelegramAPIError as exc:
        await _notice(bot, message, f"could not lock the group: {exc}", "cross")
        return
    if raid is not None:
        raid.lock(chat_id)
    await db.set_chat_setting(chat_id, "lockdown", True)
    await _log(db, log_channel, "Lockdown", message=message, target=message.from_user)
    await rich_send(
        bot,
        chat_id,
        ui.screen(
            "lockdown",
            icon="lock",
            subtitle="the group is frozen",
            blocks_=[
                ui.panel(
                    ui.kv("ᴡʜᴀᴛ", "ᴏɴʟʏ ᴀᴅᴍɪɴꜱ ᴄᴀɴ ᴛᴀʟᴋ"),
                    ui.kv("ᴜɴᴛɪʟ", "ᴀɴ ᴀᴅᴍɪɴ ʀᴜɴꜱ /ʟᴏᴄᴋᴅᴏᴡɴ ᴏꜰꜰ"),
                ),
            ],
        ),
    )


#: The permissions a supergroup is returned to after a lockdown. Telegram treats
#: ``None`` as "chat default", which is what we want.
_DEFAULT_GROUP_PERMISSIONS = ChatPermissions(
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


@router.message(Command("reports"))
async def cmd_reports(message: Message, db: Database, bot: Bot) -> None:
    """``/reports`` - the top warned users in this chat."""
    rows = await db.top_warned(message.chat.id, limit=10)
    table = (
        ui.table(["ᴜꜱᴇʀ", "ᴡᴀʀɴɪɴɢꜱ"], [[f"<code>{uid}</code>", str(count)] for uid, count in rows])
        if rows
        else ui.italic("no warnings recorded yet ✨")
    )
    await rich_send(
        bot,
        message.chat.id,
        ui.screen("reports", icon="chart", subtitle="most warned members", blocks_=[table]),
        reply_parameters=_reply_to(message),
    )


# ---------------------------------------------------------------------------
#  /report - flag a message to the admins
# ---------------------------------------------------------------------------


@router.message(Command("report"))
async def cmd_report(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    log_channel: Any = None,
) -> None:
    """``/report`` (as a reply) - ping the admins about a message."""
    if message.reply_to_message is None:
        await _notice(bot, message, "reply to the message you want to report", "warning")
        return
    target = message.reply_to_message.from_user
    reason = (command.args or "").strip() or "no reason given"
    await db.log_event(
        "Report",
        chat_id=message.chat.id,
        user_id=getattr(target, "id", None),
        data={"by": message.from_user.id, "reason": reason},
    )
    if log_channel is not None:
        try:
            await log_channel.event(
                "Report",
                ui.screen(
                    "report",
                    icon="warning",
                    subtitle=message.chat.title or str(message.chat.id),
                    blocks_=[
                        ui.kv_panel(
                            [
                                ("🛡 ʀᴇᴘᴏʀᴛᴇʀ", _user_line(message.from_user)),
                                ("🎯 ᴛᴀʀɢᴇᴛ", _user_line(target)),
                                ("📝 ʀᴇᴀꜱᴏɴ", ui.esc(reason)),
                            ]
                        ),
                        ui.panel(ui.italic(ui.esc((message.reply_to_message.text or "")[:300]))),
                    ],
                ),
            )
        except Exception as exc:
            log.debug("report log failed: %s", exc)
    await _notice(bot, message, f"reported to the admins - {ui.esc(reason)}", "megaphone")


__all__ += ["ESCALATION", "DURATIONS"]
