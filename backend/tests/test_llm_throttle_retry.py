from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.llm.caller import call_llm
from app.services.llm.client import LLMError, LLMResponse


class _ThrottleScriptClient:
    def __init__(self, script: list[LLMError | LLMResponse]) -> None:
        self._script = list(script)
        self.stream_calls: list[dict[str, Any]] = []
        self.closed = False

    async def stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        if not self._script:
            raise AssertionError("script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        self.closed = True


class _FakeModel(SimpleNamespace):
    provider = "qwen"
    model = "qwen3.5-plus"
    base_url = "https://dashscope.invalid"
    api_key_encrypted = ""
    temperature = 0.7
    max_output_tokens = None
    request_timeout = 30.0
    id = "model-x"


def _stop_response(content: str) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="stop")


def _tool_response(*tool_calls: dict) -> LLMResponse:
    return LLMResponse(content="", tool_calls=list(tool_calls), finish_reason="tool_calls")


def _patch_call_llm_collaborators(monkeypatch, client):
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **_kwargs: client)
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *_args, **_kwargs: 1024)
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "fake-key")
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(50, None)))
    monkeypatch.setattr("app.services.llm.caller._get_user_name", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("STATIC", "DYN")),
    )
    monkeypatch.setattr("app.services.llm.caller.get_agent_tools_for_llm", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.services.llm.caller.record_token_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.llm.caller._sleep_before_throttle_retry",
        AsyncMock(return_value=None),
        raising=False,
    )


@pytest.mark.asyncio
async def test_provider_throttle_is_retried_before_returning_success(monkeypatch):
    client = _ThrottleScriptClient(
        [
            LLMError(
                "HTTP 429: {\"error\":{\"message\":\"Request rate increased too quickly\","
                "\"code\":\"limit_burst_rate\"}}"
            ),
            LLMError(
                "HTTP 500: {\"error\":{\"message\":\"<503> Too many requests. "
                "Your requests are being throttled due to system capacity limits\","
                "\"code\":\"ServiceUnavailable\"}}"
            ),
            _stop_response("重试后成功"),
        ]
    )
    _patch_call_llm_collaborators(monkeypatch, client)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="测试助手",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="",
    )

    assert result == "重试后成功"
    assert len(client.stream_calls) == 3
    assert client.closed is True


@pytest.mark.asyncio
async def test_provider_throttle_exhaustion_returns_later_user_message(monkeypatch):
    client = _ThrottleScriptClient(
        [
            LLMError(
                "HTTP 429: {\"error\":{\"message\":\"Request rate increased too quickly\","
                "\"code\":\"limit_burst_rate\"}}"
            ),
            LLMError(
                "HTTP 429: {\"error\":{\"message\":\"Request rate increased too quickly\","
                "\"code\":\"limit_burst_rate\"}}"
            ),
            LLMError(
                "HTTP 429: {\"error\":{\"message\":\"Request rate increased too quickly\","
                "\"code\":\"limit_burst_rate\"}}"
            ),
        ]
    )
    _patch_call_llm_collaborators(monkeypatch, client)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="测试助手",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="",
    )

    assert not result.startswith("[LLM Error]")
    assert "稍后再试" in result
    assert len(client.stream_calls) == 3
    assert client.closed is True


@pytest.mark.asyncio
async def test_call_llm_persists_all_running_tool_markers_before_any_execution(monkeypatch):
    client = _ThrottleScriptClient(
        [
            _tool_response(
                {
                    "id": "call_read",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                },
                {
                    "id": "call_list",
                    "type": "function",
                    "function": {"name": "list_sessions", "arguments": '{"query": "刘喜"}'},
                },
            ),
            _stop_response("done"),
        ]
    )
    _patch_call_llm_collaborators(monkeypatch, client)
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(
            return_value=[
                {"type": "function", "function": {"name": "read_file", "parameters": {}}},
                {"type": "function", "function": {"name": "list_sessions", "parameters": {}}},
            ]
        ),
    )

    events: list[dict] = []
    executed: list[str] = []
    persist_batches: list[list[tuple[str, str]]] = []

    async def fake_persist_tool_events(events_to_persist: list[dict], **_kwargs):
        persist_batches.append([(evt["name"], evt["status"]) for evt in events_to_persist])
        return True

    async def fake_on_tool_call(evt: dict):
        events.append(evt)

    async def fake_execute_tool(name, args, **_kwargs):
        if not executed:
            assert persist_batches[0] == [
                ("read_file", "running"),
                ("list_sessions", "running"),
            ]
            assert [(e["name"], e["status"]) for e in events] == [
                ("read_file", "running"),
                ("list_sessions", "running"),
            ]
        executed.append(name)
        return f"{name} result"

    monkeypatch.setattr("app.services.llm.caller._persist_tool_call_events_strict", fake_persist_tool_events)
    monkeypatch.setattr("app.services.llm.caller.execute_tool", fake_execute_tool)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "run two tools"}],
        agent_name="测试助手",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="sess-x",
        on_tool_call=fake_on_tool_call,
    )

    assert result == "done"
    assert executed == ["read_file", "list_sessions"]
    assert [(e["name"], e["status"]) for e in events] == [
        ("read_file", "running"),
        ("list_sessions", "running"),
        ("read_file", "done"),
        ("list_sessions", "done"),
    ]
    assert persist_batches == [
        [("read_file", "running"), ("list_sessions", "running")],
        [("read_file", "done")],
        [("list_sessions", "done")],
    ]


@pytest.mark.asyncio
async def test_call_llm_does_not_execute_tool_when_running_marker_cannot_persist(monkeypatch):
    client = _ThrottleScriptClient(
        [
            _tool_response(
                {
                    "id": "call_read",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                },
            ),
        ]
    )
    _patch_call_llm_collaborators(monkeypatch, client)
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(return_value=[{"type": "function", "function": {"name": "read_file", "parameters": {}}}]),
    )

    execute_tool = AsyncMock(return_value="should not run")

    async def fail_persist(*_args, **_kwargs):
        raise RuntimeError("durable failed")

    monkeypatch.setattr("app.services.llm.caller._persist_tool_call_events_strict", fail_persist)
    monkeypatch.setattr("app.services.llm.caller.execute_tool", execute_tool)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "run tool"}],
        agent_name="测试助手",
        role_description="",
        agent_id="00000000-0000-0000-0000-000000000001",
        user_id="00000000-0000-0000-0000-000000000002",
        session_id="sess-x",
    )

    assert result.startswith("[LLM call error]")
    assert "durable failed" in result
    execute_tool.assert_not_awaited()
