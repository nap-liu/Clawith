from __future__ import annotations

import json
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


def _finish_response(content: str) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            {
                "id": "call_finish",
                "type": "function",
                "function": {
                    "name": "finish",
                    "arguments": json.dumps({"content": content}, ensure_ascii=False),
                },
            }
        ],
        finish_reason="tool_calls",
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
            _finish_response("重试后成功"),
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
