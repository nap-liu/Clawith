"""Unified LLM client for multiple providers.

Supports OpenAI-compatible APIs, Anthropic native API, and streaming/non-streaming modes.
Provides a consistent interface for all LLM operations across the application.
"""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from loguru import logger

# ============================================================================
# Data Models
# ============================================================================


class LLMClientCloseGuard:
    """Close a provider client once, including when its owner task is cancelled."""

    def __init__(self, client: Any):
        self._client = client
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._owner_task = asyncio.current_task()
        if self._owner_task is not None:
            self._owner_task.add_done_callback(self._on_owner_done)

    async def close(self) -> None:
        if self._closed:
            return
        await asyncio.shield(self._ensure_close_task())

    def _ensure_close_task(self) -> asyncio.Task[None]:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
            self._close_task.add_done_callback(self._on_close_done)
        return self._close_task

    async def _close(self) -> None:
        try:
            await self._client.close()
        except Exception as exc:  # noqa: BLE001 - cleanup must never mask turn result
            logger.warning("[LLM] client close failed (ignored): {}", exc)

    def _on_close_done(self, _task: asyncio.Task[None]) -> None:
        self._closed = True
        if self._owner_task is not None:
            self._owner_task.remove_done_callback(self._on_owner_done)
            self._owner_task = None

    def _on_owner_done(self, _task: asyncio.Task) -> None:
        if self._closed:
            return
        try:
            self._ensure_close_task()
        except RuntimeError:
            pass


