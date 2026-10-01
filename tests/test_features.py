"""Tests for the group/owner control surfaces, filters and the new modules.

These are all offline: no Telegram, no network, no tokens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gohan.anime import _clean, _stars, anime_card
from gohan.config import Settings
from gohan.content import HELP_SECTIONS, help_screen
from gohan.handlers.filters import OWNER_ONLY_KEY, _panel as filters_panel
from gohan.handlers.owner import OWNER_PAGES, _render, home_body, home_keyboard
from gohan.rich import keys, ui
from gohan.storage import Database

# ---------------------------------------------------------------------------
#  storage: filters, chats, users
# ---------------------------------------------------------------------------


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.sqlite3")


async def _with_db(tmp_path: Path):
    db = _db(tmp_path)
    await db.connect()
    return db


def test_filters_crud(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await _with_db(tmp_path)
        try:
            await db.save_filter(1, "Hello", "hi there", created_by=7)
            await db.save_filter(1, "bye", "see you")
            assert await db.count_filters(1) == 2

            rows = [row["trigger"] for row in await db.list_filters(1)]
            assert rows == ["bye", "hello"]  # ordered by uses, then trigger

            await db.bump_filter_uses(1, "hello")
            row = await db.get_filter(1, "#HELLO")  # lookup normalises the trigger
            assert row is not None and row["uses"] == 1

            await db.save_filter(1, "hello", "hi again")  # replace, not duplicate
            assert await db.count_filters(1) == 2
            assert (await db.get_filter(1, "hello"))["reply"] == "hi again"

            assert await db.delete_filter(1, "bye") is True
            assert await db.delete_filter(1, "bye") is False
            assert await db.clear_filters(1) == 1
            assert await db.count_filters(1) == 0
        finally:
            await db.close()

    asyncio.run(scenario())


def test_chat_tracking(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await _with_db(tmp_path)
        try:
            await db.upsert_chat(-100123, title="Testers", type="supergroup", added_by=5)
            await db.upsert_chat(-100124, title="Other", type="group", added_by=6)
            assert await db.count_chats() == 2

            await db.set_chat_active(-100124, False)
            active = [row["chat_id"] for row in await db.recent_chats(limit=5)]
            assert active == [-100123]

            every = [row["chat_id"] for row in await db.recent_chats(limit=5, active_only=False)]
            assert set(every) == {-100123, -100124}

            sizes = await db.table_sizes()
            assert "filters" in sizes
        finally:
            await db.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
#  inline keyboards: colours and icons on every button
# ---------------------------------------------------------------------------


def test_every_inline_button_is_coloured() -> None:
    markup = keys.grid(
        [
            ("Save", "cfg:save"),          # success - keyword
            ("Delete", "cfg:del"),         # danger  - keyword
            ("Docs", "https://example.com"),  # link - url target
            ("Stats", "cfg:stats"),        # primary - default
        ],
        per_row=2,
        icons=["check", "trash", "book", "chart"],
    )
    flat = [button for row in markup.inline_keyboard for button in row]
    assert len(flat) == 4
    assert all(button.style for button in flat), "no button may be left unstyled"
    assert {button.style for button in flat} == {"success", "danger", "link", "primary"}
    # most icons resolve to a pack id; a character we do not own simply has none
    assert sum(bool(button.icon_custom_emoji_id) for button in flat) >= 3


def test_confirm_pair_uses_the_new_styles() -> None:
    markup = keys.confirm("own:restart:yes")
    yes, no = markup.inline_keyboard[0][0], markup.inline_keyboard[1][0]
    assert yes.style == "success" and yes.callback_data == "own:restart:yes"
    assert no.style == "danger" and no.callback_data == "ui:cancel"
    assert yes.icon_custom_emoji_id and no.icon_custom_emoji_id


def test_with_back_appends_navigation() -> None:
    markup = keys.with_back([[keys.button("Stats", callback="own:stats")]], back="own:home", close="own:close")
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert labels == ["Stats", "Back", "Close"]
    styles = {button.text: button.style for row in markup.inline_keyboard for button in row}
    assert styles["Back"] == "primary" and styles["Close"] == "danger"


def test_unknown_icon_still_yields_a_button() -> None:
    markup = keys.keyboard([keys.button("Mystery", callback="x:y", style="primary", icon="not_a_real_name")])
    assert markup.inline_keyboard[0][0].icon_custom_emoji_id is None
    assert markup.inline_keyboard[0][0].style == "primary"


# ---------------------------------------------------------------------------
#  tables: raw markup vs escaping
# ---------------------------------------------------------------------------


def test_table_escapes_by_default_and_allows_raw_markup() -> None:
    escaped = ui.table(["ᴀ", "ʙ"], [("<code>cmd</code>", "<script>")])
    assert "&lt;code&gt;" in escaped and "&lt;script&gt;" in escaped

    raw = ui.table(["ᴀ", "ʙ"], [("<code>cmd</code>", ui.esc("<script>"))], raw=True)
    assert "<code>cmd</code>" in raw and "&lt;script&gt;" in raw


def test_help_screen_keeps_command_code_tags() -> None:
    for section in HELP_SECTIONS:
        screen = help_screen(section)
        assert "<code>/" in screen, section
        assert "&lt;code&gt;" not in screen, section


# ---------------------------------------------------------------------------
#  group panels
# ---------------------------------------------------------------------------


def test_filter_panel_lists_and_hides() -> None:
    empty = filters_panel("Testers", [])
    assert "ɴᴏ ꜰɪʟᴛᴇʀꜱ ʏᴇᴛ" in empty
    assert ui.caps("Testers") in empty  # the chat title, in small caps

    rows = [
        {"trigger": "hello", "kind": "text", "uses": 3, "reply": "hi there"},
        {"trigger": "rules", "kind": "photo", "uses": 0, "reply": ""},
    ]
    panel = filters_panel("Testers", rows)
    assert "#hello" in panel and "#rules" in panel
    assert "<code>2</code>" in panel or "2" in panel


def test_owner_pages_render(tmp_path: Path) -> None:
    async def scenario() -> None:
        db = await _with_db(tmp_path)
        try:
            settings = Settings(bot_name="GOHAN")
            await db.upsert_chat(-1001, title="Group", type="supergroup")
            await db.save_filter(-1001, "hi", "hello")
            home = await home_body(db, settings)
            assert ui.caps("GOHAN") in home

            for page in OWNER_PAGES:
                body = await _render(db, settings, page)
                assert body, page
                assert "<" in body  # real rich HTML, not a bare string

            markup = home_keyboard(maintenance=False)
            flat = [button for row in markup.inline_keyboard for button in row]
            assert len(flat) >= 12
            assert all(button.style for button in flat)
            callbacks = {button.callback_data for button in flat if button.callback_data}
            assert {"own:stats", "own:groups", "own:restart:ask", "own:stop:ask"} <= callbacks
        finally:
            await db.close()

    asyncio.run(scenario())


def test_owner_only_filter_mode_key() -> None:
    assert OWNER_ONLY_KEY == "filters_owner_only"


# ---------------------------------------------------------------------------
#  anime helpers (pure functions - no HTTP)
# ---------------------------------------------------------------------------


def test_stars() -> None:
    assert _stars(None) == "—"
    assert _stars(100).startswith("★★★★★")
    assert _stars(40).startswith("★★☆☆☆")


def test_anime_card_renders_key_fields() -> None:
    media = {
        "title": {"english": "Frieren", "native": "葬送のフリーレン"},
        "description": "A <i>mage</i> and her journey.<br>Second line.",
        "episodes": 28,
        "duration": 24,
        "averageScore": 92,
        "favourites": 120000,
        "status": "FINISHED",
        "seasonYear": 2023,
        "format": "TV",
        "genres": ["Adventure", "Drama"],
        "studios": {"nodes": [{"name": "Madhouse"}]},
        "siteUrl": "https://anilist.co/anime/154587",
        "nextAiringEpisode": {"episode": 12, "timeUntilAiring": 7200},
    }
    card = anime_card(media)
    assert ui.caps("Frieren") in card
    assert "Madhouse" in card
    assert "<blockquote>" in card  # the stat panel is a blockquote, per house style
    assert "<h1>" in card and "<footer>" in card
    assert "★★★★★" in card or "★★★★☆" in card
    assert _clean(media["description"]).startswith("A mage and her journey.")


# ---------------------------------------------------------------------------
#  content + settings
# ---------------------------------------------------------------------------


def test_new_help_sections_exist() -> None:
    for section in ("notes", "filters", "anime", "music", "owner"):
        assert section in HELP_SECTIONS


def test_settings_have_the_new_ops_keys() -> None:
    settings = Settings()
    assert settings.drop_pending_updates is True
    assert settings.watchdog_interval >= 30
    assert settings.log_channel_id == 0
    assert settings.llm_provider == "off"  # the bot runs fine with no API key


@pytest.mark.parametrize("mode", ["auto", "user", "bot", "off"])
def test_mtproto_mode_values(mode: str) -> None:
    settings = Settings(mtproto_mode=mode)  # type: ignore[arg-type]
    assert settings.mtproto_mode == mode
