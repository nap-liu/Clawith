"""Background tool loops must keep oversized tool output out of LLM context."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import get_settings
from app.services.llm.caller import call_agent_llm_with_tools
from app.services.llm.client import LLMResponse
from app.services.llm.tool_output_store import PERSISTED_OPEN

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


async def test_background_context_guard_is_truthful_and_never_persists_fake_session(
    monkeypatch,
):
    agent_id = uuid.uuid4()
    primary_id = uuid.uuid4()
    fallback_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="background-agent",
        creator_id=uuid.uuid4(),
        primary_model_id=primary_id,
        fallback_model_id=fallback_id,
    )

    def _small_model(model_id):
        return SimpleNamespace(
            id=model_id,
            provider="custom",
            model="tiny-context",
            base_url=None,
            temperature=0.2,
            max_output_tokens=100,
            request_timeout=30,
            context_window=1_000,
        )

    query_results = [
        _Result(agent),
        _Result(_small_model(primary_id)),
        _Result(_small_model(fallback_id)),
    ]

    async def _execute(*_args, **_kwargs):
        return query_results.pop(0)

    class _NeverCalledClient:
        async def complete(self, **_kwargs):
            raise AssertionError("oversized request reached provider")

        async def close(self):
            return None

    terminate = AsyncMock(return_value="must not persist")
    monkeypatch.setattr(
        "app.services.llm.session_context_guard.get_session_context_termination",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.llm.session_context_guard.terminate_session_context",
        terminate,
    )
    monkeypatch.setattr(
        "app.services.llm.caller.create_llm_client",
        lambda **_kwargs: _NeverCalledClient(),
    )
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "key")
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(return_value=[]),
    )

    db = SimpleNamespace(execute=_execute, commit=AsyncMock())
    reply = await call_agent_llm_with_tools(
        db,
        agent_id,
        "system",
        "数" * 1_000,
        session_id=str(uuid.uuid4()),
    )

    assert reply == "上下文过长，请新开会话。"
    db.commit.assert_awaited_once()
    terminate.assert_not_awaited()


async def test_large_read_file_result_is_materialized_before_second_model_round(
    monkeypatch,
    tmp_path,
):
    agent_id = uuid.uuid4()
    model_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="background-agent",
        creator_id=uuid.uuid4(),
        primary_model_id=model_id,
        fallback_model_id=None,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="qwen",
        model="qwen-test",
        base_url=None,
        temperature=0.2,
        max_output_tokens=1_000,
        request_timeout=30,
        context_window=1_000_000,
    )

    responses = [
        LLMResponse(
            content="reading",
            tool_calls=[
                {
                    "id": "read-1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"large.json"}',
                    },
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        ),
        LLMResponse(
            content="done",
            usage={"prompt_tokens": 20, "completion_tokens": 5},
        ),
    ]

    class _Client:
        def __init__(self):
            self.requests = []

        async def complete(self, *, messages, **_kwargs):
            self.requests.append(list(messages))
            return responses.pop(0)

        async def close(self):
            return None

    client = _Client()
    query_results = [_Result(agent), _Result(model)]

    async def _execute(*_args, **_kwargs):
        return query_results.pop(0)

    db = SimpleNamespace(execute=_execute, commit=AsyncMock())
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.services.llm.session_context_guard.get_session_context_termination",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.create_llm_client",
        lambda **_kwargs: client,
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_model_api_key",
        lambda _model: "test-key",
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(
            return_value=[
                {
                    "type": "function",
                    "function": {"name": "read_file", "description": "read"},
                }
            ]
        ),
    )
    huge = "x" * 200_000
    monkeypatch.setattr(
        "app.services.llm.caller.execute_tool",
        AsyncMock(return_value=huge),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.record_token_usage",
        AsyncMock(return_value=None),
    )

    try:
        result = await call_agent_llm_with_tools(
            db,
            agent_id,
            "system",
            "process the file",
            max_rounds=2,
            session_id=str(uuid.uuid4()),
        )
    finally:
        get_settings.cache_clear()

    assert result == "reading\n\ndone"
    db.commit.assert_awaited_once()
    tool_message = next(msg for msg in client.requests[1] if msg.role == "tool")
    assert PERSISTED_OPEN in tool_message.content
    assert len(tool_message.content) < len(huge)
    assert huge not in tool_message.content


async def test_background_tool_round_content_becomes_confirmation_intro(monkeypatch):
    agent_id = uuid.uuid4()
    model_id = uuid.uuid4()
    execution_user_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="background-agent",
        creator_id=execution_user_id,
        primary_model_id=model_id,
        fallback_model_id=None,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="qwen",
        model="qwen-test",
        base_url=None,
        temperature=0.2,
        max_output_tokens=1_000,
        request_timeout=30,
        context_window=1_000_000,
    )
    responses = [
        LLMResponse(
            content="background answer",
            tool_calls=[
                {
                    "id": "lookup-1",
                    "type": "function",
                    "function": {"name": "knowledge_search", "arguments": "{}"},
                }
            ],
        ),
        LLMResponse(
            content="",
            tool_calls=[
                {
                    "id": "confirm-1",
                    "type": "function",
                    "function": {
                        "name": "request_confirmation",
                        "arguments": (
                            '{"title":"Confirm","summary":"Continue?","buttons":[{"text":"Yes","value":"yes"}]}'
                        ),
                    },
                }
            ],
        ),
    ]

    class _Client:
        async def complete(self, **_kwargs):
            return responses.pop(0)

        async def close(self):
            return None

    query_results = [_Result(agent), _Result(model)]

    async def _execute_query(*_args, **_kwargs):
        return query_results.pop(0)

    monkeypatch.setattr(
        "app.services.llm.session_context_guard.get_session_context_termination",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **_kwargs: _Client())
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "test-key")
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(
            return_value=[
                {"type": "function", "function": {"name": "knowledge_search", "description": "search"}},
                {"type": "function", "function": {"name": "request_confirmation", "description": "confirm"}},
            ]
        ),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.execute_tool",
        AsyncMock(return_value="matched"),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.record_token_usage",
        AsyncMock(return_value=None),
    )
    suspend = AsyncMock(return_value="confirmation-row")
    monkeypatch.setattr(
        "app.services.confirmation_service.suspend_for_confirmation",
        suspend,
    )

    db = SimpleNamespace(execute=_execute_query, commit=AsyncMock())
    reply = await call_agent_llm_with_tools(
        db,
        agent_id,
        "system",
        "prompt",
        session_id=str(uuid.uuid4()),
    )

    assert reply == ""
    db.commit.assert_awaited_once()
    assert suspend.await_args.kwargs["intro_text"] == "background answer"
