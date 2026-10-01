"""Games: quiz, number guess, word chain, dice duels, slots - with leaderboards.

Design rules that keep group games fun instead of annoying:

* **one game per chat at a time** - a new game refuses to start while another is
  running, so two people cannot fight over the same chat,
* **games expire** (60-120 s), so a group is never stuck with an abandoned board,
* **scores are per chat** in the ``scores`` table, so ``/top`` is *this* group's
  leaderboard,
* **Telegram's own dice** are used for luck games (``/dice``, ``/slot``) - they
  are provably fair because the server rolls them.

Every result screen is a rich message with a table, and progress is shown with
``ChatActionSender`` so the bot feels alive while it "thinks".
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from aiogram.utils.chat_action import ChatActionSender

from .content import QUIZ_QUESTIONS
from .filters import IsGroup, RateLimit
from .logging_setup import get_logger
from .rich import ui
from .rich.sender import rich_edit, rich_send
from .storage import Database

log = get_logger("games")

router = Router(name="games")

__all__ = ["GAME_NAMES", "GameManager", "router"]

GAME_NAMES = {
    "quiz": "Quiz",
    "guess": "Number guess",
    "chain": "Word chain",
    "dice": "Dice duel",
    "slot": "Slots",
}

#: Give up on a game after this many seconds without activity.
GAME_TIMEOUT = 120.0
QUIZ_SECONDS = 20

_WORDS = (
    "apple", "banana", "cherry", "dragon", "ember", "forest", "galaxy", "harbor",
    "island", "jungle", "kettle", "lantern", "mountain", "nebula", "ocean",
    "pirate", "quartz", "rocket", "safari", "tiger", "umbrella", "violet",
    "whale", "xylophone", "yonder", "zebra", "amber", "biscuit", "cactus",
    "dolphin", "engine", "falcon", "garden", "hammer", "igloo", "jacket",
    "kitten", "lemon", "marble", "needle", "orange", "pencil", "rabbit",
    "silver", "tunnel", "urban", "velvet", "winter", "yogurt", "zigzag",
)
_WORD_SET = frozenset(_WORDS)


@dataclass
class QuizRound:
    """One in-flight quiz question."""

    question: str
    options: tuple[str, ...]
    answer: int
    asked_by: int
    message_id: int | None = None
    answered: dict[int, int] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.started) > QUIZ_SECONDS


@dataclass
class GuessRound:
    """Number-guessing game state."""

    secret: int
    high: int
    low: int
    attempts: dict[int, int] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    max_attempts: int = 12

    @property
    def total_attempts(self) -> int:
        return sum(self.attempts.values())


@dataclass
class ChainRound:
    """Word-chain state."""

    last_word: str
    used: set[str] = field(default_factory=set)
    scores: dict[int, int] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    turns: int = 0


class GameManager:
    """One active game per chat, all in memory.

    Nothing here needs to survive a restart: an interrupted game is simply gone,
    and the score it would have produced was already written to the database at
    the moment it was decided.
    """

    def __init__(self) -> None:
        self.quizzes: dict[int, QuizRound] = {}
        self.guesses: dict[int, GuessRound] = {}
        self.chains: dict[int, ChainRound] = {}

    # -- lifecycle -----------------------------------------------------------

    def busy(self, chat_id: int) -> str | None:
        """Name of the game already running in this chat, if any."""
        now = time.monotonic()
        for name, table in (("quiz", self.quizzes), ("guess", self.guesses), ("chain", self.chains)):
            entry = table.get(chat_id)
            if entry is not None and (now - entry.started) > GAME_TIMEOUT:
                table.pop(chat_id, None)
                continue
            if entry is not None:
                return name
        return None

    def clear(self, chat_id: int) -> None:
        self.quizzes.pop(chat_id, None)
        self.guesses.pop(chat_id, None)
        self.chains.pop(chat_id, None)

    def reap(self) -> int:
        """Drop abandoned games (called by the watchdog)."""
        now = time.monotonic()
        dropped = 0
        for table in (self.quizzes, self.guesses, self.chains):
            for chat_id in [k for k, v in table.items() if now - v.started > GAME_TIMEOUT]:
                table.pop(chat_id, None)
                dropped += 1
        return dropped

    def summary(self) -> dict[str, int]:
        return {
            "quiz": len(self.quizzes),
            "guess": len(self.guesses),
            "chain": len(self.chains),
        }


manager = GameManager()


# ---------------------------------------------------------------------------
#  helpers
# ---------------------------------------------------------------------------


def _name(user: Any) -> str:
    return ui.esc(" ".join(filter(None, (user.first_name, user.last_name))) or str(user.id))


async def _score(
    db: Database, chat_id: int, game: str, user: Any, points: int, *, meta: dict[str, Any] | None = None
) -> None:
    try:
        await db.add_score(
            chat_id, game, user.id, points, user_name=user.full_name or str(user.id), meta=meta
        )
        await db.log_event(game, chat_id=chat_id, user_id=user.id, data={"score": points})
    except Exception as exc:
        log.debug("score write failed: %s", exc)


async def _refuse_if_busy(message: Message, bot: Bot) -> bool:
    running = manager.busy(message.chat.id)
    if running:
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(ui.no(f"a game of <b>{GAME_NAMES.get(running, running)}</b> is already running here"), icon="warning"),
            reply_parameters=__import__("aiogram.types", fromlist=["ReplyParameters"]).ReplyParameters(
                message_id=message.message_id
            ),
        )
        return True
    return False


def _reply_to(message: Message):
    from aiogram.types import ReplyParameters

    return ReplyParameters(message_id=message.message_id)


# ---------------------------------------------------------------------------
#  /games hub
# ---------------------------------------------------------------------------


@router.message(Command(commands=["games", "play"]), IsGroup())
async def cmd_games(message: Message, bot: Bot, db: Database) -> None:
    """The games hub."""
    stats = await db.game_stats(message.chat.id)
    table = (
        ui.table(["ɢᴀᴍᴇ", "ᴘʟᴀʏꜱ"], [[GAME_NAMES.get(g, g), str(n)] for g, n in stats])
        if stats
        else ui.italic("no games played here yet")
    )
    running = manager.busy(message.chat.id)
    await rich_send(
        bot,
        message.chat.id,
        ui.screen(
            "games",
            icon="game",
            subtitle=f"{message.chat.title or 'this chat'} · scores are per chat",
            blocks_=[
                ui.table(
                    ["ɢᴀᴍᴇ", "ʜᴏᴡ ɪᴛ ᴡᴏʀᴋꜱ"],
                    [
                        ("/quiz", "multiple choice · 20 s"),
                        ("/guess", "find my number · 1-100"),
                        ("/chain", "word chain · last letter"),
                        ("/dice [@user]", "roll against someone"),
                        ("/slot", "spin the machine"),
                        ("/top", "leaderboard"),
                    ],
                ),
                table,
                ui.panel(ui.italic(f"running now: <b>{GAME_NAMES.get(running, running)}</b>") if running else ui.italic("nothing running")),
            ],
        )
        + "\n\n"
        + ui.action_bar(
            [
                ("Quiz", "game:quiz"),
                ("Guess", "game:guess"),
                ("Chain", "game:chain"),
                ("Dice", "game:dice"),
                ("Slots", "game:slot"),
                ("Leaderboard", "game:top"),
            ],
            per_row=3,
        ),
    )


@router.callback_query(F.data.startswith("game:"))
async def on_game_button(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    """Buttons on the hub start the same games as the commands."""
    action = (callback.data or "").split(":", 1)[-1]
    if callback.message is None:
        await callback.answer()
        return
    chat_id = callback.message.chat.id
    if action == "top":
        await _send_leaderboard(bot, db, chat_id)
        await callback.answer()
        return
    if manager.busy(chat_id):
        await callback.answer("a game is already running here", show_alert=True)
        return
    await callback.answer(f"starting {GAME_NAMES.get(action, action)}…")
    if action == "quiz":
        await _start_quiz(bot, chat_id, callback.from_user)
    elif action == "guess":
        await _start_guess(bot, chat_id, callback.from_user)
    elif action == "chain":
        await _start_chain(bot, chat_id, callback.from_user)
    elif action == "dice":
        await _roll_dice(bot, db, chat_id, callback.from_user, None)
    elif action == "slot":
        await _spin_slot(bot, db, chat_id, callback.from_user)


# ---------------------------------------------------------------------------
#  quiz
# ---------------------------------------------------------------------------


@router.message(Command("quiz"), IsGroup())
async def cmd_quiz(message: Message, bot: Bot) -> None:
    """Start a multiple-choice question."""
    if await _refuse_if_busy(message, bot):
        return
    await _start_quiz(bot, message.chat.id, message.from_user)


async def _start_quiz(bot: Bot, chat_id: int, user: Any) -> None:
    question, options, answer = random.choice(QUIZ_QUESTIONS)
    round_ = QuizRound(question=question, options=options, answer=answer, asked_by=user.id)
    manager.quizzes[chat_id] = round_

    body = ui.screen(
        "quiz",
        icon="question",
        subtitle="answer within 20 seconds",
        blocks_=[
            ui.panel(f"<b>{ui.esc(question)}</b>", icon="bulb"),
            ui.italic(f"asked by {_name(user)}"),
        ],
    )
    letters = ("A", "B", "C", "D", "E", "F")
    keyboard = ui.actions(
        *[
            [ui.button(f"{letters[i]}. {opt}", callback=f"qz:{i}", style="primary")]
            for i, opt in enumerate(options)
        ]
    )
    message = await rich_send(bot, chat_id, body + "\n\n" + keyboard)
    if message is not None:
        round_.message_id = message.message_id

    async def expiry() -> None:
        await asyncio.sleep(QUIZ_SECONDS)
        current = manager.quizzes.get(chat_id)
        if current is not round_:
            return
        manager.quizzes.pop(chat_id, None)
        await rich_send(
            bot,
            chat_id,
            ui.panel(
                ui.no(f"time is up - the answer was <b>{ui.esc(options[answer])}</b>"),
                icon="clock",
            ),
        )

    asyncio.get_running_loop().create_task(expiry())


@router.callback_query(F.data.startswith("qz:"))
async def on_quiz_answer(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    """Score a quiz answer."""
    if callback.message is None:
        await callback.answer()
        return
    chat_id = callback.message.chat.id
    round_ = manager.quizzes.get(chat_id)
    if round_ is None:
        await callback.answer("this round is over", show_alert=True)
        return
    if callback.from_user.id in round_.answered:
        await callback.answer("you already answered", show_alert=True)
        return

    try:
        choice = int((callback.data or "").split(":", 1)[-1])
    except ValueError:
        await callback.answer()
        return
    round_.answered[callback.from_user.id] = choice

    if choice != round_.answer:
        await callback.answer("not quite ❌", show_alert=False)
        await rich_send(
            bot,
            chat_id,
            ui.panel(ui.no(f"{_name(callback.from_user)} guessed <b>{ui.esc(round_.options[choice])}</b> - no"), icon="cross"),
        )
        return

    # correct: score by speed (fewer earlier answers = more points)
    points = max(3, 12 - 3 * len(round_.answered))
    manager.quizzes.pop(chat_id, None)
    await _score(db, chat_id, "quiz", callback.from_user, points)
    best = await db.user_best(chat_id, "quiz", callback.from_user.id)
    await callback.answer("correct! 🎉")
    await rich_send(
        bot,
        chat_id,
        ui.screen(
            "correct!",
            icon="trophy",
            subtitle=_name(callback.from_user),
            blocks_=[
                ui.kv_panel(
                    [
                        ("✅ ᴀɴꜱᴡᴇʀ", ui.esc(round_.options[round_.answer])),
                        ("⭐ ᴘᴏɪɴᴛꜱ", f"+{points}"),
                        ("🏆 ʙᴇꜱᴛ ʜᴇʀᴇ", str(best)),
                    ],
                    icon="check",
                ),
                ui.actions([ui.button("Another", callback="game:quiz", style="success", icon="refresh")]),
            ],
        ),
    )


# ---------------------------------------------------------------------------
#  number guessing
# ---------------------------------------------------------------------------


@router.message(Command("guess"), IsGroup())
async def cmd_guess(message: Message, bot: Bot) -> None:
    """Start a number-guessing game (1-100)."""
    if await _refuse_if_busy(message, bot):
        return
    await _start_guess(bot, message.chat.id, message.from_user)


async def _start_guess(bot: Bot, chat_id: int, user: Any) -> None:
    secret = random.randint(1, 100)
    manager.guesses[chat_id] = GuessRound(secret=secret, low=1, high=100)
    await rich_send(
        bot,
        chat_id,
        ui.screen(
            "number guess",
            icon="target",
            subtitle="1 - 100 · first correct guess wins",
            blocks_=[
                ui.panel(
                    ui.kv("ᴛʀɪᴇꜱ ʟᴇꜰᴛ", "12 ᴛᴏᴛᴀʟ"),
                    ui.kv("ʜᴏᴡ", "ᴊᴜꜱᴛ ꜱᴇɴᴅ ᴀ ɴᴜᴍʙᴇʀ"),
                ),
                ui.italic(f"started by {_name(user)}"),
            ],
        ),
    )


class GameRunning(BaseFilter):
    """Match typed answers only while a game is actually waiting for one.

    Without this, ``^\\d{1,3}$`` and ``^[A-Za-z]{2,20}$`` would match ordinary
    messages, consume the update, and stop the filter engine and the guardian
    from ever seeing them.
    """

    def __init__(self, game: str) -> None:
        self.game = game

    async def __call__(self, message: Message, games: GameManager | None = None, **_: Any) -> bool:
        if games is None or message.from_user is None:
            return False
        if message.from_user.is_bot:
            return False
        return games.busy(message.chat.id) == self.game


@router.message(GameRunning("guess"), F.text.regexp(r"^\d{1,3}$").as_("guess"), IsGroup())
async def on_guess_attempt(message: Message, bot: Bot, db: Database, guess: Any = None) -> None:
    """Handle a bare number while a guessing game is running."""
    chat_id = message.chat.id
    round_ = manager.guesses.get(chat_id)
    if round_ is None:
        return  # not our game; other handlers may care
    try:
        value = int((guess.group(0) if guess else message.text) or 0)
    except (TypeError, ValueError):
        return
    if not 1 <= value <= 100:
        return
    if round_.total_attempts >= round_.max_attempts:
        manager.guesses.pop(chat_id, None)
        await rich_send(bot, chat_id, ui.panel(ui.no(f"out of tries - it was <b>{round_.secret}</b>"), icon="clock"))
        return

    round_.attempts[message.from_user.id] = round_.attempts.get(message.from_user.id, 0) + 1
    if value < round_.secret:
        round_.low = max(round_.low, value + 1)
        await message.reply(f"⬆️ higher than {value}")
        return
    if value > round_.secret:
        round_.high = min(round_.high, value - 1)
        await message.reply(f"⬇️ lower than {value}")
        return

    manager.guesses.pop(chat_id, None)
    tries = round_.attempts[message.from_user.id]
    points = max(2, 15 - tries * 2)
    await _score(db, chat_id, "guess", message.from_user, points)
    await rich_send(
        bot,
        chat_id,
        ui.screen(
            "found it!",
            icon="trophy",
            subtitle=_name(message.from_user),
            blocks_=[
                ui.kv_panel(
                    [
                        ("🎯 ɴᴜᴍʙᴇʀ", str(round_.secret)),
                        ("🔢 ʏᴏᴜʀ ᴛʀɪᴇꜱ", str(tries)),
                        ("⭐ ᴘᴏɪɴᴛꜱ", f"+{points}"),
                    ],
                    icon="check",
                ),
                ui.actions([ui.button("Play again", callback="game:guess", style="success", icon="refresh")]),
            ],
        ),
        reply_parameters=_reply_to(message),
    )


# ---------------------------------------------------------------------------
#  word chain
# ---------------------------------------------------------------------------


@router.message(Command("chain"), IsGroup())
async def cmd_chain(message: Message, bot: Bot) -> None:
    """Start a word chain."""
    if await _refuse_if_busy(message, bot):
        return
    await _start_chain(bot, message.chat.id, message.from_user)


async def _start_chain(bot: Bot, chat_id: int, user: Any) -> None:
    start = random.choice(_WORDS)
    round_ = ChainRound(last_word=start, used={start})
    round_.scores[user.id] = 1
    manager.chains[chat_id] = round_
    await rich_send(
        bot,
        chat_id,
        ui.screen(
            "word chain",
            icon="link",
            subtitle="each word must start with the last letter of the previous one",
            blocks_=[
                ui.panel(ui.kv("ꜱᴛᴀʀᴛ ᴡᴏʀᴅ", f"<b>{start}</b> · next must start with <b>{start[-1].upper()}</b>"), icon="bulb"),
                ui.italic(f"opened by {_name(user)}"),
            ],
        ),
    )


@router.message(GameRunning("chain"), F.text.regexp(r"^[A-Za-z]{2,20}$").as_("word"), IsGroup())
async def on_chain_word(message: Message, bot: Bot, db: Database, word: Any = None) -> None:
    """Validate a chain word."""
    chat_id = message.chat.id
    round_ = manager.chains.get(chat_id)
    if round_ is None:
        return
    candidate = ((word.group(0) if word else message.text) or "").lower()
    if candidate in round_.used:
        await message.reply("🔁 already used - try another word")
        return
    if candidate[0] != round_.last_word[-1]:
        await message.reply(f"❌ must start with <b>{round_.last_word[-1].upper()}</b>")
        return

    round_.used.add(candidate)
    round_.last_word = candidate
    round_.turns += 1
    round_.scores[message.from_user.id] = round_.scores.get(message.from_user.id, 0) + 1
    streak = round_.scores[message.from_user.id]

    if round_.turns >= 15:
        manager.chains.pop(chat_id, None)
        for uid, score in sorted(round_.scores.items(), key=lambda kv: kv[1], reverse=True)[:3]:
            await _score(db, chat_id, "chain", _FakeUser(uid, str(uid)), score * 3)
        rows = sorted(round_.scores.items(), key=lambda kv: kv[1], reverse=True)[:10]
        await rich_send(
            bot,
            chat_id,
            ui.scoreboard(
                "chain finished",
                ["ᴜꜱᴇʀ", "ᴘᴏɪɴᴛꜱ"],
                [[f"<code>{uid}</code>", str(score)] for uid, score in rows],
                icon="trophy",
                note="15 turns - nice run",
            ),
        )
        return

    await message.reply(
        f"✅ <b>{candidate}</b> · streak {streak} · next starts with <b>{candidate[-1].upper()}</b>"
    )


class _FakeUser:
    """Minimal stand-in so chain scores can be written without a User object."""

    __slots__ = ("id", "full_name", "first_name", "last_name", "username", "is_bot")

    def __init__(self, user_id: int, name: str) -> None:
        self.id = user_id
        self.full_name = name
        self.first_name = name
        self.last_name = None
        self.username = None
        self.is_bot = False


# ---------------------------------------------------------------------------
#  dice + slots
# ---------------------------------------------------------------------------


@router.message(Command("dice"), IsGroup())
async def cmd_dice(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """``/dice [@user]`` - roll a real Telegram die against someone."""
    opponent = message.reply_to_message.from_user if message.reply_to_message else None
    await _roll_dice(bot, db, message.chat.id, message.from_user, opponent)


async def _roll_dice(bot: Bot, db: Database, chat_id: int, roller: Any, opponent: Any | None) -> None:
    async with ChatActionSender(bot=bot, chat_id=chat_id, action="typing"):
        try:
            mine = await bot.send_dice(chat_id, emoji="🎲")
            my_value = getattr(mine.dice, "value", 0) if mine.dice else 0
        except TelegramAPIError as exc:
            log.warning("send_dice failed: %s", exc)
            return
        their_value = None
        if opponent is not None:
            try:
                theirs = await bot.send_dice(chat_id, emoji="🎲")
                their_value = getattr(theirs.dice, "value", 0) if theirs.dice else 0
            except TelegramAPIError:
                their_value = None

    await _score(db, chat_id, "dice", roller, my_value)

    if their_value is None:
        await rich_send(
            bot,
            chat_id,
            ui.panel(ui.kv(f"🎲 {_name(roller)}", f"<b>{my_value}</b>"), icon="dice"),
        )
        return

    winner = _name(roller) if my_value > their_value else _name(opponent) if their_value > my_value else None
    await rich_send(
        bot,
        chat_id,
        ui.screen(
            "dice duel",
            icon="dice",
            subtitle="higher roll wins",
            blocks_=[
                ui.table(
                    ["ᴘʟᴀʏᴇʀ", "ʀᴏʟʟ"],
                    [[_name(roller), str(my_value)], [_name(opponent), str(their_value)]],
                ),
                ui.panel(
                    ui.ok(f"<b>{winner}</b> wins!") if winner else ui.italic("a draw - roll again"),
                    icon="trophy",
                ),
            ],
        ),
    )


@router.message(Command("slot"), IsGroup())
async def cmd_slot(message: Message, bot: Bot, db: Database) -> None:
    """Spin the slot machine (Telegram's own, so it is server-rolled)."""
    await _spin_slot(bot, db, message.chat.id, message.from_user)


async def _spin_slot(bot: Bot, db: Database, chat_id: int, user: Any) -> None:
    async with ChatActionSender(bot=bot, chat_id=chat_id, action="typing"):
        await asyncio.sleep(0.8)
        try:
            sent = await bot.send_dice(chat_id, emoji="🎰")
        except TelegramAPIError as exc:
            log.warning("slot failed: %s", exc)
            return
    value = getattr(sent.dice, "value", 0) if sent.dice else 0
    # 1, 22, 43, 64 are the jackpot values for the slot machine emoji.
    if value in (1, 22, 43, 64):
        verdict = ui.ok("<b>JACKPOT!</b> that is one of the rare rolls 🎉")
        points = 50
    elif value >= 40:
        verdict = ui.ok("nice spin ⭐")
        points = value
    elif value >= 16:
        verdict = ui.italic("middle of the road 🙂")
        points = value // 2
    else:
        verdict = ui.no("not your round 😔")
        points = value // 4

    await _score(db, chat_id, "slot", user, points)
    best = await db.user_best(chat_id, "slot", user.id)
    await rich_send(
        bot,
        chat_id,
        ui.screen(
            "slots",
            icon="dice",
            subtitle=_name(user),
            blocks_=[
                ui.kv_panel(
                    [("🎰 ʀᴏʟʟ", f"<b>{value}</b>"), ("⭐ ᴘᴏɪɴᴛꜱ", f"+{points}"), ("🏆 ʙᴇꜱᴛ", str(best))],
                    icon="trophy",
                ),
                ui.panel(verdict),
            ],
        ),
    )


# ---------------------------------------------------------------------------
#  leaderboards
# ---------------------------------------------------------------------------


@router.message(Command(commands=["top", "leaderboard"]), IsGroup())
async def cmd_top(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    """``/top [game]`` - this chat's leaderboard."""
    game = (command.args or "").strip().lower()
    await _send_leaderboard(bot, db, message.chat.id, game=game)


async def _send_leaderboard(
    bot: Bot, db: Database, chat_id: int, *, game: str = ""
) -> None:
    """Render the leaderboard for one game, or the play counts for all of them."""
    if game and game not in GAME_NAMES:
        game = ""

    if game:
        entries = await db.leaderboard(chat_id, game, limit=10)
        rows = [
            [
                str(index + 1),
                ui.esc(entry.user_name) if entry.user_name else f"<code>{entry.user_id}</code>",
                str(entry.score),
            ]
            for index, entry in enumerate(entries)
        ]
        title = f"{GAME_NAMES[game]} leaderboard"
    else:
        counts = await db.game_stats(chat_id)
        rows = [[GAME_NAMES.get(name, name), str(count)] for name, count in counts[:10]]
        title = "leaderboard"

    body = ui.screen(
        title,
        icon="trophy",
        subtitle="best score per player in this chat",
        blocks_=[
            ui.table(["#", "ᴘʟᴀʏᴇʀ", "ꜱᴄᴏʀᴇ"], rows)
            if rows
            else ui.italic("nothing recorded yet - play a game with /games"),
            ui.italic("per-game boards: /top quiz · /top slot · /top dice"),
        ],
    )
    await rich_send(bot, chat_id, body)


@router.message(Command(commands=["me", "myscore"]), IsGroup())
async def cmd_me(message: Message, bot: Bot, db: Database) -> None:
    """``/me`` - your best score in every game here."""
    games = [g for g, _ in await db.game_stats(message.chat.id)] or list(GAME_NAMES)
    rows = []
    for game in games:
        best = await db.user_best(message.chat.id, game, message.from_user.id)
        if best:
            rows.append((GAME_NAMES.get(game, game), str(best)))
    await rich_send(
        bot,
        message.chat.id,
        ui.screen(
            "your scores",
            icon="medal",
            subtitle=_name(message.from_user),
            blocks_=[
                ui.table(["ɢᴀᴍᴇ", "ʙᴇꜱᴛ"], rows) if rows else ui.italic("no scores yet - try /games"),
            ],
        ),
        reply_parameters=_reply_to(message),
    )