@dataclass
class LLMMessage:
    """Unified message format."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    dynamic_content: str | None = None

    def to_openai_format(self) -> dict:
        """Convert to OpenAI format."""
        msg: dict[str, Any] = {"role": self.role}
        
        content = self.content
        if self.role == "system" and self.dynamic_content:
            content = f"{content}\n\n{self.dynamic_content}"
            
        if content is not None:
            msg["content"] = content
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        if self.reasoning_content:
            msg["reasoning_content"] = self.reasoning_content
        return msg

    def to_anthropic_format(self) -> dict | None:
        """Convert to Anthropic format (returns None for system messages)."""
        if self.role == "system":
            return None
            
        role = self.role
        
        # Tool response (from user to assistant)
        if role == "tool":
            # Build tool_result content: support both string and vision array formats
            if isinstance(self.content, list):
                # Vision content array: extract text parts and image parts
                # Anthropic tool_result content supports [{type: "text", text: ...}, {type: "image", source: ...}]
                tool_content_blocks = []
                for part in self.content:
                    if part.get("type") == "text":
                        tool_content_blocks.append({"type": "text", "text": part.get("text", "")})
                    elif part.get("type") == "image_url":
                        # Convert OpenAI image_url format to Anthropic image source format
                        img_url = part.get("image_url", {}).get("url", "")
                        if img_url.startswith("data:image/"):
                            # Parse data URL: data:image/jpeg;base64,xxxxx
                            header, b64_data = img_url.split(",", 1)
                            media_type = header.split(":")[1].split(";")[0]  # e.g. image/jpeg
                            tool_content_blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64_data,
                                }
                            })
                result_content = tool_content_blocks if tool_content_blocks else (self.content or "")
            else:
                result_content = self.content or ""
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": self.tool_call_id,
                        "content": result_content,
                    }
                ]
            }
            
        content_blocks = []
        
        # Add reasoning/thinking content if present
        if self.role == "assistant" and self.reasoning_content:
            content_blocks.append({
                "type": "thinking",
                "thinking": self.reasoning_content,
                "signature": self.reasoning_signature or "synthetic_signature" 
            })

        if self.content:
            if isinstance(self.content, list):
                for part in self.content:
                    if part.get("type") == "text":
                        content_blocks.append({"type": "text", "text": part.get("text", "")})
                    elif part.get("type") == "image_url":
                        img_url = part.get("image_url", {}).get("url", "")
                        if img_url.startswith("data:image/"):
                            header, b64_data = img_url.split(",", 1)
                            media_type = header.split(":")[1].split(";")[0]
                            content_blocks.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": b64_data,
                                }
                            })
            else:
                content_blocks.append({"type": "text", "text": self.content})
            
        # Tool requests (from assistant to user)
        if self.tool_calls:
            for tc in self.tool_calls:
                function_call = tc.get("function", {})
                args = function_call.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                
                content_blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": function_call.get("name", ""),
                    "input": args
                })
                
        # Handle the structure
        if len(content_blocks) == 1 and content_blocks[0]["type"] == "text":
            content = content_blocks[0]["text"]
        else:
            content = content_blocks

        return {"role": role, "content": content}


@dataclass
class LLMResponse:
    """Unified response format."""

    content: str
    tool_calls: list[dict] = field(default_factory=list)
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    model: str | None = None


@dataclass
class LLMStreamChunk:
    """Stream chunk format."""

    content: str = ""
    reasoning_content: str = ""
    tool_call: dict | None = None
    finish_reason: str | None = None
    is_finished: bool = False
    usage: dict | None = None


# ============================================================================
# Type Definitions
# ============================================================================

ChunkCallback = Callable[[str], Coroutine[Any, Any, None]]
ToolCallback = Callable[[dict], Coroutine[Any, Any, None]]
ThinkingCallback = Callable[[str], Coroutine[Any, Any, None]]


# ============================================================================
# Base Client Interface
# ============================================================================

class LLMClient(ABC):
    """Abstract base class for LLM clients."""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        provider_managed_timeout: bool = False,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.provider_managed_timeout = provider_managed_timeout

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a completion request and return the full response."""
        pass

    @abstractmethod
    async def stream(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_chunk: ChunkCallback | None = None,
        on_tool_delta: ToolCallback | None = None,
        on_thinking: ThinkingCallback | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send a streaming request and return the aggregated response."""
        pass

    @abstractmethod
    def _get_headers(self) -> dict[str, str]:
        """Get request headers."""
        pass


# ============================================================================
# OpenAI-Compatible Client
# ============================================================================


def _httpx_timeout(timeout: float, *, provider_managed_timeout: bool) -> httpx.Timeout:
    """Bound transport setup only; never time-limit model generation/reads.

    ``provider_managed_timeout`` remains in the public constructor for backward
    compatibility but no longer changes read behavior. Every LLM call — normal
    turns, compaction, model checks and background runs — ends only when the
    provider/transport ends it or the caller explicitly cancels it.
    """
    return httpx.Timeout(
        connect=timeout,
        read=None,
        write=timeout,
        pool=timeout,
    )


def _is_markable_payload_msg(msg: dict) -> bool:
    """A payload message can carry cache_control iff it has text content to
    attach the marker to.

    Plain-string content counts (it gets wrapped into a ``[{type:text,...}]``
    block at apply time); list content counts iff it holds at least one
    non-empty text block. assistant turns whose content is ``None``/empty
    (tool-call-only) are NOT markable and must fall back to a neighbour.
    """
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            for b in content
        )
    return False


def select_cache_breakpoints(messages_payload: list[dict]) -> list[int]:
    """Single source of truth for DashScope explicit-cache breakpoints.

    Returns the indices of messages whose last text block should carry
    ``cache_control``. Correct-by-construction invariant: when the message
    list ends in a tool / assistant-tool_call tail (i.e. mid tool-loop), the
    returned set ALWAYS contains a breakpoint at or after the last *stable*
    message — so the growing tool-loop tail enters the cache instead of being
    re-prefilled every round (the root cause of "responses get slower as the
    conversation grows").

    Strategy (<=4 markers, DashScope's per-request limit):

    * **system prefix** (index 0) when present.
    * **last user message** — locks the large pre-loop prefix; survives tail
      churn across rounds.
    * **last markable message** walking back from the end — advances the
      breakpoint to the freshly-appended tail each round (the heart of the
      fix). When the final message is a content-less assistant tool-call turn,
      this naturally lands on the preceding tool result.
    """
    n = len(messages_payload)
    if n == 0:
        return []
    marks: set[int] = set()

    has_system = messages_payload[0].get("role") == "system"
    if has_system:
        marks.add(0)

    # last user message (mid-conversation anchor)
    for i in range(n - 1, -1, -1):
        if messages_payload[i].get("role") == "user":
            marks.add(i)
            break

    # last markable message overall (advances to the tool-loop tail)
    for i in range(n - 1, -1, -1):
        if _is_markable_payload_msg(messages_payload[i]):
            marks.add(i)
            break

    ordered = sorted(marks)
    # DashScope allows at most 4 cache breakpoints per request. Keep the most
    # valuable: the system prefix (if any) plus the markers nearest the end.
    if len(ordered) > 4:
        head = [ordered[0]] if has_system else []
        tail = ordered[len(ordered) - (4 - len(head)):]
        ordered = sorted(set(head + tail))
    return ordered


def _observable_messages_payload(messages_payload: list[dict]) -> list[dict]:
    """Keep the request diagnosable while masking explicit credential fields."""
    from app.utils.sanitize import sanitize_sensitive_values

    observable = sanitize_sensitive_values(messages_payload)
    for message in observable:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            decoded = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            continue
        message["content"] = json.dumps(
            sanitize_sensitive_values(decoded), ensure_ascii=False, default=str
        )
    return observable

class LLMError(Exception):
    """Base provider error with structured HTTP/code evidence when available."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        error_type: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.error_type = error_type

    @classmethod
    def from_http(cls, status_code: int, body: str) -> "LLMError":
        """Parse common provider envelopes without relying on display text."""
        error_code: str | None = None
        error_type: str | None = None
        provider_message = ""
        try:
            payload = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            sources = [error, payload] if isinstance(error, dict) else [payload]
            for source in sources:
                if error_code is None and source.get("code") is not None:
                    error_code = str(source["code"])
                if error_type is None and source.get("type") is not None:
                    error_type = str(source["type"])
                if not provider_message and source.get("message") is not None:
                    provider_message = str(source["message"])
        rendered = provider_message or str(body or "")[:500]
        return cls(
            f"HTTP {status_code}: {rendered}",
            status_code=status_code,
            error_code=error_code,
            error_type=error_type,
        )

__all__ = [name for name in globals() if not name.startswith("__")]
