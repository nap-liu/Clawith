"""P1-A message layout contract: stable prefix for Anthropic prompt cache
and Qwen/DashScope automatic prefix cache.

What this test locks in:
  1. ``messages[0]`` is ``system`` and its ``content`` is the *static* prompt only
     — no "Current Time" or other per-call dynamic bits leak into system.
  2. ``messages[0].dynamic_content is None`` — no back-door dynamic injection
     via the old OpenAI ``to_openai_format`` path.
  3. The *last* ``user`` message sent this round is wrapped as
     ``<context>\n{dynamic}\n</context>\n\n{original_user_input}``.
  4. *Historical* user messages (earlier turns) are NOT wrapped — they retain
     their original append-only content.
  5. ``tools`` passed to ``client.stream`` are sorted by ``function.name`` so
     ``tools[-1]`` (where Anthropic puts cache_control) is stable per agent.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.services.llm.caller import call_llm
from app.services.llm.client import LLMMessage, LLMResponse


class _FakeClient:
    """Captures the arguments to ``stream`` / ``close`` for inspection."""

    def __init__(self) -> None:
        self.stream_calls: list[dict[str, Any]] = []
        self.closed = False

    async def stream(
        self,
        messages,
        tools=None,
        temperature=None,
        max_tokens=None,
        on_chunk=None,
        on_thinking=None,
        **kwargs,
    ):
        # Deep-snapshot inputs: the caller may mutate api_messages after this
        # call, but the dispatched list should already be fully-formed.
        self.stream_calls.append({
            "messages": list(messages),
            "tools": list(tools) if tools else None,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        # finish() is the only clean stop signal under the merged
        # finish-protocol loop: a plain-text response no longer ends the
        # turn (the loop would inject FINISH_PROTOCOL_REMINDER and re-stream).
        # Returning a valid finish() makes caller.call_llm return its content
        # and exit after exactly one round. (See test_finish_protocol.)
        return LLMResponse(
            content="",
            tool_calls=[
                {
                    "id": "call_finish",
                    "type": "function",
                    "function": {
                        "name": "finish",
                        "arguments": json.dumps({"content": "ok-final"}),
                    },
                }
            ],
            finish_reason="tool_calls",
            usage=None,
        )

    async def close(self):
        self.closed = True


class _FakeModel:
    provider = "qwen"
    model = "qwen-turbo"
    base_url = "https://example.invalid"
    api_key_encrypted = ""
    temperature = 0.7
    max_output_tokens = None
    request_timeout = 30.0
    id = "model-x"


@pytest.mark.asyncio
async def test_message_layout_and_tool_sort(monkeypatch):
    fake_client = _FakeClient()

    # Patch all external collaborators the caller touches before ``stream``.
    monkeypatch.setattr(
        "app.services.llm.caller.create_llm_client",
        lambda **kwargs: fake_client,
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_max_tokens",
        lambda *args, **kwargs: 1024,
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_model_api_key",
        lambda model: "fake-key",
    )
    # Config lookup would hit the DB — short-circuit it.
    monkeypatch.setattr(
        "app.services.llm.caller._get_agent_config",
        AsyncMock(return_value=(50, None)),
    )
    monkeypatch.setattr(
        "app.services.llm.caller._get_user_name",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("STATIC", "DYN-CONTENT")),
    )
    # get_agent_tools_for_llm is imported at top of caller.py → patch there.
    # Return tools in non-alphabetical order so we can verify the sort.
    tools_raw = [
        {"type": "function", "function": {"name": "write_file",  "description": "w"}},
        {"type": "function", "function": {"name": "read_file",   "description": "r"}},
        {"type": "function", "function": {"name": "grep",        "description": "g"}},
    ]
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(return_value=tools_raw),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.record_token_usage",
        AsyncMock(return_value=None),
    )

    # Conversation history: one earlier user → assistant exchange, then the
    # fresh user turn we're about to send. This exercises the "only wrap the
    # LAST user" rule.
    history = [
        {"role": "user",      "content": "earlier-user-question"},
        {"role": "assistant", "content": "earlier-assistant-reply"},
        {"role": "user",      "content": "current-user-question"},
    ]

    result = await call_llm(
        model=_FakeModel(),
        messages=history,
        agent_name="TestAgent",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="sess-1",
    )

    assert result == "ok-final"
    assert fake_client.closed is True
    assert len(fake_client.stream_calls) == 1

    call = fake_client.stream_calls[0]
    sent: list[LLMMessage] = call["messages"]

    # ── Assertion 1: system has ONLY the static prompt ────────────────────
    assert sent[0].role == "system"
    assert sent[0].content == "STATIC"
    assert "DYN-CONTENT" not in (sent[0].content or "")
    # Assertion 2: no legacy dynamic_content backdoor
    assert sent[0].dynamic_content is None

    # ── Assertion 3: last user wrapped with <context>…</context> ──────────
    last_user = sent[-1]
    assert last_user.role == "user"
    assert isinstance(last_user.content, str)
    assert last_user.content.startswith("<context>\n")
    assert "\n</context>\n\n" in last_user.content
    assert last_user.content.endswith("current-user-question")
    assert "DYN-CONTENT" in last_user.content

    # ── Assertion 4: historical user message stays raw ────────────────────
    # Layout: [system, user(earlier, raw), assistant, user(current, wrapped)]
    earlier_user = sent[1]
    assert earlier_user.role == "user"
    assert earlier_user.content == "earlier-user-question"
    assert "<context>" not in (earlier_user.content or "")

    # ── Assertion 5: tools sorted by function.name ────────────────────────
    tool_names = [t["function"]["name"] for t in call["tools"]]
    assert tool_names == sorted(tool_names)
    assert tool_names == ["grep", "read_file", "write_file"]


@pytest.mark.asyncio
async def test_api_messages_remain_append_only_across_rounds(monkeypatch):
    """Even after the dispatched messages are wrapped, the caller's internal
    api_messages list (used as the growing history) must keep the original
    raw user content — otherwise the next round's prefix bytes change."""
    # Client that returns no tool calls -> loop exits after one round; we can
    # still reach into caller state via the dispatched messages snapshot and
    # compare against what the caller would use next round. For that we need
    # a client that captures its inputs.
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "app.services.llm.caller.create_llm_client",
        lambda **kwargs: fake_client,
    )
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *a, **k: 1024)
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda m: "k")
    monkeypatch.setattr(
        "app.services.llm.caller._get_agent_config",
        AsyncMock(return_value=(50, None)),
    )
    monkeypatch.setattr(
        "app.services.llm.caller._get_user_name", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("STATIC", "DYN")),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.record_token_usage", AsyncMock(return_value=None),
    )

    history = [{"role": "user", "content": "raw-input"}]
    await call_llm(
        model=_FakeModel(),
        messages=history,
        agent_name="T", role_description="",
        agent_id="agent-x", user_id="user-x", session_id="s",
    )

    # Dispatched message had the wrapper…
    sent = fake_client.stream_calls[0]["messages"]
    last_user_sent = sent[-1]
    assert last_user_sent.content.startswith("<context>\n")
    assert last_user_sent.content.endswith("raw-input")

    # …but the original history dict that the caller received is untouched
    # (caller builds LLMMessage copies, never mutates caller-provided list).
    assert history[-1]["content"] == "raw-input"
