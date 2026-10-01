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
