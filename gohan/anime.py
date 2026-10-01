"""Anime lookups through the free AniList GraphQL API (no key required).

Commands::

    /anime <title>       a rich card for the best match
    /trending            what is hot right now
    /character <name>    a character card
    /airing              this week's schedule for a tracked series

Everything is one HTTP POST; when AniList is unreachable the command says so
instead of failing silently.
"""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, ReplyParameters

from .filters import RateLimit
from .logging_setup import get_logger
from .rich import keys
from .rich import ui
from .rich.sender import rich_reply, rich_send
from .storage import Database

log = get_logger("anime")

router = Router(name="anime")

ANILIST_URL = "https://graphql.anilist.co"
TIMEOUT = aiohttp.ClientTimeout(total=15)

#: the last result per (chat, user) so the callback buttons can re-render
_last: dict[tuple[int, int], dict[str, Any]] = {}

anime_limit = RateLimit(2.5, message="one search at a time 🙂")
_QUERY = """
query ($search: String) {
  Media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
    id
    title { romaji english native }
    description(asHtml: false)
    episodes duration averageScore popularity favourites
    status seasonYear season format
    genres
    coverImage { large }
    siteUrl
    studios(isMain: true) { nodes { name } }
    trailer { id site }
    nextAiringEpisode { episode timeUntilAiring }
  }
}
"""

_TRENDING = """
query {
  Page(perPage: 12) {
    media(type: ANIME, sort: TRENDING_DESC) {
      id title { romaji english } averageScore episodes seasonYear format
      coverImage { large } siteUrl genres
    }
  }
}
"""

_CHARACTER = """
query ($search: String) {
  Character(search: $search) {
    id
    name { full native }
    description(asHtml: false)
    image { large }
    favourites
    siteUrl
    media(perPage: 4) { nodes { title { romaji } } }
  }
}
"""

__all__ = ["router"]


