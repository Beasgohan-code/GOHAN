"""GOHAN — a Telegram bot built on aiogram 3 and Kurigram.

The package is organised around three layers:

* :mod:`gohan.rich`      — a Rich Message builder (HTML *and* block entities) plus
                           a streaming-draft engine for AI-style live replies.
* :mod:`gohan.mtproto`   — the Kurigram side: everything the Bot API cannot do
                           (2 GB transfers, history, restricted chats).
* :mod:`gohan.handlers`  — thin, declarative aiogram routers wiring it together.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
