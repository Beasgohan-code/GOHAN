"""Run the status page on its own, with demo numbers.

    python -m gohan.services.preview [port]

Handy for developing the page (or showing it off) without a bot token.
"""

from __future__ import annotations

import asyncio
import random
import sys
import time

from ..config import Settings
from ..logging_setup import setup_logging
from ..web.auth import Authenticator
from ..web.state import build_state
from .keep_alive import KeepAlive

_STARTED = time.time()


def demo_stats() -> dict[str, object]:
    """Deterministic-ish numbers so the page looks alive while developing."""
    minutes = int((time.time() - _STARTED) / 30)
    return {
        "intro": "I have lots of features like AI Chatbot, Anime, Music, Notes, Filters, Fun and many others useful commands!",
        "tagline": "this is my plan of bot",
        "users": 1240 + minutes * 3,
        "active_24h": 318 + minutes,
        "chats": 37,
        "warnings_24h": 12 + random.randint(0, 3),
        "games_24h": 86 + minutes * 2,
        "mtproto": "user session",
        "assistant": "demo mode",
        "watchdog": f"{minutes + 1} sweeps · 0 alerts",
        "rich_messages": "on",
        "custom_emoji": "600 ids",
        "filters": 214,
        "notes": 58,
        "anime_searches_24h": 96,
    }


async def main(port: int = 8080, *, demo: bool = True) -> None:
    setup_logging("INFO")
    settings = Settings(bot_name="GOHAN", port=port, keep_alive_url="", web_auth="off")
    state = build_state(settings, None, {}, demo=demo)
    auth = Authenticator(settings)
    server = KeepAlive(settings, stats=demo_stats, state=state)
    server.auth = auth
    await server.start()
    print(f"\n  GOHAN control panel -> http://0.0.0.0:{port}/   (Ctrl+C to stop)")
    print(f"  status page         -> http://0.0.0.0:{port}/status")
    print(f"  api                 -> http://0.0.0.0:{port}/api/overview\n")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await server.stop()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8080
    asyncio.run(main(port))