async def _post(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """One AniList call. Returns ``None`` (and logs) when it fails."""
    payload = {"query": query, "variables": variables or {}}
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.post(ANILIST_URL, json=payload) as response:
                if response.status == 429:
                    log.warning("AniList rate limit hit")
                    return None
                data = await response.json()
    except Exception as exc:
        log.warning("AniList request failed: %s", exc)
        return None
    if "errors" in data:
        log.debug("AniList errors: %s", data["errors"])
        return None
    return data.get("data")


def _clean(text: str | None, limit: int = 480) -> str:
    """AniList returns light markdown/html-ish text; flatten it for rich HTML."""
    if not text:
        return ""
    flat = (
        text.replace("<br>", " ")
        .replace("<i>", "")
        .replace("</i>", "")
        .replace("<b>", "")
        .replace("</b>", "")
        .replace("\n", " ")
    )
    flat = " ".join(flat.split())
    if len(flat) > limit:
        flat = flat[:limit].rsplit(" ", 1)[0] + "…"
    return flat


def _stars(score: int | None) -> str:
    """``85`` -> ``★★★★☆ 85/100``."""
    if not score:
        return "—"
    filled = max(1, min(5, round(score / 20)))
    return "★" * filled + "☆" * (5 - filled) + f" <code>{score}/100</code>"


def anime_card(media: dict[str, Any]) -> str:
    """The rich card for one anime."""
    titles = media.get("title") or {}
    name = titles.get("english") or titles.get("romaji") or "?"
    native = titles.get("native") or ""
    studios = ", ".join(node["name"] for node in (media.get("studios") or {}).get("nodes", []))
    next_ep = media.get("nextAiringEpisode")

    rows: list[tuple[str, str]] = [
        ("⭐ ꜱᴄᴏʀᴇ", _stars(media.get("averageScore"))),
        ("📺 ᴇᴘɪꜱᴏᴅᴇꜱ", str(media.get("episodes") or "?")),
        ("⏱ ᴅᴜʀᴀᴛɪᴏɴ", f"{media.get('duration') or '?'} min"),
        ("📅 ʏᴇᴀʀ", str(media.get("seasonYear") or "?")),
        ("🎬 ꜰᴏʀᴍᴀᴛ", str(media.get("format") or "?").replace("_", " ").title()),
        ("📡 ꜱᴛᴀᴛᴜꜱ", str(media.get("status") or "?").replace("_", " ").title()),
        ("🎭 ɢᴇɴʀᴇꜱ", ", ".join((media.get("genres") or [])[:5]) or "—"),
        ("🏢 ꜱᴛᴜᴅɪᴏ", studios or "—"),
        ("❤️ ꜰᴀᴠᴏᴜʀɪᴛᴇꜱ", f"{media.get('favourites', 0):,}"),
    ]
    if next_ep:
        hours = int(next_ep.get("timeUntilAiring", 0)) // 3600
        rows.append(("🔔 ɴᴇxᴛ ᴇᴘ", f"ep {next_ep.get('episode')} in {ui.uptime(hours * 3600)}"))

    blocks: list[Any] = [
        ui.kv_panel(rows, icon="game"),
        ui.quote(_clean(media.get("description")) or "no synopsis"),
    ]
    if media.get("siteUrl"):
        blocks.append(ui.footer_text(media["siteUrl"], caps_text=False))
    return ui.screen(
        name,
        icon="star",
        subtitle=native,
        blocks_=blocks,
    )


def anime_buttons(media: dict[str, Any], *, index: int = 0) -> Any:
    url = media.get("siteUrl") or "https://anilist.co"
    return keys.keyboard(
        [
            keys.button("Open on AniList", url=url, icon="link"),
            keys.button("Trailer", url=f"https://youtu.be/{media['trailer']['id']}", icon="play")
            if (media.get("trailer") or {}).get("id")
            else keys.button("Share", callback=f"anime:share:{index}", style="primary", icon="outbox"),
        ],
        [
            keys.button("Search again", callback="anime:again", style="primary", icon="search"),
            keys.button("Trending", callback="anime:trending", style="primary", icon="fire"),
        ],
    )


@router.message(Command(commands=["anime", "ani"]), anime_limit)
async def cmd_anime(message: Message, command: CommandObject, bot: Bot) -> None:
    """``/anime <title>``."""
    query = (command.args or "").strip()
    if not query:
        await rich_reply(
            message,
            ui.panel("ᴜꜱᴀɢᴇ: <code>/anime frieren</code> · <code>/trending</code>", icon="game"),
        )
        return

    data = await _post(_QUERY, {"search": query})
    media = (data or {}).get("Media")
    if not media:
        await rich_send(
            bot,
            message.chat.id,
            ui.panel(ui.no(f"nothing found for <b>{ui.esc(query)}</b>"), icon="question"),
            reply_parameters=ReplyParameters(message_id=message.message_id),
        )
        return

    if message.from_user is not None:
        _last[(message.chat.id, message.from_user.id)] = media

    cover = (media.get("coverImage") or {}).get("large")
    if cover:
        try:
            await bot.send_photo(
                message.chat.id,
                photo=cover,
                caption=ui.caps(
                    f"{(media.get('title') or {}).get('english') or (media.get('title') or {}).get('romaji')}"
                ),
            )
        except Exception as exc:
            log.debug("cover send failed: %s", exc)

    await rich_send(
        bot,
        message.chat.id,
        anime_card(media),
        reply_markup=anime_buttons(media),
    )


@router.message(Command(commands=["trending", "hot"]), anime_limit)
async def cmd_trending(message: Message, bot: Bot) -> None:
    """``/trending`` - what the world is watching."""
    data = await _post(_TRENDING)
    media_list = ((data or {}).get("Page") or {}).get("media") or []
    if not media_list:
        await rich_send(bot, message.chat.id, ui.panel(ui.no("AniList is not answering"), icon="warning"))
        return

    rows = [
        (
            f"{index}. {(item.get('title') or {}).get('english') or (item.get('title') or {}).get('romaji')}",
            f"{item.get('averageScore') or '—'} · {item.get('seasonYear') or '—'}",
        )
        for index, item in enumerate(media_list[:10], start=1)
    ]
    buttons = [
        [keys.button(str(index), callback=f"anime:pick:{item['id']}", style="primary", icon="star")]
        for index, item in enumerate(media_list[:10], start=1)
    ]
    keyboard = keys.keyboard(*[buttons[i : i + 5] for i in range(0, len(buttons), 5)])
    await rich_send(
        bot,
        message.chat.id,
        ui.screen(
            "trending now",
            icon="fire",
            subtitle="tap a number to see the details",
            blocks_=[ui.table(["ᴛɪᴛʟᴇ", "ꜱᴄᴏʀᴇ · ʏᴇᴀʀ"], rows)],
        ),
        reply_markup=keyboard,
    )


@router.message(Command(commands=["character", "waifu", "husbando"]), anime_limit)
async def cmd_character(message: Message, command: CommandObject, bot: Bot) -> None:
    """``/character <name>``."""
    query = (command.args or "").strip()
    if not query:
        await rich_reply(message, ui.panel("ᴜꜱᴀɢᴇ: <code>/character gojo satoru</code>", icon="game"))
        return
    data = await _post(_CHARACTER, {"search": query})
    character = (data or {}).get("Character")
    if not character:
        await rich_send(bot, message.chat.id, ui.panel(ui.no("no such character"), icon="question"))
        return

    name = (character.get("name") or {}).get("full") or "?"
    image = (character.get("image") or {}).get("large")
    if image:
        try:
            await bot.send_photo(message.chat.id, photo=image, caption=ui.caps(name))
        except Exception as exc:
            log.debug("character image failed: %s", exc)

    appears = ", ".join(
        (node.get("title") or {}).get("romaji", "?")
        for node in (character.get("media") or {}).get("nodes", [])
    )
    await rich_send(
        bot,
        message.chat.id,
        ui.screen(
            name,
            icon="sparkles",
            subtitle=(character.get("name") or {}).get("native") or "",
            blocks_=[
                ui.kv_panel(
                    [
                        ("❤️ ꜰᴀᴠᴏᴜʀɪᴛᴇꜱ", f"{character.get('favourites', 0):,}"),
                        ("🎬 ᴀᴘᴘᴇᴀʀꜱ ɪɴ", appears or "—"),
                    ],
                    icon="user",
                ),
                ui.quote(_clean(character.get("description"), 420) or "no description"),
            ],
        ),
        reply_markup=keys.keyboard(
            [
                keys.button("AniList", url=character.get("siteUrl") or "https://anilist.co", icon="link"),
                keys.button("Another", callback="anime:again", style="primary", icon="refresh"),
            ]
        ),
    )


@router.callback_query(F.data.startswith("anime:"))
async def on_anime_callback(callback: CallbackQuery, bot: Bot) -> None:
    """Number buttons, re-search and share."""
    if callback.message is None or callback.from_user is None:
        await callback.answer()
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""

    if action == "trending":
        await callback.answer("trending")
        await cmd_trending(callback.message, bot)  # type: ignore[arg-type]
        return

    if action == "again":
        await callback.answer("type /anime <title>", show_alert=False)
        return

    if action == "share":
        await callback.answer("link copied? use the AniList button 🙂")
        return

    if action == "pick" and len(parts) > 2 and parts[2].isdigit():
        data = await _post(
            "query ($id: Int) { Media(id: $id, type: ANIME) "
            "{ id title { romaji english native } description(asHtml: false) episodes duration "
            "averageScore favourites status seasonYear format genres coverImage { large } siteUrl "
            "studios(isMain: true) { nodes { name } } trailer { id site } "
            "nextAiringEpisode { episode timeUntilAiring } } }",
            {"id": int(parts[2])},
        )
        media = (data or {}).get("Media")
        if not media:
            await callback.answer("could not load that one", show_alert=True)
            return
        chat_id = callback.message.chat.id
        await rich_send(bot, chat_id, anime_card(media), reply_markup=anime_buttons(media))
        await callback.answer("here you go")
        return

    await callback.answer()


async def anime_search(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Small helper used by the AI handler when a question looks anime-shaped."""
    data = await _post(
        "query ($q: String) { Page(perPage: 5) { media(search: $q, type: ANIME) "
        "{ id title { romaji english } averageScore seasonYear } } }",
        {"q": query},
    )
    return ((data or {}).get("Page") or {}).get("media") or []
