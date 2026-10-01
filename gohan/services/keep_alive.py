"""Keep-alive web server + status page.

Free hosts (Render, Koyeb, Railway, Fly, Replit…) idle a web service that gets no
traffic, so the bot periodically pings its own public URL. The same server also
serves a small status page so a human can check on the bot from a browser.

Ports and URLs are auto-detected from the usual platform variables - see
:func:`gohan.config.detect_public_url` - so on most hosts ``KEEP_ALIVE_URL`` does
not need to be set at all.

Routes
------
``GET /``        the status page (dark, no build step, no external assets)
``GET /health``  JSON status - useful for uptime monitors and the Docker
                 ``HEALTHCHECK``
``GET /ping``    plain "ok" (the self-ping target; cheapest possible response)
"""

from __future__ import annotations

import asyncio
import html
import platform
import sys
import time
from typing import Any, Awaitable, Callable

from aiohttp import ClientSession, ClientTimeout, web

from .. import __version__
from ..config import Settings, detect_host
from ..logging_setup import get_logger

log = get_logger("keep_alive")

__all__ = ["KeepAlive", "health_payload"]

#: ``() -> dict`` or coroutine returning extra health data (db counts, …).
StatsProvider = Callable[[], "dict[str, Any] | Awaitable[dict[str, Any]]"]


class KeepAlive:
    """Owns the aiohttp app, the runner and the self-ping loop."""

    def __init__(self, settings: Settings, *, stats: StatsProvider | None = None) -> None:
        self.settings = settings
        self.stats = stats
        self.started = time.time()
        self._runner: web.AppRunner | None = None
        self._task: asyncio.Task[None] | None = None
        self._pings = 0
        self._ping_failures = 0
        self._last_error: str | None = None

    # -- data ----------------------------------------------------------------

    async def _extra(self) -> dict[str, Any]:
        if self.stats is None:
            return {}
        try:
            result = self.stats()
            if asyncio.iscoroutine(result):
                result = await result
            return dict(result) if result else {}
        except Exception as exc:  # a broken stat must never break /health
            log.debug("stats provider failed: %s", exc)
            return {}

    async def health(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "ok",
            "bot": self.settings.bot_name,
            "version": __version__,
            "uptime_s": int(time.time() - self.started),
            "host": detect_host(),
            "python": platform.python_version(),
            "pings": self._pings,
        }
        if self._last_error:
            payload["last_error"] = self._last_error
        payload.update(await self._extra())
        return payload

    # -- routes --------------------------------------------------------------

    async def _root(self, request: web.Request) -> web.Response:
        data = await self.health()
        return web.Response(text=render_page(data), content_type="text/html")

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response(await self.health(), headers={"Cache-Control": "no-store"})

    async def _ping(self, request: web.Request) -> web.Response:
        return web.Response(text="ok", headers={"Cache-Control": "no-store"})

    def make_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._root)
        app.router.add_get("/health", self._health)
        app.router.add_get("/ping", self._ping)
        return app

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Start the web server (idempotent)."""
        if self._runner is not None:
            return
        runner = web.AppRunner(self.make_app(), access_log=None)
        await runner.setup()
        # 0.0.0.0 so the platform's proxy (and the preview environment) can reach it.
        site = web.TCPSite(runner, "0.0.0.0", self.settings.port)
        await site.start()
        self._runner = runner
        log.info("status page on http://0.0.0.0:%s/", self.settings.port)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def start_background(self) -> None:
        """Start the server and the self-ping loop as background tasks."""
        loop = asyncio.get_running_loop()
        loop.create_task(self.start())
        if self.settings.resolved_keep_alive_url:
            self._task = loop.create_task(self.self_ping_loop())

    async def self_ping_loop(self) -> None:
        """Ping our own public URL so the host does not idle the process."""
        base = self.settings.resolved_keep_alive_url
        if not base:
            log.info(
                "no public URL detected - self-ping disabled "
                "(set KEEP_ALIVE_URL if this host idles services)"
            )
            return
        url = f"{base}/ping"
        interval = self.settings.keep_alive_interval
        log.info("self-ping every %ss -> %s", interval, url)
        while True:
            await asyncio.sleep(interval)
            try:
                async with ClientSession(timeout=ClientTimeout(total=20)) as session:
                    async with session.get(url) as response:
                        self._last_error = None if response.status < 500 else f"HTTP {response.status}"
                        if response.status < 500:
                            self._pings += 1
                            self._ping_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._ping_failures += 1
                self._last_error = str(exc)[:200]
                if self._ping_failures in (1, 5) or self._ping_failures % 20 == 0:
                    log.warning("self-ping failed (%sx): %s", self._ping_failures, exc)


# ---------------------------------------------------------------------------
#  the status page (inline so there is no template directory to lose)
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{bot} · {tagline}</title><style>
:root{{--bg:#0b0d13;--bg2:#11141d;--card:#161a25;--line:#252a38;--fg:#e9ecf4;--mut:#8a93a8;
--ok:#3fb950;--blue:#4c8dff;--gold:#e3b341;--pink:#f778ba}}
*{{box-sizing:border-box}}
body{{margin:0;font:15px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
background:radial-gradient(1100px 500px at 15% -10%,#1b2340 0%,var(--bg) 55%);color:var(--fg)}}
main{{max-width:820px;margin:0 auto;padding:44px 20px 64px}}
header{{display:flex;align-items:center;gap:12px}}
.dot{{width:11px;height:11px;border-radius:50%;background:var(--ok);box-shadow:0 0 14px var(--ok);
animation:pulse 2.4s infinite}}
@keyframes pulse{{50%{{opacity:.45}}}}
h1{{font-size:30px;margin:0;letter-spacing:1px}}
.badge{{margin-left:auto;font-size:12px;color:var(--mut);border:1px solid var(--line);
border-radius:999px;padding:4px 12px;background:var(--bg2)}}
p.sub{{color:var(--fg);opacity:.85;margin:14px 0 4px;font-size:16px}}
p.tag{{color:var(--mut);margin:0 0 26px}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 26px}}
.chip{{background:var(--bg2);border:1px solid var(--line);border-radius:999px;
padding:6px 14px;font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:12px}}
.card{{background:linear-gradient(180deg,var(--card),var(--bg2));border:1px solid var(--line);
border-radius:14px;padding:15px}}
.k{{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.8px}}
.v{{font-size:23px;font-weight:650;margin-top:6px;font-variant-numeric:tabular-nums}}
h2{{font-size:14px;color:var(--mut);text-transform:uppercase;letter-spacing:1px;
margin:34px 0 8px;font-weight:600}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
td{{padding:8px 0;border-bottom:1px solid var(--line)}}
td:last-child{{text-align:right;font-variant-numeric:tabular-nums}}
footer{{color:var(--mut);font-size:12px;margin-top:34px;display:flex;gap:14px;flex-wrap:wrap}}
code{{background:var(--bg2);border:1px solid var(--line);border-radius:6px;padding:2px 7px}}
a{{color:var(--blue);text-decoration:none}}
</style></head><body><main>
<header><span class="dot"></span><h1>{bot}</h1><span class="badge">v{version} · port {port}</span></header>
<p class="sub">{intro}</p>
<p class="tag">{tagline} · up {uptime} · self-refreshes every 15s</p>
<div class="chips">{chips}</div>
<h2>live</h2>
<div class="grid">{cards}</div>
<h2>everything</h2>
<table>{rows}</table>
<footer><span>auto-refresh 15s</span><span><code>/health</code> json</span>
<span><code>/ping</code> ok</span><span>{host}</span></footer>
</main><script>setTimeout(()=>location.reload(),15000)</script></body></html>
"""

