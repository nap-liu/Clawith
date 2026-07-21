"""Hard context guard tests: oversized prompts never reach a provider."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from app.services.llm.caller import (
    QWEN_INPUT_CHAR_HARD_LIMIT,
    PROVIDER_CONTEXT_BLOCKED_MESSAGE,
    _dispatch_context_size,
    _guard_provider_dispatch,
)
from app.services.llm.client import LLMMessage


pytestmark = pytest.mark.asyncio


def _model(*, provider="qwen", context_window=262_144):
    return SimpleNamespace(
        provider=provider,
        model="test-model",
        context_window=context_window,
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
