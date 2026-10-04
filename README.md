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
| 🎵 **Voice chat** | the AnvuMusic engine in Python: `/play`, queues, playlists, `/afk`, autoplay, panels with live `<tg-time>` progress |
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
* **No py-tgcalls?** The player runs on a silent backend: the queue, the cards,
  the playlists, `/afk` and the web Music page all behave normally, and every card
  says exactly what to install.

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
| **Music** | what every voice chat is playing right now - cover art, live progress, the queue, transport buttons that reach the real player, playlists with delete, who is away, who may control |
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

## Voice chat

`gohan/voice/` is a from-scratch Python port of
[AnvuMusic](https://github.com/Naman-Devio/AnvuMusic) (Go, GPL-3.0) - the same
feature set, rebuilt around aiogram 3.31 and the new Bot API pieces:

```bash
pip install "gohan[voice]"   # py-tgcalls + yt-dlp
sudo apt install ffmpeg      # or your distro's package
```

| Group | Commands |
|---|---|
| **Playback** | `/play <song/link/playlist>` (YouTube, SoundCloud, Spotify links, replies to audio), `/playlist <url>`, `/playplaylist <name>` |
| **Transport** | `/pause`, `/resume`, `/skip`, `/stop`, `/replay`, `/seek`, `/seekback`, `/position`, `/speed` |
| **Queue** | `/queue`, `/active`, `/shuffle`, `/jump`, `/remove`, `/clear`, `/loop` |
| **Sound** | `/volume`, `/vmute`, `/vunmute`, `/autoplay`, `/vsettings` |
| **Playlists** | `/createplaylist`, `/addtoplaylist`, `/removefromplaylist`, `/deleteplaylist`, `/myplaylists`, `/playlistinfo` |
| **People** | `/afk [reason]`, `/auth`, `/unauth`, `/authlist` |
| **Panel** | `/voice`, `/musichelp`, `/panel` |

**Rich everywhere.** The now-playing card carries the cover art, a table of
track/requester/queue, a progress bar and a **live** `<tg-time>` countdown; the
controls are coloured inline buttons with custom-emoji glyphs (`primary`
actions, `success` for play, `danger` for stop). Search results stream into a
`sendRichMessageDraft` preview so the chat sees "searching…" instead of a typing
indicator, and in groups the control panel is posted **ephemerally** - visible
only to whoever pressed the button, and replaced in place by the next screen.

**The engine.** `sources.py` speaks yt-dlp (search, resolve, playlists,
related, downloads, 6 h cache cleanup); `player.py` keeps one room per chat with
loop modes, history, 0.5-2× speed, 0-200 % volume, seek, autoplay from related
tracks and autoleave after an idle period; `backend.py` hides py-tgcalls behind
an alias-tolerant adapter so a version bump cannot break the join call.
Everything is database-backed: playlists, away mode and the per-chat controller
list survive restarts.

**No crash without the extra.** With no py-tgcalls, the null backend runs the
whole state machine silently, so `/queue`, the panels and the web page stay
testable - and `/voice` tells you what to install.

## Music in the browser

Same idea in the panel: every room live, with cover art, a progress bar, the
queue, and transport buttons (`Pause` / `Skip` / `Shuffle` / `Loop` / `Stop`)
that call the real player through `POST /api/music/{chat_id}/actions/{action}`.
Playlists are listed with their owners and a delete button, and two tables show
who is away and who is allowed to drive the player. With no backend attached the
page still renders demo data and labels every action as a no-op.

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
bash scripts/bootstrap.sh dev       # .venv + pytest + ruff
.venv/bin/python -m pytest -q       # the whole suite
.venv/bin/python -m gohan --check   # configuration, capabilities, router wiring

# the panel boots in a sandbox and every page is rendered - no browser needed
.venv/bin/python -m gohan.services.preview 8080 &
node scripts/check_panel.mjs
```

`scripts/check_panel.mjs` exists because `node --check` only proves a file
parses: it evaluates `app.js` against a stubbed DOM, talks to the running panel,
then renders all eight pages and fails on anything the browser would choke on.


Layout: `gohan/rich/` (messages, buttons, emoji), `gohan/guardian/` (moderation,
captcha, anti-raid, events, panel), `gohan/voice/` (the music engine),
`gohan/web/` (control panel + API), `gohan/ai/`, `gohan/mtproto/`,
`gohan/handlers/`, `gohan/services/` (keep-alive, watchdog, log channel).

## Credits

* [**AnvuMusic**](https://github.com/Naman-Devio/AnvuMusic) (Echo Team, GPL-3.0) - the
  Go bot whose voice-chat feature set, command surface and cogs this engine
  reimplements in Python. GOHAN's own code stays MIT, but if you redistribute a
  build that includes the ported voice engine, treat that part as GPL-3.0:
  keep this attribution, and read the licence before shipping it as a product.
* [**richgram**](https://github.com/Badmunda05/richgram) (MIT) - the rich-message
  helpers the sender layer falls back on.
* Videl1 - the keep-alive, watchdog and log-channel design, reimplemented on
  aiohttp + aiogram 3.
* [aiogram](https://github.com/aiogram/aiogram) 3.31 and
  [Kurigram](https://github.com/KurimuzonAkuma/pyrogram) for the Bot API and
  MTProto sides.

MIT licensed (see the note above).
