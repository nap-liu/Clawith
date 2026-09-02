"""Provider-authoritative context governance behavior."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.llm.caller import (
    DispatchBudget,
    _authoritative_usage_details,
    _is_provider_context_overflow,
    call_llm,
    call_llm_with_failover,
    measure_dispatch,
)
from app.services.llm.client import GeminiClient, LLMError, LLMMessage, LLMResponse
from app.services.token_tracker import extract_token_usage

pytestmark = pytest.mark.asyncio


def _model(*, context_window=1_000, usage_ratio=1.0):
    return SimpleNamespace(
        id=None,
        provider="qwen",
        model="qwen-test",
        context_window=context_window,
        context_usage_ratio=usage_ratio,
        keep_recent_turns=3,
        base_url=None,
        temperature=0.2,
        max_output_tokens=100,
        request_timeout=30,
    )


async def test_provider_usage_is_the_only_input_capacity_authority():
    budget = measure_dispatch(
        model=_model(context_window=1_000),
        messages=[
            LLMMessage(role="user", content="数" * 100_000),
            LLMMessage(role="user", content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}]),
        ],
        tools=[{"type": "function", "function": {"name": "tool", "parameters": {}}}],
        max_output_tokens=100,
    )

    assert budget.authoritative_prompt_tokens is None
    assert budget.token_overflow is False
    assert budget.fits is True


async def test_gemini_usage_preserves_cache_and_multimodal_provider_details():
    raw = {
        "promptTokenCount": 1234,
        "candidatesTokenCount": 56,
        "totalTokenCount": 1290,
        "cachedContentTokenCount": 900,
        "promptTokensDetails": [
            {"modality": "TEXT", "tokenCount": 234},
            {"modality": "IMAGE", "tokenCount": 1000},
        ],
        "candidatesTokensDetails": [{"modality": "TEXT", "tokenCount": 56}],
    }
    normalized = GeminiClient(api_key="test", model="gemini-test")._normalize_usage(raw)
    usage = extract_token_usage(normalized)

    assert usage is not None
    assert usage.context_input_tokens == 1234
    assert usage.cache_read_tokens == 900
    assert normalized["input_tokens_details"]["modality_token_counts"][1]["modality"] == "IMAGE"
    assert normalized["output_tokens_details"]["modality_token_counts"][0]["tokenCount"] == 56
    persisted = _authoritative_usage_details(normalized)
    assert "input_tokens_details" in persisted
    assert "output_tokens_details" in persisted


async def test_gemini_keeps_dynamic_system_context_before_tool_result_tail():
    client = GeminiClient(api_key="test", model="gemini-test")
    payload = client._build_payload(
        [
            LLMMessage(
                role="system",
                content="STATIC",
                dynamic_content="CURRENT-SNAPSHOT",
            ),
            LLMMessage(
                role="assistant",
                tool_calls=[{
                    "id": "external-1",
                    "type": "function",
                    "function": {
                        "name": "wait_for_external_result",
                        "arguments": "{}",
                    },
                }],
            ),
            LLMMessage(
                role="tool",
                content="completed",
                tool_call_id="external-1",
            ),
        ],
        tools=None,
        temperature=0.2,
        max_tokens=100,
    )

    assert payload["systemInstruction"]["parts"] == [
        {"text": "STATIC\n\nCURRENT-SNAPSHOT"}
    ]
    assert payload["contents"][-1]["parts"][0]["functionResponse"] == {
        "name": "wait_for_external_result",
        "response": {"result": "completed"},
    }


async def test_authoritative_provider_count_enforces_ratio_and_output_reserve():
    common = dict(
        model=_model(context_window=1_000, usage_ratio=0.7),
        messages=[LLMMessage(role="user", content="irrelevant")],
        tools=None,
        max_output_tokens=100,
        count_source="provider_usage",
    )
    at_limit = measure_dispatch(**common, authoritative_prompt_tokens=600)
    above_limit = measure_dispatch(**common, authoritative_prompt_tokens=601)

    assert at_limit.fits is True
    assert above_limit.token_overflow is True
    assert above_limit.fits is False


async def test_context_overflow_prefers_structured_provider_evidence():
    structured = LLMError(
        "request rejected",
        status_code=400,
        error_code="context_length_exceeded",
        error_type="invalid_request_error",
    )
    unrelated = LLMError(
        "HTTP 400: invalid tool schema",
        status_code=400,
        error_code="invalid_parameter",
    )

    assert _is_provider_context_overflow(structured) is True
    assert _is_provider_context_overflow(LLMError("payload too large", status_code=413)) is True
    assert _is_provider_context_overflow(unrelated) is False


async def test_context_rejection_retries_same_round_until_recovery_succeeds(monkeypatch):
    calls = 0

    class _Client:
        async def stream(self, **_kwargs):
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise LLMError("context_length_exceeded")
            return LLMResponse(content="ok", usage={"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11})

        async def close(self):
            return None

    recovery = AsyncMock(return_value=[{"role": "user", "content": "compacted"}])
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(1, None)))
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **_kwargs: _Client())
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "key")
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *_args, **_kwargs: 100)

    result = await call_llm(
        _model(), [{"role": "user", "content": "large"}], "agent", "",
        prepared_turn_context=("system", "dynamic"), prepared_tools=[],
        context_recovery=recovery, turn_anchor_id=uuid.uuid4(),
    )

    assert result == "ok"
    assert calls == 4
    assert recovery.await_count == 3
    assert recovery.await_args.args[1].provider_overflow is True


async def test_context_rejection_after_stream_progress_is_not_replayed(monkeypatch):
    chunks: list[str] = []

    class _Client:
        async def stream(self, *, on_chunk=None, **_kwargs):
            await on_chunk("partial")
            raise LLMError("maximum context length")

        async def close(self):
            return None

    async def _on_chunk(value: str):
        chunks.append(value)

    recovery = AsyncMock(return_value=[{"role": "user", "content": "compacted"}])
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(1, None)))
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **_kwargs: _Client())
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "key")
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *_args, **_kwargs: 100)

    result = await call_llm(
        _model(), [{"role": "user", "content": "large"}], "agent", "",
        prepared_turn_context=("system", "dynamic"), prepared_tools=[],
        context_recovery=recovery, turn_anchor_id=uuid.uuid4(), on_chunk=_on_chunk,
    )

    assert chunks == ["partial"]
    assert result.startswith("[LLM Error]")
    recovery.assert_not_awaited()


async def test_provider_rejection_degrades_one_protected_turn_per_retry(monkeypatch):
    observed_keeps: list[int | None] = []

    async def recovery(_model, budget):
        observed_keeps.append(budget.keep_recent_turns_override)
        return [{"role": "user", "content": f"keep={budget.keep_recent_turns_override}"}]

    async def fake_call(model, *_args, context_recovery=None, **_kwargs):
        budget = DispatchBudget(
            physical_context_window=1_000,
            context_usage_ratio=0.7,
            effective_context_window=700,
            input_capacity=600,
            safe_input_limit=600,
            token_overflow=True,
            provider_overflow=True,
        )
        for _ in range(4):
            assert await context_recovery(model, budget) is not None
        assert await context_recovery(model, budget) is None
        return "ok"

    monkeypatch.setattr(
        "app.services.llm.caller._build_turn_context",
        AsyncMock(return_value=("system", "dynamic")),
    )
    monkeypatch.setattr("app.services.llm.caller.call_llm", fake_call)

    result = await call_llm_with_failover(
        _model(),
        None,
        [{"role": "user", "content": "large"}],
        "agent",
        "",
        prepared_tools=[],
        context_recovery=recovery,
    )

    assert result == "ok"
    assert observed_keeps == [3, 2, 1, 0]


async def test_plain_final_round_uses_actual_usage_for_post_round_compaction(monkeypatch):
    class _Client:
        async def stream(self, **_kwargs):
            return LLMResponse(content="ok", usage={"prompt_tokens": 900, "completion_tokens": 10, "total_tokens": 910})

        async def close(self):
            return None

    recovery = AsyncMock(return_value=[{"role": "user", "content": "compacted"}])
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(1, None)))
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **_kwargs: _Client())
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda _model: "key")
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *_args, **_kwargs: 100)

    result = await call_llm(
        _model(context_window=1_000), [{"role": "user", "content": "request"}], "agent", "",
        prepared_turn_context=("system", "dynamic"), prepared_tools=[],
        context_recovery=recovery, turn_anchor_id=uuid.uuid4(),
    )

    assert result == "ok"
    recovery.assert_awaited_once()
    assert recovery.await_args.args[1].authoritative_prompt_tokens == 900
    assert recovery.await_args.args[1].count_source == "provider_usage"
