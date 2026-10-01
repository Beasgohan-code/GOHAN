"""Kurigram (MTProto) side of the bot.

Everything here is *optional*: with ``MTPROTO_MODE=off`` or missing API
credentials the bridge simply reports itself unavailable and the Bot API half of
the bot keeps working.

What MTProto adds that the Bot API cannot do:

* **2 GB transfers** (Bot API caps at 20 MB download / 50 MB upload)
* **history** - read messages from chats the bot is not a member of
* **reputation** - check whether a user spams elsewhere, which is the single most
  useful signal when deciding a join request
* **mass deletion** - clear a flood properly, including messages older than 48 h
"""

from __future__ import annotations

from .bridge import MTProtoBridge, MTProtoStatus, build_bridge

__all__ = ["MTProtoBridge", "MTProtoStatus", "build_bridge"]
