"""Hard context guard tests: oversized prompts never reach a provider."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from app.services.llm.caller import (
    QWEN_INPUT_CHAR_HARD_LIMIT,
    PROVIDER_CONTEXT_BLOCKED_MESSAGE,
    _dispatch_context_size,
    _guard_provider_dispatch,
    call_llm,
    call_llm_with_failover,
)
from app.services.llm.client import LLMMessage, LLMResponse


pytestmark = pytest.mark.asyncio


def _model(*, provider="qwen", context_window=262_144):
    return SimpleNamespace(
        provider=provider,
        model="test-model",
        context_window=context_window,
        base_url=None,
        temperature=0.2,
        max_output_tokens=None,
        request_timeout=30,
    )


async def test_small_dispatch_passes():
    result = await _guard_provider_dispatch(
        model=_model(),
        messages=[LLMMessage(role="user", content="small request")],
        tools=[],
        max_output_tokens=32_768,
        session_id="session",
    )

    assert result is None


async def test_qwen_character_limit_blocks_without_claiming_termination(monkeypatch):
    terminate = AsyncMock(return_value="must not be used")
    monkeypatch.setattr(
        "app.services.llm.session_context_guard.terminate_session_context",
        terminate,
    )
    body = "x" * QWEN_INPUT_CHAR_HARD_LIMIT

    result = await _guard_provider_dispatch(
        model=_model(context_window=1_000_000),
        messages=[LLMMessage(role="user", content=body)],
        tools=[],
        max_output_tokens=1_000,
        session_id="session",
    )

    assert result == PROVIDER_CONTEXT_BLOCKED_MESSAGE
    assert result == "上下文过长，请新开会话。"
    terminate.assert_not_awaited()


async def test_cjk_token_estimate_reserves_output_window():
    result = await _guard_provider_dispatch(
        model=_model(provider="custom", context_window=1_000),
        messages=[LLMMessage(role="user", content="数" * 901)],
        tools=[],
        max_output_tokens=100,
        session_id="session",
    )

    assert result == PROVIDER_CONTEXT_BLOCKED_MESSAGE
    chars, estimated_tokens = _dispatch_context_size(
        [LLMMessage(role="user", content="数" * 901)], []
    )
    assert chars == 901
    assert estimated_tokens == 901


async def test_first_dispatch_can_recover_once_before_provider(monkeypatch):
    calls = []

    class _Client:
        async def stream(self, *, messages, **_kwargs):
            calls.append(messages)
            return LLMResponse(content="ok", usage={"prompt_tokens": 10})

        async def close(self):
            return None

    recovery = AsyncMock(return_value=[{"role": "user", "content": "small"}])
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(3, None)))
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **_kwargs: _Client())
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "key")
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *_args, **_kwargs: 100)

    model = _model(provider="custom", context_window=1_000)
    model.max_output_tokens = 100
    result = await call_llm(
        model,
        [{"role": "user", "content": "数" * 900}],
        "agent",
        "",
        prepared_turn_context=("system", "dynamic"),
        prepared_tools=[],
        context_recovery=recovery,
        turn_anchor_id=uuid.uuid4(),
    )

    assert result == "ok"
    assert recovery.await_count == 1
    assert len(calls) == 1


async def test_first_dispatch_preflight_compacts_before_hard_overflow(monkeypatch):
    """Every anchored session surface uses the standard pre-flight boundary.

    The initial prompt still fits the provider window, but it has crossed the
    compactor's conservative input-capacity threshold.  Recovery must therefore
    run before the first provider request, not wait for a later hard overflow.
    Project group and project A2A durable children use this exact caller path.
    """

    calls = []

    class _Client:
        async def stream(self, *, messages, **_kwargs):
            calls.append(messages)
            return LLMResponse(content="ok", usage={"prompt_tokens": 10})

        async def close(self):
            return None

    recovery = AsyncMock(return_value=[{"role": "user", "content": "compacted"}])
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(3, None)))
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **_kwargs: _Client())
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "key")
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *_args, **_kwargs: 100)

    model = _model(provider="custom", context_window=1_000)
    model.max_output_tokens = 100
    result = await call_llm(
        model,
        [{"role": "user", "content": "数" * 880}],
        "agent",
        "",
        prepared_turn_context=("system", "dynamic"),
        prepared_tools=[],
        context_recovery=recovery,
        turn_anchor_id=uuid.uuid4(),
    )

    assert result == "ok"
    recovery.assert_awaited_once()
    assert calls[0][-1].content.endswith("compacted")


async def test_failed_recovery_never_dispatches_normal_generation(monkeypatch):
    class _NeverClient:
        async def stream(self, **_kwargs):
            raise AssertionError("oversized request reached provider")

        async def close(self):
            return None

    recovery = AsyncMock(return_value=None)
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(3, None)))
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **_kwargs: _NeverClient())
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "key")
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *_args, **_kwargs: 100)

    result = await call_llm(
        _model(provider="custom", context_window=1_000),
        [{"role": "user", "content": "数" * 900}],
        "agent",
        "",
        prepared_turn_context=("system", "dynamic"),
        prepared_tools=[],
        context_recovery=recovery,
        turn_anchor_id=uuid.uuid4(),
    )

    assert result == PROVIDER_CONTEXT_BLOCKED_MESSAGE
    recovery.assert_awaited_once()


async def test_qwen_character_overflow_can_recover_before_dispatch(monkeypatch):
    calls = []

    class _Client:
        async def stream(self, *, messages, **_kwargs):
            calls.append(messages)
            return LLMResponse(content="ok", usage={"prompt_tokens": 10})

        async def close(self):
            return None

    recovery = AsyncMock(return_value=[{"role": "user", "content": "small"}])
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(3, None)))
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **_kwargs: _Client())
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "key")
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *_args, **_kwargs: 100)

    result = await call_llm(
        _model(provider="qwen", context_window=500_000),
        [{"role": "user", "content": "x" * QWEN_INPUT_CHAR_HARD_LIMIT}],
        "agent",
        "",
        prepared_turn_context=("system", "dynamic"),
        prepared_tools=[],
        context_recovery=recovery,
        turn_anchor_id=uuid.uuid4(),
    )

    assert result == "ok"
    assert len(calls) == 1
    assert recovery.await_args.args[1].char_overflow is True


async def test_unanchored_oversized_call_never_uses_recovery(monkeypatch):
    class _NeverClient:
        async def stream(self, **_kwargs):
            raise AssertionError("oversized request reached provider")

        async def close(self):
            return None

    recovery = AsyncMock(return_value=[{"role": "user", "content": "small"}])
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(3, None)))
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **_kwargs: _NeverClient())
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "key")
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *_args, **_kwargs: 100)

    result = await call_llm(
        _model(provider="custom", context_window=1_000),
        [{"role": "user", "content": "数" * 900}],
        "agent",
        "",
        prepared_turn_context=("system", "dynamic"),
        prepared_tools=[],
        context_recovery=recovery,
    )

    assert result == PROVIDER_CONTEXT_BLOCKED_MESSAGE
    recovery.assert_not_awaited()


async def test_later_tool_round_overflow_stops_without_recovery_or_replay(monkeypatch):
    stream_calls = []

    class _Client:
        async def stream(self, *, messages, **_kwargs):
            stream_calls.append(messages)
            return LLMResponse(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "large_result", "arguments": "{}"},
                    }
                ],
                usage={"prompt_tokens": 10},
            )

        async def close(self):
            return None

    async def append_large_tool_result(**kwargs):
        kwargs["api_messages"].append(
            LLMMessage(role="tool", tool_call_id="call-1", content="数" * 1_300)
        )
        return ""

    recovery = AsyncMock(return_value=[{"role": "user", "content": "replayed"}])
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(2, None)))
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **_kwargs: _Client())
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "key")
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *_args, **_kwargs: 100)
    monkeypatch.setattr("app.services.llm.caller._process_tool_call", append_large_tool_result)
    monkeypatch.setattr(
        "app.services.llm.caller._persist_tool_call_events_strict",
        AsyncMock(return_value=False),
    )

    model = _model(provider="custom", context_window=1_500)
    model.max_output_tokens = 100
    result = await call_llm(
        model,
        [{"role": "user", "content": "small"}],
        "agent",
        "",
        prepared_turn_context=("system", "dynamic"),
        prepared_tools=[
            {
                "type": "function",
                "function": {"name": "large_result", "parameters": {}},
            }
        ],
        context_recovery=recovery,
        turn_anchor_id=uuid.uuid4(),
    )

    assert result == PROVIDER_CONTEXT_BLOCKED_MESSAGE
    assert len(stream_calls) == 1
    recovery.assert_not_awaited()


async def test_same_model_id_skips_fallback(monkeypatch):
    model_id = "same-record"
    primary = SimpleNamespace(id=model_id, provider="custom", model="p")
    fallback = SimpleNamespace(id=model_id, provider="custom", model="p")
    call = AsyncMock(return_value="[LLM Error] timeout")
    monkeypatch.setattr("app.services.llm.caller.call_llm", call)
    monkeypatch.setattr(
        "app.services.llm.caller._build_turn_context",
        AsyncMock(return_value=("static", "dynamic")),
    )
    monkeypatch.setattr("app.services.llm.caller.get_agent_tools_for_llm", AsyncMock(return_value=[]))

    result = await call_llm_with_failover(
        primary_model=primary,
        fallback_model=fallback,
        messages=[{"role": "user", "content": "hello"}],
        agent_name="agent",
        role_description="",
        agent_id="agent-id",
    )

    assert result == "[LLM Error] timeout"
    assert call.await_count == 1


@pytest.mark.parametrize("partial_callback", ["on_thinking", "on_tool_delta"])
async def test_partial_provider_output_blocks_fallback(monkeypatch, partial_callback):
    primary = SimpleNamespace(id="primary", provider="custom", model="p")
    fallback = SimpleNamespace(id="fallback", provider="custom", model="f")
    calls = []

    async def fake_call_llm(model, _messages, *_args, **kwargs):
        calls.append(model.id)
        callback = kwargs[partial_callback]
        await callback("thinking" if partial_callback == "on_thinking" else {"name": "tool"})
        return "[LLM Error] timeout"

    monkeypatch.setattr("app.services.llm.caller.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "app.services.llm.caller._build_turn_context",
        AsyncMock(return_value=("static", "dynamic")),
    )
    monkeypatch.setattr("app.services.llm.caller.get_agent_tools_for_llm", AsyncMock(return_value=[]))

    result = await call_llm_with_failover(
        primary_model=primary,
        fallback_model=fallback,
        messages=[{"role": "user", "content": "hello"}],
        agent_name="agent",
        role_description="",
        agent_id="agent-id",
    )

    assert result == "[LLM Error] timeout"
    assert calls == ["primary"]


async def test_fallback_never_compacts_after_primary_provider_attempt(monkeypatch):
    primary = SimpleNamespace(id="primary", provider="custom", model="p")
    fallback = SimpleNamespace(id="fallback", provider="custom", model="f")
    recovery = AsyncMock(return_value=[{"role": "user", "content": "replayed"}])
    recovery_callbacks = []

    async def fake_call_llm(model, _messages, *_args, context_recovery=None, **_kwargs):
        recovery_callbacks.append(context_recovery)
        if model.id == "primary":
            return "[LLM Error] timeout"
        return PROVIDER_CONTEXT_BLOCKED_MESSAGE

    monkeypatch.setattr("app.services.llm.caller.call_llm", fake_call_llm)
    monkeypatch.setattr(
        "app.services.llm.caller._build_turn_context",
        AsyncMock(return_value=("static", "dynamic")),
    )
    monkeypatch.setattr("app.services.llm.caller.get_agent_tools_for_llm", AsyncMock(return_value=[]))

    await call_llm_with_failover(
        primary_model=primary,
        fallback_model=fallback,
        messages=[{"role": "user", "content": "hello"}],
        agent_name="agent",
        role_description="",
        agent_id="agent-id",
        turn_anchor_id=uuid.uuid4(),
        context_recovery=recovery,
    )

    assert callable(recovery_callbacks[0])
    assert recovery_callbacks[1] is None
    recovery.assert_not_awaited()
