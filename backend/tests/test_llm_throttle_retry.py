from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.llm.caller import (
    _complete_with_throttle_retry,
    _provider_slot,
    _stream_with_throttle_retry,
    call_llm,
    call_llm_with_failover,
)
from app.services.llm.client import LLMError, LLMMessage, LLMResponse
from app.services.llm.failure_outcome import LLMFailure


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


def _rate_limit_error(*, retry_after: str | None = None) -> LLMError:
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    return LLMError.from_http(
        429,
        '{"error":{"message":"Request rate increased too quickly","code":"limit_burst_rate"}}',
        headers,
    )


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
    record_usage = AsyncMock(return_value=None)
    monkeypatch.setattr("app.services.llm.caller.record_token_usage", record_usage)
    monkeypatch.setattr(
        "app.services.llm.caller._sleep_before_throttle_retry",
        AsyncMock(return_value=None),
        raising=False,
    )
    return record_usage


@pytest.mark.asyncio
async def test_provider_throttle_is_retried_before_returning_success(monkeypatch):
    client = _ThrottleScriptClient(
        [
            _rate_limit_error(),
            LLMError.from_payload(
                {"error": {"message": "slow down", "type": "rate_limit_error"}}
            ),
            _stop_response("重试后成功"),
        ]
    )
    _patch_call_llm_collaborators(monkeypatch, client)
    statuses: list[dict] = []

    async def collect_status(status: dict) -> None:
        statuses.append(status)

    result = await call_llm_with_failover(
        primary_model=_FakeModel(),
        fallback_model=_FakeModel(id="fallback-model"),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="测试助手",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="",
        on_status=collect_status,
    )

    assert result == "重试后成功"
    assert len(client.stream_calls) == 3
    assert [status["retry_index"] for status in statuses] == [1, 2, 0]
    assert statuses[-1]["state"] == "recovered"
    assert all("on_status" not in request for request in client.stream_calls)
    assert client.closed is True


@pytest.mark.asyncio
async def test_429_after_stream_progress_is_not_replayed(monkeypatch):
    class _PartialThenRateLimitedClient:
        calls = 0
        closed = False

        async def stream(self, **kwargs):
            self.calls += 1
            await kwargs["on_chunk"]("partial")
            raise _rate_limit_error()

        async def close(self):
            self.closed = True

    client = _PartialThenRateLimitedClient()
    _patch_call_llm_collaborators(monkeypatch, client)
    statuses: list[dict] = []

    async def collect_status(status: dict) -> None:
        statuses.append(status)

    result = await call_llm_with_failover(
        primary_model=_FakeModel(),
        fallback_model=_FakeModel(id="fallback-model"),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="测试助手",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="",
        on_chunk=AsyncMock(return_value=None),
        on_status=collect_status,
    )

    assert isinstance(result, LLMFailure)
    assert result.code == "provider_rate_limit_exhausted"
    assert result.details["retry_count"] == 0
    assert result.details["had_provider_progress"] is True
    assert client.calls == 1
    assert statuses == []


@pytest.mark.asyncio
async def test_rate_limit_lane_does_not_stack_with_5xx_or_failover(monkeypatch):
    client = _ThrottleScriptClient(
        [
            _rate_limit_error(),
            LLMError.from_http(503, '{"error":{"message":"temporarily unavailable"}}'),
        ]
    )
    _patch_call_llm_collaborators(monkeypatch, client)
    statuses: list[dict] = []

    async def collect_status(status: dict) -> None:
        statuses.append(status)

    result = await call_llm_with_failover(
        primary_model=_FakeModel(),
        fallback_model=_FakeModel(id="fallback-model"),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="测试助手",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="",
        on_status=collect_status,
    )

    assert isinstance(result, LLMFailure)
    assert result.code == "provider_request_failed"
    assert result.allow_failover is False
    assert len(client.stream_calls) == 2
    assert [status["retry_index"] for status in statuses] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        LLMError(
            "HTTP 400: InternalError.Algo.InvalidParameter: "
            "The provided URL does not appear to be valid"
        ),
        LLMError("backend buffer overflow"),
    ],
)
async def test_whitelisted_provider_error_gets_one_identical_retry(monkeypatch, error):
    client = _ThrottleScriptClient([error, _stop_response("recovered")])
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

    assert result == "recovered"
    assert len(client.stream_calls) == 2
    assert client.stream_calls[0]["messages"] == client.stream_calls[1]["messages"]


@pytest.mark.asyncio
async def test_raw_connection_failure_uses_bounded_identical_retry(monkeypatch):
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    client = _ThrottleScriptClient([httpx.ConnectError("unreachable", request=request), _stop_response("ok")])
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

    assert result == "ok"
    assert len(client.stream_calls) == 2
    assert client.stream_calls[0]["messages"] == client.stream_calls[1]["messages"]


