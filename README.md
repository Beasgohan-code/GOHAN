# GOHAN 🧚‍♂️

> ✨ ɪ'ᴍ **ɢᴏʜᴀɴ** — ɪ ʜᴀᴠᴇ ʟᴏᴛꜱ ᴏꜰ ꜰᴇᴀᴛᴜʀᴇꜱ ʟɪᴋᴇ ᴀɪ ᴄʜᴀᴛʙᴏᴛ, ᴀɴɪᴍᴇ, ᴍᴜꜱɪᴄ, ɴᴏᴛᴇꜱ, ꜰɪʟᴛᴇʀꜱ, ꜰᴜɴ ᴀɴᴅ ᴍᴀɴʏ ᴏᴛʜᴇʀ ᴜꜱᴇꜰᴜʟ ᴄᴏᴍᴍᴀɴᴅꜱ!
> ᴛʜɪꜱ ɪꜱ ᴍʏ ᴘʟᴀɴ ᴏꜰ ʙᴏᴛ 😺

A Telegram bot built on **aiogram 3.31** (Bot API 10.x) with **Kurigram** (MTProto)
for the things the Bot API cannot do, and **Rich Messages** everywhere: headings,
tables, blockquotes, expandable details, `<tg-emoji>` custom emoji and coloured
buttons — no plain-looking screens.

## Features

| Area | What it does |
|---|---|
| 🛡 **Guardian** | captcha, anti-raid lockdown, anti-spam, links/forwards, warnings with escalation |
| 🔮 **AI Chatbot** | `/ask` streams into a live draft, then lands as a rich message; `/ai on`, `/ai prompt`, `/summary`, `/translate` |
| 🎬 **Anime** | `/anime`, `/trending`, `/character` via the free AniList API, with artwork and buttons |
| 🎧 **Music** | `/music` search with number buttons, `/download` sends the audio (yt-dlp; Bot API 45 MB, MTProto 2 GB) |
| 📝 **Notes** | `/save`, `/notes`, and `#name` to print one |
| 🧲 **Filters** | `/filter`, `/stop`, `/stopall`, `/filters` panel; group admins manage, the owner can lock it with `/filtermode owner` |
| ✨ **Fun** | 19 actions: `/hug`, `/kiss`, `/slap`, `/dance`, … plus `/setgif` to teach custom animations |
| 🎮 **Games** | quiz, guess-the-number, word chain, real dice, slots, per-chat leaderboards |
| 👑 **Owner panel** | `/owner` — every administrative action behind a coloured button |

## Setup

```bash
git clone <this repo> && cd GOHAN
cp .env.example .env          # fill in BOT_TOKEN (and API_ID/API_HASH if you want MTProto)
bash scripts/bootstrap.sh dev # creates .venv and installs everything, tests included
.venv/bin/python -m gohan --check
```

Run it:

```bash
.venv/bin/python -m gohan            # start the bot
.venv/bin/python -m gohan --login    # create the MTProto user session (SMS code)
.venv/bin/python -m gohan --check    # validate configuration without starting
```

### Nothing is mandatory

* **No AI key?** The assistant runs on a local demo generator — the bot starts
  and answers, and `/ask` says clearly that it is in demo mode. Add
  `LLM_PROVIDER` + `LLM_API_KEY` to switch to a real model.
* **No MTProto?** Set `MTPROTO_MODE=off` (or leave the API keys blank) and
  everything still works, just without 2 GB transfers, history and cross-chat
  reputation.
* **No yt-dlp?** `/music` returns search links instead of audio files.
* **No ffmpeg?** MP3 conversion is skipped; files are sent as-is.

## Web control panel

The same port that keeps the bot awake serves a full control panel
(``WEB_DASHBOARD=true``, default)::

    http://<your-host>:8080/          the panel
    http://<your-host>:8080/status    the classic status page
    http://<your-host>:8080/health    JSON for uptime monitors

| Page | What it gives you |
|---|---|
| **Overview** | KPI cards, a 24 h activity chart, runtime health, live event feed, quick actions |
| **Moderation** | warnings per day (7/30 d), most-warned members, recent cases with a live search, busiest groups |
| **Users** | search by id, `@username` or name; filter to blocked users; open a case file per person |
| **Modules** | every switch (22 of them) grouped into protection / community / fun / system, plus per-group scope |
| **Groups** | searchable table; click a group for its modules, an **editor** for its welcome message, rules and warn limit, plus top filters and most-warned members |
| **Events** | the log channel in a browser: tag filters, search, pause, CSV export |
| **Settings** | configuration summary, broadcast composer, maintenance, danger zone |

**Case files.** Clicking a warned user - from Moderation, Users or a group drawer -
opens their history: every warning with reason, chat and age, the groups they have
been warned in, their best game scores, and the two buttons that matter
(**Block user** / **Unblock**, **Clear warnings**). Blocking sets the bot-wide
ban the middleware enforces, and if a chat id is supplied it also bans them on
Telegram.

**Bulk control.** Every per-group module card on the Modules page has a `⇄`
button that applies that switch to **every** active group at once - the thing
you actually want when a raid wave starts.

* **Live**: a server-sent-events stream pushes new events into the page; the
  dashboard falls back to polling on its own if the stream drops.
* **Control**: toggles write straight into the chat settings the bot reads, so a
  switch flipped in the browser is live in Telegram immediately.
* **Command palette**: `Ctrl/Cmd + K` jumps anywhere and runs owner actions.
* **Works offline**: no CDN, no build step, no external fonts - one CSS file and
  one JS file served by the bot itself.
* **Themes**: dark by default, light one click away, remembered per browser.

### Securing it

`WEB_AUTH` has three modes:

```ini
WEB_AUTH=off       # private network or local use
WEB_AUTH=token     # a shared secret; the bot writes one to data/.web_token
WEB_AUTH=telegram  # Mini App initData, verified with BOT_TOKEN, owners only
```

Token sessions are HMAC-signed cookies that expire after 12 h, and failed logins
are rate limited per IP (8 tries per 10 minutes).

## What the owner gets

* a **DM on every start** with a live status card and quick buttons,
* **group notifications** — added, promoted, removed — with chat id and actor,
* `/owner` control panel: stats, groups, users, filters, logs, system, watchdog,
  assistant, broadcast, backup, maintenance, **restart**, shut down,
* a **daily report**, watchdog alerts and a log channel (`LOG_CHANNEL_ID`),
* `/restart confirm` and `/shutdown confirm` (also buttons in the panel).

## Visual style

Every user-facing string follows the house style: small caps, blockquote panels,
tables with small-capped headers, headings, expandable details, `<footer>` and
custom emoji from the 600-id registry (`/emojis` browses it). Buttons — both
rich (`<tg-button>`) and inline — always carry a colour (`primary`, `success`,
`danger`, `link`) and, where the registry has one, a custom-emoji icon.

## Development

```bash
.venv/bin/python -m pytest tests/ -q     # 50 tests, no network required
.venv/bin/python -m gohan.services.preview 8080   # status page with demo data
```

Layout: `gohan/rich/` (messages, buttons, emoji), `gohan/guardian/` (moderation,
captcha, anti-raid, events, panel), `gohan/ai/`, `gohan/mtproto/`,
`gohan/handlers/`, `gohan/services/` (keep-alive, watchdog, log channel).

MIT licensed.
