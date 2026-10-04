# GOHAN 🍚

**A rich channel publisher Telegram bot** — composes posts from HTML/Markdown,
renders them as native **Bot API 10.x Rich Messages** (`sendRichMessage`),
streams them live with `sendRichMessageDraft`, schedules delivery, and backs
it all with an optional **Kurigram** (MTProto) engine for the things the Bot
API can't do.

Built with the *new* stack you asked for:

| Engine | Version | Role |
|---|---|---|
| **aiogram** | `3.31.0` | Bot API **10.3** — rich messages, drafts, effects, the lot |
| **Kurigram** | `2.2.26` | MTProto extras — channel history, richer chat info, publish fallback |
| *(rich send)* | — | we use the official `sendRichMessage` / `InputRichMessage` methods on **both** engines |

> **Kurigram import gotcha:** Kurigram is a drop-in Pyrogram replacement and
> installs *as the `pyrogram` package* — `from pyrogram import Client` **is**
> Kurigram. `import kurigram` will fail; that's expected.

---

## Feature tour — new Bot API, actually used

| Bot API | Since | Where in GOHAN |
|---|---|---|
| `sendRichMessage` + `InputRichMessage` (html/markdown/**blocks**) | 10.1 | every preview, publish, `/start`, `/help`, `/status`, `/chinfo`, `/richdemo` |
| Rich blocks: headings, tables (`is_compact`), details, expandable quotes, checklists, math, thinking, dividers | 10.1–10.2 | `/richdemo`, stream placeholders |
| `sendRichMessageDraft` (`can_stop`, `keep_on_stop`) | 10.1/10.3 | 🧪 Stream button / `/stream` — progressive rendering with client stop button |
| `stopped_message_generation` update (`MessageGenerationStopped`) | 10.3 | cancels the streaming task the moment you tap stop |
| `editMessageText(rich_message=…)` | 10.1 | 🔀 Mode button re-renders the preview **in place** |
| Rich message buttons (`RichBlockButtons` + `RichMessageButton`) | 10.3 | inside `/richdemo` (URL, callback **and disabled** variants) |
| `InlineKeyboardButton.disabled` (`DisabledButton`) | 10.3 | demo keyboard + rich demo |
| `message_effect_id` | 7.x+ | ✨ Effect toggle, `/effect <id>` |
| `allow_paid_broadcast` | 7.3+ | 💰 Paid toggle (0.1★/message) |
| `protect_content` / `disable_notification` | 5.x+ | 🔒 / 🔇 composer toggles |
| `link_preview_options` | 6.6+ | 🔗 toggle (applies to plain-text fallback sends) |
| `getChatMemberCount` (replaces deprecated plural name) | 9.x | `/channels`, `/addchannel`, `/chinfo` |
| Kurigram `Client.send_rich_message` | MTProto | last-resort publish engine in the send ladder |
| Kurigram `get_chat_history` | MTProto | `/latest` — read the channel's last post (impossible via Bot API) |

---

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
#   BOT_TOKEN=...        ← from @BotFather (required)
#   OWNER_IDS=123456789  ← your user id (recommended)
#   API_ID/API_HASH      ← optional, enables the Kurigram engine

.venv/bin/python bot.py
```

No token yet? The offline smoke test runs without one:

```bash
.venv/bin/python bot.py --selftest          # builders, storage, imports
.venv/bin/python tests/integration.py       # full dispatch against a mock Bot API
```

---

## Using the composer

1. **Type (or paste) your post** in the private chat — HTML or Markdown, it
   auto-detects and immediately renders a live Rich Message preview with the
   control panel attached.
2. From the panel:
   - **📤 Send now** → pick a channel → published via `sendRichMessage`
   - **📅 Schedule** → pick a channel → +5m / +30m / +2h / +1d
   - **🧪 Stream** → the draft animates in with `sendRichMessageDraft`
     (stop button appears in your client — pressing it fires the 10.3
     `stopped_message_generation` update)
   - **🔀 Mode** → cycles `auto → html → markdown`, re-rendering the preview
     in place through `editMessageText(rich_message=…)`
   - **🔘 Buttons** → add URL buttons as `Label | https://…`
   - **🔒 🔇 💰 ✨ 🔗** → per-post send flags
3. The publish pipeline is a ladder — it *cannot* silently drop a post:
   `sendRichMessage` → plain HTML → classic Markdown → raw text →
   Kurigram MTProto `send_rich_message`.

### Commands

| Command | What it does |
|---|---|
| `/new` | start a fresh draft |
| `/preview` · `/stream` | render / live-stream the current draft |
| `/schedule 90` | schedule the draft for 90 minutes from now |
| `/queue` | list + cancel pending posts |
| `/effect <id>` / `/effect off` | message effect for the ✨ toggle |
| `/addchannel @chan` | register a channel (bot must be admin with **Post messages**) |
| `/channels` · `/chinfo [@chan]` | manage / inspect channels |
| `/latest [@chan]` | last channel post — needs the Kurigram engine |
| `/richdemo` | the 10.1–10.3 feature zoo (tables, math, checklists, rich buttons…) |
| `/status` | engines, database, queue |
| `/cancel` | discard the draft |

---

## Architecture

```
bot.py                     entrypoint (+ --selftest)
gohan/
├── config.py              .env → Config
├── rich.py                richsend: InputRichMessage builders, cards, keyboards
├── storage.py             aiosqlite: drafts · channels · scheduled posts
├── publisher.py           send ladder (Bot API → MTProto fallback)
├── stream.py              sendRichMessageDraft streaming + stop handling
├── scheduler.py           background queue (5s tick, owner notifications)
├── mtproto.py             Kurigram engine (optional)
├── auth.py                OWNER_IDS / private-chat filters
└── handlers/
    ├── common.py          /start /help /richdemo /status
    ├── compose.py         composer commands  + plain-text intake (last)
    ├── channels.py        /channels /addchannel /chinfo /latest
    └── actions.py         control-panel callbacks + generation-stop
tests/
└── integration.py         16 scenarios against a local mock Bot API
```

Router order matters: the plain-text intake is included **last** so it can
never swallow slash commands owned by other routers.

### Optional Kurigram (MTProto) setup

Set in `.env`:

```env
API_ID=12345
API_HASH=0123456789abcdef...
SESSION_STRING=           # optional user session; empty → bot logs in as itself
```

- **as the bot** (no session) — extras limited to chats the bot is in;
- **as your user account** (session string) — full power: channel history for
  `/latest`, publish fallback where only *you* are admin.

### Storage

Single SQLite file (`DATABASE=gohan.db`): drafts per user, registered
channels, and the schedule queue (`pending → done | failed | cancelled`).

---

## Tests

```bash
.venv/bin/python bot.py --selftest        # 9 offline checks
.venv/bin/python tests/integration.py     # 16 end-to-end scenarios
```

The integration suite spins up a local mock of `api.telegram.org` and pushes
real `Update` objects through the real dispatcher — asserting the exact Bot
API methods fired (including `sendRichMessageDraft` + the stop-button flow).