@pytest.mark.asyncio
async def test_raw_connection_failure_stays_typed_when_retries_are_disabled():
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    client = _ThrottleScriptClient([httpx.ConnectError("unreachable", request=request)])

    with pytest.raises(LLMError) as exc_info:
        await _stream_with_throttle_retry(
            client,
            model=_FakeModel(),
            round_i=1,
            messages=[],
            allow_retries=False,
        )

    assert exc_info.value.error_code == "ConnectError"
    assert exc_info.value.error_type == "connection_error"


def test_non_json_http_error_keeps_header_diagnostics():
    error = LLMError.from_http(
        429,
        "upstream overloaded",
        {"x-request-id": "request-123", "retry-after": "7"},
    )

    assert error.request_id == "request-123"
    assert error.retry_after_seconds == 7.0


@pytest.mark.asyncio
async def test_fallback_does_not_start_a_second_retry_lane(monkeypatch):
    primary_client = _ThrottleScriptClient([LLMError("temporary provider failure")])
    fallback_client = _ThrottleScriptClient([_rate_limit_error()])
    _patch_call_llm_collaborators(monkeypatch, primary_client)
    monkeypatch.setattr(
        "app.services.llm.caller.create_llm_client",
        lambda **kwargs: fallback_client if kwargs["model"] == "fallback" else primary_client,
    )

    result = await call_llm_with_failover(
        primary_model=_FakeModel(),
        fallback_model=_FakeModel(id="fallback-model", model="fallback"),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="测试助手",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="",
    )

    assert isinstance(result, LLMFailure)
    assert result.code == "provider_failover_failed"
    assert len(primary_client.stream_calls) == 1
    assert len(fallback_client.stream_calls) == 1


@pytest.mark.asyncio
async def test_second_invalid_tool_batch_closes_provider_client(monkeypatch):
    malformed = {
        "id": "call-invalid",
        "type": "function",
        "function": {"name": "", "arguments": "{}"},
    }
    first = _tool_response(malformed)
    first.usage = {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}
    second = _tool_response(malformed)
    second.usage = {"prompt_tokens": 12, "completion_tokens": 1, "total_tokens": 13}
    client = _ThrottleScriptClient([first, second])
    record_usage = _patch_call_llm_collaborators(monkeypatch, client)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "run tool"}],
        agent_name="测试助手",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="",
    )

    assert isinstance(result, LLMFailure)
    assert result.code == "invalid_tool_call_stream"
    assert len(client.stream_calls) == 2
    assert client.closed is True
    assert record_usage.await_count == 1


@pytest.mark.asyncio
async def test_provider_round_has_no_platform_timeout_and_cancel_closes_client(monkeypatch):
    class _HeartbeatForeverClient:
        closed = False

        async def stream(self, **_kwargs):
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    client = _HeartbeatForeverClient()
    _patch_call_llm_collaborators(monkeypatch, client)
    model = _FakeModel(request_timeout=0.01)

    task = asyncio.create_task(
        call_llm(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="测试助手",
            role_description="",
            agent_id="agent-x",
            user_id="user-x",
            session_id="",
        )
    )

    await asyncio.sleep(0.03)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed is True


@pytest.mark.asyncio
async def test_provider_ttft_is_not_limited_or_retried_by_platform(monkeypatch):
    class _SlowThenSuccessClient:
        def __init__(self):
            self.calls = 0
            self.closed = False

        async def stream(self, **_kwargs):
            self.calls += 1
            await asyncio.sleep(0.03)
            return _stop_response("slow-ok")

        async def close(self):
            self.closed = True

    client = _SlowThenSuccessClient()
    _patch_call_llm_collaborators(monkeypatch, client)

    result = await call_llm(
        model=_FakeModel(request_timeout=0.01),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="测试助手",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="",
    )

    assert result == "slow-ok"
    assert client.calls == 1
    assert client.closed is True


@pytest.mark.asyncio
async def test_meaningful_stream_progress_is_not_time_limited(monkeypatch):
    class _ProgressClient:
        closed = False

        async def stream(self, **kwargs):
            for chunk in ("a", "b", "c"):
                await asyncio.sleep(0.008)
                await kwargs["on_chunk"](chunk)
            return _stop_response("done")

        async def close(self):
            self.closed = True

    client = _ProgressClient()
    _patch_call_llm_collaborators(monkeypatch, client)

    result = await call_llm(
        model=_FakeModel(request_timeout=0.01),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="测试助手",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="",
        on_chunk=AsyncMock(return_value=None),
    )

    assert result == "done"
    assert client.closed is True


