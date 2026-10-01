"""Command handlers.

``general``  ``/start``, ``/help``, ``/id``, notes, ``/ping``
``ai``       ``/ask``, ``/ai``, ``/summary``, ``/translate``
``owner``    watchdog, reports, broadcast, backup, ``/status``, ``/stats``

The routers are attached in :func:`gohan.dispatcher.build_dispatcher`, which also
lists the order they run in.
"""

from __future__ import annotations

__all__ = ["ai", "general", "owner"]
