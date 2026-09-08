"""Provider token throttling recovers without replaying completed tools."""

import copy
from unittest.mock import AsyncMock

import pytest
from test_llm_throttle_retry import (
    _FakeModel,
    _patch_call_llm_collaborators,
    _stop_response,
    _ThrottleScriptClient,
    _tool_response,
)

from app.services.llm.caller import call_llm_with_failover
from app.services.llm.client import LLMError, LLMMessage
from app.services.llm.failure_outcome import LLMFailure
from app.services.llm.provider_retry import _complete_with_throttle_retry

TOKEN_LIMIT_MESSAGE = (
    "You exceeded your current quota, please check your plan and billing details. "
    "For details, see: https://help.aliyun.com/zh/model-studio/error-code#token-limit"
)
DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _token_limit(*, code="insufficient_quota", status=429, error_type=None):
    return LLMError.from_payload(
        {"error": {"message": TOKEN_LIMIT_MESSAGE, "code": code, "type": error_type}},
        status_code=status,
    )


@pytest.mark.asyncio
async def test_token_429_after_tool_execution_retries_only_current_request(monkeypatch):
    client = _ThrottleScriptClient([
        _tool_response({
            "id": "read-once",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"report.txt"}'},
        }),
        _token_limit(),
        _stop_response("done"),
    ])
    _patch_call_llm_collaborators(monkeypatch, client)
    execute = AsyncMock(return_value="already completed tool result")
    monkeypatch.setattr("app.services.llm.caller.execute_tool", execute)
    monkeypatch.setattr(
        "app.services.llm.caller._persist_tool_call_events_strict",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(return_value=[{
            "type": "function", "function": {"name": "read_file", "parameters": {}},
        }]),
    )
    status = AsyncMock()
    result = await call_llm_with_failover(
        primary_model=_FakeModel(provider="deepseek", base_url=DASHSCOPE_URL),
        fallback_model=_FakeModel(id="fallback"),
        messages=[{"role": "user", "content": "read the report"}],
        agent_name="Test assistant", role_description="",
        agent_id="agent-x", user_id="user-x", session_id="sess-x",
        on_status=status,
    )
    assert result == "done"
    assert len(client.stream_calls) == 3
    execute.assert_awaited_once()
    assert client.stream_calls[1]["messages"] == client.stream_calls[2]["messages"]
    assert [call.args[0]["state"] for call in status.await_args_list] == ["retrying", "recovered"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [
    ("insufficient_quota", 429),
    ("Throttling.AllocationQuota", None),
])
async def test_background_token_limit_retries_identical_payload(monkeypatch, code, status):
    requests = []

    async def complete(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        if len(requests) == 1:
            raise _token_limit(code=code, status=status)
        return _stop_response("done")

    monkeypatch.setattr("app.services.llm.caller._sleep_before_throttle_retry", AsyncMock())
    client = _ThrottleScriptClient([])
    client.complete = complete
    response = await _complete_with_throttle_retry(
        client, model=_FakeModel(base_url=None), round_i=2,
        messages=[LLMMessage(role="tool", content="completed result", tool_call_id="read-once")],
    )
    assert response.content == "done"
    assert len(requests) == 2
    assert requests[0] == requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,error", [
    ("https://api.openai.com/v1", _token_limit()),
    ("https://dashscope.aliyuncs.com.example.org/v1", _token_limit()),
    (DASHSCOPE_URL, _token_limit(error_type="billing_error")),
    (DASHSCOPE_URL, _token_limit(status=403)),
    (DASHSCOPE_URL, LLMError("credit balance exhausted", status_code=429)),
    (DASHSCOPE_URL, LLMError("bill expired", status_code=429, error_code="PrepaidBillOverdue")),
])
async def test_quota_exception_does_not_retry_permanent_or_other_provider_errors(
    monkeypatch, endpoint, error,
):
    client = _ThrottleScriptClient([error])
    _patch_call_llm_collaborators(monkeypatch, client)
    result = await call_llm_with_failover(
        primary_model=_FakeModel(base_url=endpoint),
        fallback_model=_FakeModel(id="fallback"),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Test assistant", role_description="", skip_tools=True,
    )
    assert isinstance(result, LLMFailure)
    assert result.allow_failover is False
    assert len(client.stream_calls) == 1


@pytest.mark.asyncio
async def test_token_429_exhaustion_keeps_existing_retry_limit(monkeypatch):
    client = _ThrottleScriptClient([_token_limit()] * 6)
    _patch_call_llm_collaborators(monkeypatch, client)
    result = await call_llm_with_failover(
        primary_model=_FakeModel(base_url=DASHSCOPE_URL),
        fallback_model=_FakeModel(id="fallback"),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Test assistant", role_description="", skip_tools=True,
    )
    assert isinstance(result, LLMFailure)
    assert result.code == "provider_rate_limit_exhausted"
    assert result.details["retry_count"] == 5
    assert result.allow_failover is False
    assert len(client.stream_calls) == 6


@pytest.mark.asyncio
async def test_token_429_after_stream_progress_is_not_replayed(monkeypatch):
    client = _ThrottleScriptClient([])

    async def stream(**kwargs):
        client.stream_calls.append(kwargs)
        await kwargs["on_chunk"]("partial response")
        raise _token_limit()

    client.stream = stream
    _patch_call_llm_collaborators(monkeypatch, client)
    result = await call_llm_with_failover(
        primary_model=_FakeModel(base_url=DASHSCOPE_URL),
        fallback_model=_FakeModel(id="fallback"),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Test assistant", role_description="", skip_tools=True,
    )
    assert isinstance(result, LLMFailure)
    assert result.details["had_provider_progress"] is True
    assert result.details["retry_count"] == 0
    assert result.allow_failover is False
    assert len(client.stream_calls) == 1
