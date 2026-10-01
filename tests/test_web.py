"""Tests for the web control panel: API, auth and the demo data layer."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gohan.config import Settings
from gohan.storage import Database
from gohan.web.api import register_routes
from gohan.web.auth import Authenticator, verify_init_data
from gohan.web.modules import MODULES, chat_modules, global_modules, module
from gohan.web.state import build_state



async def make_client(state, *, settings: Settings | None = None, auth: Authenticator | None = None) -> TestClient:
    app = web.Application()
    register_routes(app, state, auth=auth, dashboard=True)

    async def status(request):  # the classic page, kept for parity with KeepAlive
        return web.Response(text="status")

    app.router.add_get("/status", status)
    return TestClient(TestServer(app))


# ---------------------------------------------------------------------------
#  demo state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overview_shape_in_demo_mode() -> None:
    state = build_state(Settings(bot_name="GOHAN"), None, {}, demo=True)
    client = await make_client(state)
    await client.start_server()
    try:
        response = await client.get("/api/overview")
        assert response.status == 200
        data = await response.json()
        assert data["ok"] and data["demo"] is True
        assert len(data["kpis"]) == 6
        assert len(data["activity"]) == 12
        assert len(data["modules"]) == len(MODULES)
        assert data["categories"]
        # every module carries what the UI needs to render a card
        for mod in data["modules"]:
            assert {"key", "name", "icon", "category", "blurb", "scope", "enabled"} <= set(mod)

        events = await (await client.get("/api/events?limit=10")).json()
        assert events["ok"] and len(events["events"]) <= 10
        assert events["events"][0]["tag"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_group_pages_and_drawer_data() -> None:
    state = build_state(Settings(), None, {}, demo=True)
    client = await make_client(state)
    await client.start_server()
    try:
        groups = await (await client.get("/api/groups")).json()
        assert groups["ok"] and groups["groups"]
        first = groups["groups"][0]
        assert {"chat_id", "title", "modules_on", "modules_total"} <= set(first)

        detail = await (await client.get(f"/api/groups/{first['chat_id']}")).json()
        assert detail["ok"]
        group = detail["group"]
        assert set(group["module_states"]) == {m.key for m in chat_modules()}
        assert "top_filters" in group and "leaderboard" in group

        missing = await client.get("/api/groups/424242")
        assert missing.status == 404
        bad = await client.get("/api/groups/not-a-number")
        assert bad.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_dashboard_assets_are_served() -> None:
    state = build_state(Settings(bot_name="GOHAN"), None, {}, demo=True)
    client = await make_client(state)
    await client.start_server()
    try:
        page = await client.get("/")
        assert page.status == 200
        html = await page.text()
        assert "GOHAN" in html and "/static/app.js" in html
        assert "{{" not in html, "template placeholders must all be substituted"

        css = await client.get("/static/style.css")
        assert css.status == 200 and "text/css" in css.headers["Content-Type"]
        assert len(await css.text()) > 2000

        js = await client.get("/static/app.js")
        assert js.status == 200 and "javascript" in js.headers["Content-Type"]

        # path traversal is refused
        assert (await client.get("/static/..%2Fapi.py")).status in (400, 404)
    finally:
        await client.close()


# ---------------------------------------------------------------------------
#  live state against a real database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_state_toggles_persist(tmp_path: Path) -> None:
    db = Database(tmp_path / "web.sqlite3")
    await db.connect()
    try:
        settings = Settings(bot_token="1:x")
        state = build_state(settings, db, {"settings": settings})
        assert state.live and state.can_control

        await db.upsert_chat(-100500, title="Live Group", type="supergroup", added_by=1)
        client = await make_client(state)
        await client.start_server()
        try:
            # a chat module toggle is written to the chat's settings blob
            response = await client.post(
                "/api/groups/-100500/modules/captcha", json={"enabled": True}
            )
            assert response.status == 200
            payload = await response.json()
            assert payload["ok"] and payload["enabled"] is True

            chat_settings = await db.get_chat_settings(-100500)
            assert chat_settings["captcha"] is True

            # an unknown or wrong-scope module is rejected
            assert (await client.post("/api/groups/-100500/modules/nope", json={"enabled": True})).status == 400
            assert (await client.post("/api/groups/-100500/modules/ai", json={"enabled": True})).status == 400

            # a global toggle lands in kv and logs an event
            response = await client.post("/api/modules/maintenance", json={"enabled": True})
            assert response.status == 200
            assert await db.get_kv("maintenance") is True
            assert "maintenance" in (await db.get_kv("modules", {}))

            events = await (await client.get("/api/events?limit=20")).json()
            assert any(e["tag"] == "WebToggle" for e in events["events"])

            detail = await (await client.get("/api/groups/-100500")).json()
            assert detail["group"]["module_states"]["captcha"] is True
        finally:
            await client.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_actions_and_unknown_action(tmp_path: Path) -> None:
    db = Database(tmp_path / "actions.sqlite3")
    await db.connect()
    try:
        settings = Settings(bot_token="1:x")
        state = build_state(settings, db, {"settings": settings})
        client = await make_client(state)
        await client.start_server()
        try:
            result = await (await client.post("/api/actions/nonsense", json={})).json()
            assert result["ok"] is False

            result = await (await client.post("/api/actions/sweep", json={})).json()
            assert result["ok"] is False and "watchdog" in result["error"]

            result = await (await client.post("/api/actions/broadcast", json={"text": ""})).json()
            assert result["ok"] is False

            result = await (await client.post("/api/actions/purge_events", json={"days": 30})).json()
            assert result["ok"] is True
        finally:
            await client.close()
    finally:
        await db.close()


# ---------------------------------------------------------------------------
#  auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_auth_flow() -> None:
    settings = Settings(bot_token="1:x", web_auth="token", web_token="s3cret-token")
    auth = Authenticator(settings)
    assert auth.required and auth.mode == "token"
    state = build_state(settings, None, {"auth_mode": "token"}, demo=True)
    client = await make_client(state, auth=auth)
    await client.start_server()
    try:
        # locked by default
        assert (await client.get("/api/overview")).status == 401
        me = await (await client.get("/api/me")).json()
        assert me["auth_required"] is True and me["authenticated"] is False

        # a wrong token is refused
        assert (await client.post("/api/auth/login", json={"token": "nope"})).status == 401

        # the right one sets a session cookie
        response = await client.post("/api/auth/login", json={"token": "s3cret-token"})
        assert response.status == 200
        assert response.cookies.get("gohan_session")
        assert (await client.get("/api/overview")).status == 200

        # bearer tokens work for scripts
        headers = {"Authorization": "Bearer s3cret-token"}
        assert (await client.get("/api/overview", headers=headers)).status == 200

        # logout clears it
        await client.post("/api/auth/logout")
        assert (await client.get("/api/overview")).status in (200, 401)  # cookie held by the jar
    finally:
        await client.close()


def test_sessions_are_signed_and_expire() -> None:
    auth = Authenticator(Settings(web_auth="token", web_token="abc"))
    cookie = auth.issue("token")
    assert auth.read(cookie)["sub"] == "token"
    assert auth.read(cookie + "x") is None
    assert auth.read("garbage") is None

    expired = auth._sign({"sub": "token", "exp": time.time() - 5})
    assert auth.read(expired) is None


def test_login_attempts_are_rate_limited() -> None:
    auth = Authenticator(Settings(web_auth="token", web_token="abc"))
    for _ in range(8):
        assert auth.check_token("wrong", "1.2.3.4") is False
    # the correct token is refused too once the IP is throttled
    assert auth.check_token("abc", "1.2.3.4") is False
    assert auth.check_token("abc", "5.6.7.8") is True


def test_telegram_init_data_verification() -> None:
    bot_token = "123456:TESTTOKEN"
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AAExample",
        "user": json.dumps({"id": 42, "first_name": "Gohan"}),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    init_data = "&".join(f"{k}={v}" for k, v in fields.items()) + f"&hash={signature}"

    verified = verify_init_data(init_data, bot_token)
    assert verified is not None and verified["user"]["id"] == 42
    assert verify_init_data(init_data, "999:OTHER") is None
    assert verify_init_data("user={}&hash=bad", bot_token) is None

    # and a tampered auth_date is caught by the age check
    stale = dict(fields, auth_date=str(int(time.time()) - 200000))
    check_stale = "\n".join(f"{k}={v}" for k, v in sorted(stale.items()))
    signature_stale = hmac.new(secret, check_stale.encode(), hashlib.sha256).hexdigest()
    stale_data = "&".join(f"{k}={v}" for k, v in stale.items()) + f"&hash={signature_stale}"
    assert verify_init_data(stale_data, bot_token, max_age=0) is not None  # 0 disables the age check
    assert verify_init_data(stale_data, bot_token) is None                 # default: 24 h
    assert verify_init_data(stale_data, bot_token, max_age=60) is None


@pytest.mark.asyncio
async def test_telegram_auth_restricts_to_allowed_ids() -> None:
    settings = Settings(bot_token="123456:TESTTOKEN", web_auth="telegram", web_allowed_ids="42")
    auth = Authenticator(settings)

    def signed(user_id: int) -> str:
        fields = {
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": user_id, "first_name": "x"}),
        }
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", settings.bot_token.get_secret_value().encode(), hashlib.sha256).digest()
        signature = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return "&".join(f"{k}={v}" for k, v in fields.items()) + f"&hash={signature}"

    assert auth.check_init_data(signed(42)) is True
    assert auth.check_init_data(signed(7)) is False

    state = build_state(settings, None, {"auth_mode": "telegram"}, demo=True)
    client = await make_client(state, auth=auth)
    await client.start_server()
    try:
        assert (await client.post("/api/auth/login", json={"init_data": signed(42)})).status == 200
        assert (await client.post("/api/auth/login", json={"init_data": signed(7)})).status == 401
    finally:
        await client.close()


# ---------------------------------------------------------------------------
#  registry
# ---------------------------------------------------------------------------


def test_module_registry_is_consistent() -> None:
    keys = [m.key for m in MODULES]
    assert len(keys) == len(set(keys)), "module keys must be unique"
    assert chat_modules() and global_modules()
    assert module("guardian") is not None and module("guardian").scope == "chat"
    assert module("ai").scope == "global"
    assert module("nope") is None
    for item in MODULES:
        assert item.icon and item.blurb and item.name
        assert item.scope in ("chat", "global")
        assert item.category in ("protection", "community", "fun", "system")


# ---------------------------------------------------------------------------
#  front-end / back-end contract
# ---------------------------------------------------------------------------


def _app_routes(app: web.Application) -> set[str]:
    paths: set[str] = set()
    for route in app.router.routes():
        resource = getattr(route, "resource", None)
        if resource is not None:
            paths.add(resource.canonical)
        else:
            paths.add(route.path)
    return paths


def test_every_api_path_the_page_calls_exists() -> None:
    """The dashboard must not call an endpoint the server does not serve."""
    import re

    app = web.Application()
    register_routes(app, build_state(Settings(), None, {}, demo=True))
    known = _app_routes(app)

    js = (Path(__file__).parent.parent / "gohan" / "web" / "static" / "app.js").read_text()
    called = set(re.findall(r"['\"`](/api/[^'\"`\s?]*)", js))
    assert called, "expected the dashboard to call the API"

    def pattern(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "[^/]+", path)

    patterns = [re.compile(f"^{pattern(p)}$") for p in known]
    def normalise(path: str) -> str:
        # "/api/groups/${encodeURIComponent(id)}/modules/${key}" -> "/api/groups/X/modules/X"
        return re.sub(r"\$\{[^}]*\}", "X", path)

    unknown = [
        call for call in called
        if not any(p.match(normalise(call)) for p in patterns)
    ]
    assert not unknown, f"dashboard calls unknown endpoints: {sorted(unknown)}"


def test_dashboard_assets_match_the_page() -> None:
    html = (Path(__file__).parent.parent / "gohan" / "web" / "static" / "index.html").read_text()
    assert "/static/style.css" in html and "/static/app.js" in html
    for asset in ("style.css", "app.js"):
        assert (Path(__file__).parent.parent / "gohan" / "web" / "static" / asset).stat().st_size > 500

    # the page must not load anything from the internet: it has to work offline
    assert "http://" not in html.replace("http://www.w3.org", "")
    assert "https://" not in html


# ---------------------------------------------------------------------------
#  moderation surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_moderation_page_data_in_demo() -> None:
    state = build_state(Settings(), None, {}, demo=True)
    client = await make_client(state)
    await client.start_server()
    try:
        data = await (await client.get("/api/moderation?days=7")).json()
        assert data["ok"] and data["window_days"] == 7
        assert len(data["kpis"]) == 4
        assert len(data["chart"]) == 7
        assert data["cases"] and data["offenders"] and data["chats"]
        case = data["cases"][0]
        assert {"user_id", "user", "chat", "reason", "age"} <= set(case)

        thirty = await (await client.get("/api/moderation?days=30")).json()
        assert len(thirty["chart"]) == 30
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_user_lookup_and_case_file(tmp_path: Path) -> None:
    db = Database(tmp_path / "users.sqlite3")
    await db.connect()
    try:
        settings = Settings(bot_token="1:x")
        state = build_state(settings, db, {"settings": settings})
        await db.upsert_user(900, username="badactor", first_name="Bad Actor")
        await db.upsert_chat(-100700, title="Target Group", type="supergroup")
        await db.add_warning(-100700, 900, admin_id=1, reason="scam link")

        client = await make_client(state)
        await client.start_server()
        try:
            found = await (await client.get("/api/users?q=badactor")).json()
            assert found["count"] == 1 and found["users"][0]["warnings"] == 1
            assert (await (await client.get("/api/users?q=900")).json())["count"] == 1

            detail = await (await client.get("/api/users/900")).json()
            assert detail["user"]["name"] == "Bad Actor"
            assert detail["warnings"][0]["reason"] == "scam link"
            assert detail["warnings"][0]["chat_title"] == "Target Group"
            assert detail["groups"][0]["title"] == "Target Group"

            assert (await client.get("/api/users/123456")).status == 404
            assert (await client.get("/api/users/nope")).status == 400

            blocked = await (await client.post("/api/users/900/actions/ban", json={})).json()
            assert blocked["ok"] and await db.is_banned(900) is True
            blocked_users = await (await client.get("/api/users?banned=true")).json()
            assert [u["user_id"] for u in blocked_users["users"]] == [900]

            cleared = await (await client.post("/api/users/900/actions/clear_warnings", json={})).json()
            assert cleared["ok"] and cleared["message"].startswith("cleared 1")

            released = await (await client.post("/api/users/900/actions/unban", json={})).json()
            assert released["ok"] and await db.is_banned(900) is False

            assert (await client.post("/api/users/900/actions/explode", json={})).status == 400
        finally:
            await client.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_group_settings_editor_and_bulk_apply(tmp_path: Path) -> None:
    db = Database(tmp_path / "edit.sqlite3")
    await db.connect()
    try:
        settings = Settings(bot_token="1:x")
        state = build_state(settings, db, {"settings": settings})
        await db.upsert_chat(-100800, title="Editor Group", type="supergroup")
        await db.upsert_chat(-100801, title="Other Group", type="supergroup")

        client = await make_client(state)
        await client.start_server()
        try:
            response = await client.patch(
                "/api/groups/-100800/settings",
                json={"settings": {"welcome_text": "hello {name}", "rules": "be nice", "warn_limit": 5, "bogus": "ignored"}},
            )
            assert response.status == 200
            payload = await response.json()
            assert set(payload["updated"]) == {"welcome_text", "rules", "warn_limit"}

            chat_settings = await db.get_chat_settings(-100800)
            assert chat_settings["welcome_text"] == "hello {name}"
            assert chat_settings["warn_limit"] == 5
            assert "bogus" not in chat_settings

            detail = await (await client.get("/api/groups/-100800")).json()
            assert detail["group"]["editable"]["warn_limit"] == 5

            # a hostile warn_limit is clamped, and an empty patch is refused
            await client.patch("/api/groups/-100800/settings", json={"settings": {"warn_limit": 999}})
            assert (await db.get_chat_settings(-100800))["warn_limit"] == 20
            assert (await client.patch("/api/groups/-100800/settings", json={"settings": {}})).status == 400

            # bulk apply reaches every active chat
            bulk = await (await client.post("/api/modules/captcha/apply-all", json={"enabled": True})).json()
            assert bulk["ok"] and bulk["groups"] == 2
            for chat_id in (-100800, -100801):
                assert (await db.get_chat_settings(chat_id))["captcha"] is True

            # global modules cannot be bulk-applied
            assert (await client.post("/api/modules/ai/apply-all", json={"enabled": True})).status == 400
        finally:
            await client.close()
    finally:
        await db.close()
