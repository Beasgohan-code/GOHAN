"""Guardian: keeping a group clean.

The package is split so the *decisions* are testable without Telegram:

``filters``     pure heuristics - is this message spam, a scam, a flood?
``captcha``     new-member screening (join requests + in-chat button)
``antiraid``    raid detection and lockdown
``moderation``  the admin commands (warn/mute/kick/ban/purge/lockdown)
``panel``       the per-chat settings screen

Default posture: **report, don't delete**, for anything ambiguous. False
positives annoy real members far more than a delayed ban annoys an admin, and
every automatic action is recorded in the log channel so it can be reviewed.
"""

from __future__ import annotations

from .antiraid import RaidDetector, RaidState
from .filters import SpamVerdict, Verdict, inspect_message

__all__ = ["RaidDetector", "RaidState", "SpamVerdict", "Verdict", "inspect_message"]
