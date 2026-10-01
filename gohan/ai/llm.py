"""LLM access, provider-agnostic.

Two providers:

``openai`` / ``openai_compatible``
    any endpoint that speaks ``POST {base}/chat/completions`` with SSE streaming:
    OpenAI, OpenRouter, Groq, Together, a local llama.cpp server, Ollama
    (``/v1``), LM Studio…

``off``
    no network at all. :class:`DemoGenerator` produces deterministic, *clearly
    labelled* placeholder output so the whole streaming UI can be developed and
    demonstrated without a key - and never pretends to be a real model.

The client is a thin async wrapper: no SDK, one dependency (``aiohttp``) that the
bot already has, and full control over the streaming loop so tokens can be
forwarded to a Telegram draft as they arrive.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import aiohttp

from ..config import Settings
from ..logging_setup import get_logger

log = get_logger("ai.llm")

__all__ = ["ChatMessage", "DemoGenerator", "LLMClient", "LLMError", "OpenAICompatibleClient", "build_llm"]


class LLMError(RuntimeError):
    """Raised when the model cannot be reached or returns an error."""


@dataclass(slots=True)
class ChatMessage:
    """One message in a conversation."""

    role: str  # "system" | "user" | "assistant"
    content: str

    def as_payload(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class LLMClient:
    """Base class: everything needed to stream an answer."""

    name = "base"

    async def stream(
        self, messages: Sequence[ChatMessage], *, max_tokens: int = 900, temperature: float = 0.7
    ) -> AsyncIterator[str]:
        """Yield text chunks as they are produced."""
        raise NotImplementedError
        yield ""  # pragma: no cover - makes the return type an async generator

    async def complete(
        self, messages: Sequence[ChatMessage], *, max_tokens: int = 900, temperature: float = 0.7
    ) -> str:
        """Collect the whole answer (a convenience wrapper over :meth:`stream`)."""
        return "".join([chunk async for chunk in self.stream(messages, max_tokens=max_tokens, temperature=temperature)])


class OpenAICompatibleClient(LLMClient):
    """Streaming client for ``/chat/completions`` endpoints."""

    name = "openai_compatible"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        timeout: float = 90.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    async def stream(
        self, messages: Sequence[ChatMessage], *, max_tokens: int = 900, temperature: float = 0.7
    ) -> AsyncIterator[str]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.as_payload() for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout, sock_read=45)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                ) as response:
                    if response.status >= 400:
                        body = (await response.text())[:300]
                        raise LLMError(f"{response.status}: {body}")
                    async for raw in response.content:
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            yield piece
        except asyncio.TimeoutError as exc:
            raise LLMError("the model took too long to answer") from exc
        except aiohttp.ClientError as exc:
            raise LLMError(f"could not reach the model: {exc}") from exc


class DemoGenerator(LLMClient):
    """Offline placeholder that makes the streaming UI demonstrable.

    Output is explicitly labelled so nobody mistakes it for a real answer.
    """

    name = "demo"

    def __init__(self, *, delay: float = 0.02) -> None:
        self.delay = delay

    async def stream(
        self, messages: Sequence[ChatMessage], *, max_tokens: int = 900, temperature: float = 0.7
    ) -> AsyncIterator[str]:
        question = next((m.content for m in reversed(messages) if m.role == "user"), "")
        if not question.strip():
            question = "your question"
        answer = (
            f"<b>demo mode</b>\n\n"
            f"I received: <i>{question[:160]}</i>\n\n"
            "No model is configured, so this is a local placeholder that shows the "
            "<b>streaming renderer</b>: text appears in a live draft, then lands as a "
            "rich message with <b>headings</b>, <code>code</code>, tables and buttons.\n\n"
        )
        for word in answer.split(" "):
            yield word + " "
            if self.delay:
                await asyncio.sleep(self.delay)
        yield "\n\n"
        yield "<b>ᴇxᴀᴍᴘʟᴇ ᴛᴀʙʟᴇ</b>\n"
        yield "<table border=\"1\"><tr><th>setting</th><th>value</th></tr>"
        yield "<tr><td>LLM_PROVIDER</td><td>off (demo)</td></tr>"
        yield "<tr><td>LLM_MODEL</td><td>n/a</td></tr></table>\n"
        yield "\n<i>Set LLM_PROVIDER=openai_compatible and LLM_API_KEY to get real answers.</i>"


def build_llm(settings: Settings) -> LLMClient:
    """Pick a client from the configuration."""
    if settings.llm_provider == "off" or settings.llm_api_key is None:
        return DemoGenerator()
    return OpenAICompatibleClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