@pytest.mark.asyncio
async def test_provider_slot_bounds_parallel_dispatch(monkeypatch):
    active = 0
    maximum = 0

    class _ParallelClient:
        async def stream(self, **_kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _stop_response("done")

    monkeypatch.setenv("LLM_PROVIDER_MAX_IN_FLIGHT", "2")
    model = _FakeModel(request_timeout=1)
    await asyncio.gather(
        *(
            _stream_with_throttle_retry(
                _ParallelClient(),
                model=model,
                round_i=1,
                messages=[],
            )
            for _ in range(4)
        )
    )

    assert maximum == 2


@pytest.mark.asyncio
async def test_background_complete_shares_provider_capacity(monkeypatch):
    active = 0
    maximum = 0

    class _ParallelCompleteClient:
        async def complete(self, **_kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _stop_response("done")

    monkeypatch.setenv("LLM_PROVIDER_MAX_IN_FLIGHT", "2")
    model = _FakeModel(request_timeout=1)
    await asyncio.gather(
        *(
            _complete_with_throttle_retry(
                _ParallelCompleteClient(),
                model=model,
                round_i=1,
                messages=[],
            )
            for _ in range(4)
        )
    )

    assert maximum == 2


@pytest.mark.asyncio
async def test_background_complete_queue_wait_is_outside_request_timeout(monkeypatch):
    class _ImmediateCompleteClient:
        async def complete(self, **_kwargs):
            return _stop_response("done")

    monkeypatch.setenv("LLM_PROVIDER_MAX_IN_FLIGHT", "1")
    model = _FakeModel(request_timeout=0.01)
    provider_slot = _provider_slot(model)
    await provider_slot.acquire()
    queued = asyncio.create_task(
        _complete_with_throttle_retry(
            _ImmediateCompleteClient(),
            model=model,
            round_i=1,
            messages=[],
        )
    )

    await asyncio.sleep(0.02)
    assert not queued.done()
    provider_slot.release()

    response = await queued
    assert response.content == "done"


@pytest.mark.asyncio
async def test_provider_throttle_exhaustion_returns_later_user_message(monkeypatch):
    client = _ThrottleScriptClient([_rate_limit_error()] * 6)
    _patch_call_llm_collaborators(monkeypatch, client)
    statuses: list[dict] = []

    async def collect_status(status: dict) -> None:
        statuses.append(status)

    result = await call_llm_with_failover(
        primary_model=_FakeModel(),
        fallback_model=_FakeModel(id="fallback-model"),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="测试助手",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="",
        on_status=collect_status,
    )

    assert isinstance(result, LLMFailure)
    assert result.code == "provider_rate_limit_exhausted"
    assert result.retryable is False
    assert result.allow_failover is False
    assert result.details["retry_count"] == 5
    assert result.details["total_requests"] == 6
    assert result.details["recovery_action"] == "continue"
    assert "继续" in result
    assert "无需发送 /new" in result
    assert len(client.stream_calls) == 6
    assert [status["retry_index"] for status in statuses] == [1, 2, 3, 4, 5]
    assert client.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (
            429,
            '{"error":{"message":"You exceeded your current quota",'
            '"code":"insufficient_quota","type":"billing_error"}}',
        ),
        (401, '{"error":{"message":"Invalid API key","code":"invalid_api_key"}}'),
    ],
)
async def test_permanent_provider_failures_are_not_retried(
    monkeypatch,
    status_code,
    body,
):
    client = _ThrottleScriptClient(
        [
            LLMError.from_http(status_code, body)
        ]
    )
    _patch_call_llm_collaborators(monkeypatch, client)
    statuses: list[dict] = []

    async def collect_status(status: dict) -> None:
        statuses.append(status)

    result = await call_llm_with_failover(
        primary_model=_FakeModel(),
        fallback_model=_FakeModel(id="fallback-model"),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="测试助手",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="",
        on_status=collect_status,
    )

    assert isinstance(result, LLMFailure)
    assert result.code == "provider_request_failed"
    assert result.allow_failover is False
    assert len(client.stream_calls) == 1
    assert statuses == []


@pytest.mark.asyncio
async def test_retry_after_and_payload_snapshot_are_preserved(monkeypatch):
    original_messages = [LLMMessage(role="user", content="hello")]

    class _MutatingClient:
        def __init__(self):
            self.requests = []

        async def stream(self, **kwargs):
            self.requests.append(copy.deepcopy(kwargs))
            if len(self.requests) == 1:
                kwargs["messages"][0].content = "mutated"
                raise _rate_limit_error(retry_after="3")
            return _stop_response("ok")

    client = _MutatingClient()
    sleeps = AsyncMock(return_value=None)
    monkeypatch.setattr("app.services.llm.caller._sleep_before_throttle_retry", sleeps)
    statuses: list[dict] = []

    async def collect_status(status: dict) -> None:
        statuses.append(status)

    response = await _stream_with_throttle_retry(
        client,
        model=_FakeModel(),
        round_i=1,
        messages=original_messages,
        on_status=collect_status,
    )

    assert response.content == "ok"
    assert client.requests[0]["messages"] == client.requests[1]["messages"]
    assert sleeps.await_args.args == (1.0,)
    assert statuses[0]["delay_seconds"] == 1.0
    assert statuses[-1]["state"] == "recovered"


@pytest.mark.asyncio
async def test_call_llm_persists_complete_running_plan_before_first_execution(monkeypatch):
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
            assert persist_batches == [[
                ("read_file", "running"),
                ("list_sessions", "running"),
            ]]
            assert [(e["name"], e["status"]) for e in events] == [
                ("read_file", "running"),
                ("list_sessions", "running"),
            ]
        else:
            assert persist_batches == [
                [("read_file", "running"), ("list_sessions", "running")],
                [("read_file", "done")],
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