#: What the bot says about itself - mirrored from the /start screen.
INTRO = "I have lots of features like AI Chatbot, Anime, Music, Notes, Filters, Fun and many others useful commands!"
TAGLINE = "this is my plan of bot"
FEATURES = (
    "🤖 AI Chatbot", "🎬 Anime", "🎧 Music", "📝 Notes",
    "🧲 Filters", "✨ Fun", "🛡 Guardian", "🎮 Games",
    "🔮 Rich messages", "😺 Custom emoji",
)

_SKIP_KEYS = {"status", "bot", "version", "host", "python", "intro", "tagline"}


def _human(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int) and value >= 1000:
        return f"{value:,}"
    return str(value)


def render_page(data: dict[str, Any]) -> str:
    """Render the health payload as a self-contained HTML page."""
    bot = html.escape(str(data.get("bot", "GOHAN")))
    version = html.escape(str(data.get("version", "")))
    port = data.get("port", "-")
    uptime = int(data.get("uptime_s") or 0)
    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_text = f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s"

    cards = [("uptime", uptime_text), ("host", html.escape(str(data.get("host", "-"))))]
    for key, value in data.items():
        if key in _SKIP_KEYS or key == "uptime_s":
            continue
        cards.append((key.replace("_", " "), html.escape(_human(value))))

    card_html = "".join(
        f'<div class="card"><div class="k">{html.escape(str(k))}</div>'
        f'<div class="v">{v}</div></div>'
        for k, v in cards
    )
    rows = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(_human(v))}</td></tr>"
        for k, v in data.items()
    )
    chips = "".join(f'<span class="chip">{html.escape(name)}</span>' for name in FEATURES)
    return _PAGE.format(
        bot=bot,
        version=version,
        port=port,
        cards=card_html,
        rows=rows,
        chips=chips,
        intro=html.escape(str(data.get("intro", INTRO))),
        tagline=html.escape(str(data.get("tagline", TAGLINE))),
        host=html.escape(str(data.get("host", ""))),
        uptime=uptime_text,
    )


def health_payload() -> dict[str, Any]:  # pragma: no cover - convenience for CLIs
    """Minimal interpreter info, used by ``python -m gohan --check``."""
    return {
        "status": "ok",
        "python": platform.python_version(),
        "executable": sys.executable,
        "host": detect_host(),
    }
