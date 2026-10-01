"""The JSON API and static files behind the dashboard.

Every route answers JSON (except ``/static/*`` and the page itself) and every
mutation logs an event, so the Telegram log channel shows what the browser did.

Route map::

    GET  /                     the dashboard (or the status page when disabled)
    GET  /static/{file}        css/js for the dashboard
    GET  /api/overview         kpis, modules, activity chart, recent events
    GET  /api/groups           group list (search, paging)
    GET  /api/groups/{id}      one group: modules, filters, warnings, top players
    POST /api/groups/{id}/modules/{key}   {"enabled": true|false}
    GET  /api/modules          the module registry with current state
    POST /api/modules/{key}    toggle a global switch
    GET  /api/events           recent events (tag + text filter)
    GET  /api/settings         configuration summary (secrets redacted)
    POST /api/actions/{name}   broadcast | sweep | backup | purge_events | restart | shutdown
    GET  /api/stream           server-sent events for the live feed
    POST /api/auth/login       {"token": "…"} or Telegram initData
    POST /api/auth/logout
    GET  /api/me               who am I / is auth required
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from ..logging_setup import get_logger
from .auth import COOKIE_NAME, Authenticator
from .state import WebState

log = get_logger("web.api")

__all__ = ["WebAPI", "register_routes"]

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 32 * 1024


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


class WebAPI:
    """Binds the state object to aiohttp handlers."""

    def __init__(self, state: WebState, *, auth: Authenticator | None = None, dashboard: bool = True) -> None:
        self.state = state
        self.auth = auth or Authenticator(state.settings)
        self.dashboard = dashboard

    # -- helpers -------------------------------------------------------------

    def _guard(self, handler: Callable[[web.Request], Any]) -> Callable[[web.Request], Any]:
        """Wrap a handler with authentication."""

        async def wrapped(request: web.Request) -> web.Response:
            if not self.auth.authorised(request):
                return self.auth.unauthorised()
            try:
                return await handler(request)
            except web.HTTPException:
                raise
            except Exception as exc:  # never leak a traceback to the browser
                log.exception("api error on %s: %s", request.path, exc)
                return _json({"ok": False, "error": str(exc)[:200]}, 500)

        return wrapped

    async def _body(self, request: web.Request) -> dict[str, Any]:
        if request.can_read_body:
            try:
                raw = await request.text()
                if len(raw) > MAX_BODY:
                    return {}
                return json.loads(raw or "{}")
            except Exception:
                return {}
        return {}

    # -- pages ---------------------------------------------------------------

    async def _page_response(self, request: web.Request) -> web.Response:
        return await self.page(request)

    async def page(self, request: web.Request) -> web.Response:
        """The dashboard shell (it fetches everything else over the API)."""
        index = STATIC_DIR / "index.html"
        if not index.exists():
            return web.Response(text="dashboard assets are missing", status=500)
        html = index.read_text(encoding="utf-8")
        html = (
            html.replace("{{BOT_NAME}}", self.state.settings.bot_name)
            .replace("{{MODE}}", "demo" if self.state.demo else "live")
            .replace("{{VERSION}}", _version())
        )
        return web.Response(
            text=html,
            content_type="text/html",
            headers={
                "Cache-Control": "no-store",
                # no X-Frame-Options: the panel is meant to be embeddable
                "Referrer-Policy": "no-referrer",
            },
        )

    async def static(self, request: web.Request) -> web.Response:
        name = request.match_info["file"]
        if "/" in name or name.startswith("."):
            raise web.HTTPNotFound()
        path = STATIC_DIR / name
        if not path.exists() or not path.is_file():
            raise web.HTTPNotFound()
        content_type = {
            ".css": "text/css",
            ".js": "text/javascript",
            ".svg": "image/svg+xml",
            ".webmanifest": "application/manifest+json",
        }.get(path.suffix, "application/octet-stream")
        return web.Response(
            body=path.read_bytes(),
            content_type=content_type,
            headers={"Cache-Control": "public, max-age=300"},
        )

    # -- read endpoints ------------------------------------------------------

    async def overview(self, request: web.Request) -> web.Response:
        return _json(await self.state.overview())

    async def groups(self, request: web.Request) -> web.Response:
        query = request.query.get("q", "")
        limit = int(request.query.get("limit", 50) or 50)
        offset = int(request.query.get("offset", 0) or 0)
        return _json(await self.state.groups(query=query, limit=limit, offset=offset))

    async def group(self, request: web.Request) -> web.Response:
        try:
            chat_id = int(request.match_info["chat_id"])
        except ValueError:
            return _json({"ok": False, "error": "bad chat id"}, 400)
        result = await self.state.group_detail(chat_id)
        return _json(result, 200 if result.get("ok") else 404)

    async def modules(self, request: web.Request) -> web.Response:
        return _json({"ok": True, "modules": await self.state.modules_payload(), "demo": self.state.demo})

    async def events(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", 60) or 60)
        tag = request.query.get("tag", "")
        query = request.query.get("q", "")
        return _json(await self.state.events(limit=limit, tag=tag, query=query))

    async def settings(self, request: web.Request) -> web.Response:
        return _json(await self.state.settings_view())

    async def me(self, request: web.Request) -> web.Response:
        session = self.auth.read(request.cookies.get(COOKIE_NAME))
        return _json(
            {
                "ok": True,
                "authenticated": not self.auth.required or bool(session),
                "auth_required": self.auth.required,
                "auth_mode": self.auth.mode,
                "demo": self.state.demo,
                "can_control": self.state.can_control,
                "subject": (session or {}).get("sub"),
            }
        )

    # -- writes --------------------------------------------------------------

    async def toggle_group_module(self, request: web.Request) -> web.Response:
        try:
            chat_id = int(request.match_info["chat_id"])
        except ValueError:
            return _json({"ok": False, "error": "bad chat id"}, 400)
        key = request.match_info["key"]
        body = await self._body(request)
        enabled = bool(body.get("enabled", True))
        result = await self.state.set_chat_module(chat_id, key, enabled)
        return _json(result, 200 if result.get("ok") else 400)

    async def toggle_module(self, request: web.Request) -> web.Response:
        key = request.match_info["key"]
        body = await self._body(request)
        enabled = bool(body.get("enabled", True))
        result = await self.state.set_global_toggle(key, enabled)
        return _json(result, 200 if result.get("ok") else 400)

    async def action(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        body = await self._body(request)
        result = await self.state.action(name, body)
        # restarts are handled by the caller (the bot process), because the web
        # layer must never kill the process it is serving from.
        if result.get("restart") or result.get("shutdown"):
            self.state.services["pending_lifecycle"] = name
            hook = self.state.services.get("lifecycle")
            if callable(hook):
                try:
                    asyncio.get_running_loop().create_task(hook(name))
                except Exception as exc:
                    log.debug("lifecycle hook failed: %s", exc)
        return _json(result, 200 if result.get("ok") else 400)

    async def login(self, request: web.Request) -> web.Response:
        body = await self._body(request)
        token = str(body.get("token") or "").strip()
        init_data = str(body.get("init_data") or "")
        ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or (request.remote or "?")

        ok = False
        subject = "token"
        if token:
            ok = self.auth.check_token(token, ip)
        elif init_data:
            ok = self.auth.check_init_data(init_data)
            subject = "telegram"
        if not ok:
            return _json({"ok": False, "error": "invalid credentials"}, 401)

        response = _json({"ok": True, "subject": subject})
        response.set_cookie(
            COOKIE_NAME,
            self.auth.issue(subject),
            max_age=12 * 3600,
            httponly=True,
            samesite="Lax",
            secure=False,  # the app may be reached over http on a private network
        )
        return response

    async def logout(self, request: web.Request) -> web.Response:
        response = _json({"ok": True})
        response.del_cookie(COOKIE_NAME)
        return response

    # -- server-sent events --------------------------------------------------

    async def stream(self, request: web.Request) -> web.StreamResponse:
        if not self.auth.authorised(request):
            return self.auth.unauthorised()

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        interval = 5.0 if self.state.live else 4.0
        try:
            while True:
                payload = await self.state.snapshot()
                await response.write(f"data: {json.dumps(payload)}\n\n".encode())
                await asyncio.sleep(interval)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception as exc:
            log.debug("stream ended: %s", exc)
        finally:
            try:
                await response.write_eof()
            except Exception:
                pass
        return response

    # -- wiring --------------------------------------------------------------

    def register(self, app: web.Application) -> None:
        """Attach every route to ``app``."""
        g = self._guard

        # deliberately unguarded: the login screen needs to know whether to show itself
        app.router.add_get("/api/me", self.me)
        app.router.add_get("/api/overview", g(self.overview))
        app.router.add_get("/api/groups", g(self.groups))
        app.router.add_get("/api/groups/{chat_id}", g(self.group))
        app.router.add_get("/api/modules", g(self.modules))
        app.router.add_get("/api/events", g(self.events))
        app.router.add_get("/api/settings", g(self.settings))
        app.router.add_get("/api/stream", self.stream)

        app.router.add_post("/api/groups/{chat_id}/modules/{key}", g(self.toggle_group_module))
        app.router.add_post("/api/modules/{key}", g(self.toggle_module))
        app.router.add_post("/api/actions/{name}", g(self.action))
        app.router.add_post("/api/auth/login", self.login)
        app.router.add_post("/api/auth/logout", self.logout)

        app.router.add_get("/static/{file}", self.static)
        if self.dashboard:
            app.router.add_get("/", self.page)


def _version() -> str:
    from .. import __version__

    return __version__


def register_routes(
    app: web.Application,
    state: WebState,
    *,
    auth: Authenticator | None = None,
    dashboard: bool = True,
) -> WebAPI:
    """Convenience wrapper used by :class:`gohan.services.keep_alive.KeepAlive`."""
    api = WebAPI(state, auth=auth, dashboard=dashboard)
    api.register(app)
    return api
