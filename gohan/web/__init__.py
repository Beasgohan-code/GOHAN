"""Web control panel for GOHAN.

A single self-contained dashboard (no build step, no CDN) served by the same
aiohttp server as the keep-alive endpoints, plus a small JSON API the page talks
to. It shows the same data the bot acts on - groups, modules, filters, warnings,
events - and lets the owner flip things from a browser.

The panel works in two modes:

**live**  attached to a running bot: real database, real settings
**demo**  attached to nothing: deterministic sample data, used by
          ``python -m gohan.services.preview`` and the hosted preview
"""

from __future__ import annotations

from .api import WebAPI, register_routes
from .auth import Authenticator
from .modules import MODULES, categories, module
from .state import WebState, build_state

__all__ = [
    "MODULES",
    "Authenticator",
    "WebAPI",
    "WebState",
    "build_state",
    "categories",
    "module",
    "register_routes",
]
