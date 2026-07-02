"""Plain-text stop contract for the unified tool-calling loop.

A response WITHOUT tool calls ends the turn and its content IS the reply.
This locks in the removal of the v1.10 finish-tool protocol, which forced the
model to re-emit the entire answer inside `finish(content=...)` — on prod that
doubled the latency of every answer-bearing round (measured 105s turns where
~44s was pure re-generation).

Cases:
  A. Plain text on round 1 → returned directly, exactly one stream call.
  B. Tool-call round then plain text → tool executes, round-2 text returned.
  C. Content chunks are streamed through the caller's on_chunk (no buffering).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.llm.caller import call_llm
from app.services.llm.client import LLMResponse


class _ScriptedClient:
    """Fake LLM client: pops pre-scripted responses, forwards content to on_chunk."""

    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
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
        self.stream_calls.append({
            "messages": list(messages),
            "tools": list(tools) if tools else None,
            "on_chunk": on_chunk,
        })
        if not self._script:
            raise AssertionError("ScriptedClient ran out of responses")
        response = self._script.pop(0)
        if on_chunk and response.content:
            await on_chunk(response.content)
        return response

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


def _patch_collaborators(monkeypatch, client, tools=None):
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **kwargs: client)
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *a, **k: 1024)
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda m: "fake-key")
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(50, None)))
    monkeypatch.setattr("app.services.llm.caller._get_user_name", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("STATIC", "DYN")),
    )
    monkeypatch.setattr("app.services.llm.caller.get_agent_tools_for_llm", AsyncMock(return_value=tools or []))
    monkeypatch.setattr("app.services.llm.caller.record_token_usage", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_plain_text_ends_turn_after_one_round(monkeypatch):
    client = _ScriptedClient([
        LLMResponse(content="直接回答", finish_reason="stop"),
    ])
    _patch_collaborators(monkeypatch, client)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "hi"}],
        agent_name="T", role_description="",
        agent_id="agent-x", user_id="user-x", session_id="s",
    )

    assert result == "直接回答"
    assert len(client.stream_calls) == 1, "plain text must stop the loop — no reminder round"
    assert client.closed is True


@pytest.mark.asyncio
async def test_tool_round_then_plain_text(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    client = _ScriptedClient([
        LLMResponse(
            content="",
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
            }],
            finish_reason="tool_calls",
        ),
        LLMResponse(content="文件里写着 hello", finish_reason="stop"),
    ])
    _patch_collaborators(monkeypatch, client, tools=[
        {"type": "function", "function": {"name": "read_file", "description": "r"}},
    ])

    async def _fake_tool(tool_name, args, **kwargs):
        return "hello"

    monkeypatch.setattr("app.services.llm.caller.execute_tool", _fake_tool)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "read a.txt"}],
        agent_name="T", role_description="",
        agent_id="agent-x", user_id="user-x", session_id="s",
    )

    assert result == "文件里写着 hello"
    assert len(client.stream_calls) == 2, "tool round + answer round, nothing more"


@pytest.mark.asyncio
async def test_content_streams_through_on_chunk(monkeypatch):
    client = _ScriptedClient([
        LLMResponse(content="流式内容", finish_reason="stop"),
    ])
    _patch_collaborators(monkeypatch, client)

    received: list[str] = []

    async def _on_chunk(text: str):
        received.append(text)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "hi"}],
        agent_name="T", role_description="",
        agent_id="agent-x", user_id="user-x", session_id="s",
        on_chunk=_on_chunk,
    )

    assert result == "流式内容"
    assert received == ["流式内容"], "caller must pass on_chunk through — no chunk buffering"
