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

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.llm.caller import call_llm
from app.services.llm.client import LLMResponse
from app.services.token_tracker import TokenUsage


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
    monkeypatch.setattr(
        "app.services.llm.caller._persist_tool_call_events_strict",
        AsyncMock(return_value=True),
    )


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
async def test_late_round_message_interrupts_plain_reply_in_same_turn(monkeypatch):
    client = _ScriptedClient([
        LLMResponse(content="first answer", finish_reason="stop"),
        LLMResponse(content="answer after interruption", finish_reason="stop"),
    ])
    _patch_collaborators(monkeypatch, client)
    drain_calls: list[int] = []

    async def _before_round(round_i: int):
        drain_calls.append(round_i)
        if round_i == 1:
            return [{"role": "user", "content": "late parent message"}]
        return []

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "start"}],
        agent_name="T",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="s",
        before_round=_before_round,
    )

    assert result == "first answer\n\nanswer after interruption"
    assert len(client.stream_calls) == 2
    second_round = client.stream_calls[1]["messages"]
    assert [(row.role, row.content) for row in second_round[-2:]] == [
        ("assistant", "first answer"),
        ("user", "late parent message"),
    ]
    assert drain_calls == [0, 1, 2]


@pytest.mark.asyncio
async def test_final_allowed_round_does_not_claim_late_message(monkeypatch):
    client = _ScriptedClient([
        LLMResponse(content="final allowed answer", finish_reason="stop"),
    ])
    _patch_collaborators(monkeypatch, client)
    drain_calls: list[int] = []

    async def _before_round(round_i: int):
        drain_calls.append(round_i)
        if round_i == 1:
            return [{"role": "user", "content": "must stay pending"}]
        return []

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "start"}],
        agent_name="T",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="s",
        before_round=_before_round,
        max_tool_rounds_override=1,
    )

    assert result == "final allowed answer"
    assert len(client.stream_calls) == 1
    assert drain_calls == [0]


@pytest.mark.asyncio
async def test_plain_text_reports_normalized_usage_to_callback(monkeypatch):
    client = _ScriptedClient([
        LLMResponse(
            content="直接回答",
            finish_reason="stop",
            usage={
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
                "prompt_tokens_details": {"cached_tokens": 700},
            },
        ),
    ])
    _patch_collaborators(monkeypatch, client)
    captured = TokenUsage()

    async def _on_usage(usage: TokenUsage):
        captured.add(usage)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "hi"}],
        agent_name="T",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="s",
        on_usage=_on_usage,
    )

    assert result == "直接回答"
    assert captured.total_tokens == 1200
    assert captured.input_tokens == 1000
    assert captured.output_tokens == 200
    assert captured.cache_read_tokens == 700
    assert captured.cache_eligible_input_tokens == 1000


@pytest.mark.asyncio
async def test_tool_round_then_plain_text(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    client = _ScriptedClient([
        LLMResponse(
            content="我先读取文件。",
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

    assert result == "我先读取文件。\n\n文件里写着 hello"
    assert len(client.stream_calls) == 2, "tool round + answer round, nothing more"


@pytest.mark.asyncio
async def test_cancel_check_runs_before_tool_side_effect(monkeypatch):
    client = _ScriptedClient([
        LLMResponse(
            content="准备执行。",
            tool_calls=[{
                "id": "call_cancelled",
                "type": "function",
                "function": {"name": "write_file", "arguments": '{"path": "x"}'},
            }],
            finish_reason="tool_calls",
        ),
    ])
    _patch_collaborators(monkeypatch, client, tools=[
        {"type": "function", "function": {"name": "write_file", "description": "w"}},
    ])
    executed = False

    async def _must_not_execute(*_args, **_kwargs):
        nonlocal executed
        executed = True
        raise AssertionError("cancelled subagent must not start a tool side effect")

    async def _cancelled():
        raise asyncio.CancelledError

    monkeypatch.setattr("app.services.llm.caller.execute_tool", _must_not_execute)

    with pytest.raises(asyncio.CancelledError):
        await call_llm(
            model=_FakeModel(),
            messages=[{"role": "user", "content": "write"}],
            agent_name="T",
            role_description="",
            agent_id="agent-x",
            user_id="user-x",
            session_id="s",
            before_tool_execution=_cancelled,
        )

    assert executed is False


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


@pytest.mark.asyncio
async def test_tool_round_content_becomes_confirmation_intro(monkeypatch):
    client = _ScriptedClient([
        LLMResponse(
            content="这是知识库完整答案。",
            tool_calls=[{
                "id": "lookup-1",
                "type": "function",
                "function": {"name": "knowledge_search", "arguments": '{"query":"seal"}'},
            }],
            finish_reason="tool_calls",
        ),
        LLMResponse(
            content="",
            tool_calls=[{
                "id": "confirm-1",
                "type": "function",
                "function": {
                    "name": "request_confirmation",
                    "arguments": (
                        '{"title":"是否解决","summary":"请确认",'
                        '"buttons":[{"text":"已解决","value":"resolved"}]}'
                    ),
                },
            }],
            finish_reason="tool_calls",
        ),
    ])
    _patch_collaborators(monkeypatch, client, tools=[
        {"type": "function", "function": {"name": "knowledge_search", "description": "search"}},
        {"type": "function", "function": {"name": "request_confirmation", "description": "confirm"}},
    ])

    async def _fake_tool(tool_name, args, **kwargs):
        return "matched"

    suspend = AsyncMock(return_value="confirmation-row")
    monkeypatch.setattr("app.services.llm.caller.execute_tool", _fake_tool)
    monkeypatch.setattr(
        "app.services.llm.caller._persist_tool_call_events_strict",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.confirmation_service.suspend_for_confirmation",
        suspend,
    )

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "查询后让我确认"}],
        agent_name="T",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="s",
    )

    assert result == ""
    assert suspend.await_args.kwargs["intro_text"] == "这是知识库完整答案。"
