"""AI features: streaming answers, chat memory, per-chat personalities."""

from __future__ import annotations

from .llm import ChatMessage, LLMClient, LLMError, build_llm
from .chat import AIConversation, ConversationStore

__all__ = ["AIConversation", "ChatMessage", "ConversationStore", "LLMClient", "LLMError", "build_llm"]
