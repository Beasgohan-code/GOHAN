"""Tests for the rich-message layer, emoji registry and palette."""

from __future__ import annotations

import pytest

from gohan.rich import emoji, palette, text, ui
from gohan.rich.sender import MAX_CHARS, _open_tags, validate_html


# --------------------------------------------------------------------------- text


def test_small_caps_maps_letters_and_keeps_digits():
    assert text.small_caps("gohan 2") == "ɢᴏʜᴀɴ 2"
    assert text.small_caps("ABC") == "ᴀʙᴄ"


def test_small_caps_keeps_emoji_sequences_intact():
    out = text.small_caps_keep_emoji("fire 🔥 time")
    assert "🔥" in out and out.startswith("ꜰɪʀᴇ")


def test_sanitize_display_name_strips_bidi_and_clamps():
    dirty = "bad\u202ename" + "x" * 100
    clean = text.sanitize_display_name(dirty, max_len=10)
    assert "\u202e" not in clean and len(clean) <= 10
    assert text.sanitize_display_name("") == "unknown"


def test_fullwidth_and_monospace_are_available():
    assert text.fullwidth("ab") == "ａｂ"
    assert text.monospace("x") != "x"


# --------------------------------------------------------------------------- emoji


def test_registry_exposes_every_pack_id():
    assert emoji.total_custom_ids() >= 600


def test_custom_emoji_renders_tag_and_falls_back():
    emoji.set_custom_enabled(True)
    rendered = emoji.emoji("location")
    assert rendered.startswith("<tg-emoji emoji-id=") and rendered.endswith("</tg-emoji>")

    emoji.set_custom_enabled(False)
    try:
        assert emoji.emoji("location") == "📍"
        assert "<tg-emoji" not in emoji.upgrade_text("✅ done")
    finally:
        emoji.set_custom_enabled(True)


def test_unknown_name_uses_default_and_raw_emoji_passes_through():
    assert emoji.emoji("definitely-not-an-emoji", default="?") == "?"
    assert emoji.emoji("🎯") == "🎯"


def test_variants_and_reverse_lookup():
    assert len(emoji.variants("🔥")) > 1
    first = emoji.by_char("🔥", variant=0)
    second = emoji.by_char("🔥", variant=1)
    assert first and second and first.custom_id != second.custom_id
    assert emoji.by_id(first.custom_id).char == "🔥"


def test_aliases_resolve_to_the_same_character_family():
    assert emoji.char("shield") == "🛡"
    assert emoji.char("ban") == "🚫"
    assert emoji.char("guitar") == "🎸"


# --------------------------------------------------------------------------- palette


@pytest.mark.parametrize(
    ("label", "expected"),
    [("Delete", palette.DANGER), ("Save", palette.SUCCESS), ("Open docs", palette.LINK)],
)
def test_semantic_style(label, expected):
    assert palette.semantic_style(label) == expected


def test_no_button_is_left_unstyled():
    html = ui.action_bar([("Alpha", "a"), ("Bravo", "b"), ("Charlie", "c")], per_row=3)
    assert html.count("<tg-button ") == 3
    for tag in html.split("<tg-button ")[1:]:
        assert 'style="' in tag


def test_explicit_style_wins():
    assert "danger" in ui.button("Save", callback="x", style="danger")


def test_url_buttons_become_link_styled():
    assert 'style="link"' in ui.button("Docs", url="https://example.com")


# --------------------------------------------------------------------------- ui


def test_screen_composes_heading_panel_table_buttons():
    html = ui.screen(
        "guardian",
        icon="shield",
        blocks_=[
            ui.kv_panel([("Status", ui.ok("active"))]),
            ui.table(["Player", "Score"], [["ɢᴏʜᴀɴ", "12"]]),
            ui.actions([ui.button("Go", callback="g")]),
        ],
    )
    assert html.startswith("<h1>")
    assert "<blockquote>" in html and "<table" in html and "<tg-button-row>" in html
    assert "ɢᴜᴀʀᴅɪᴀɴ" in html


def test_kv_escapes_user_supplied_values():
    html = ui.kv("Name", ui.esc("<script>"))
    assert "<script>" not in html and "&lt;script&gt;" in html


# --------------------------------------------------------------------------- truncation


@pytest.mark.parametrize(
    "source",
    [
        "<h1>x</h1>" * 4000,
        "<blockquote>" + "y" * 40000 + "</blockquote>",
        "<details><summary>s</summary>" + "z" * 40000,
        "<blockquote>" + "q" * 32700 + "<b>bold",
        '<table border="1"><tr>' + "<td>c</td>" * 9000,
        "<blockquote><b>dangling",
    ],
)
def test_truncation_stays_within_limits_and_balanced(source):
    out = validate_html(source)
    assert len(out) <= MAX_CHARS
    assert _open_tags(out) == []


def test_short_message_is_left_alone_but_balanced():
    assert validate_html("<h1>ok</h1>") == "<h1>ok</h1>"
    assert _open_tags(validate_html("<b>dangling")) == []
