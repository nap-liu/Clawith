"""IM-channel LLM error handling & timeout behavior for ``_call_agent_llm``.

Every external IM channel (Feishu / DingTalk / WeCom / Slack / Discord / Teams /
WhatsApp / WeChat) funnels its LLM call through ``app.api.feishu._call_agent_llm``.
These tests pin two behaviors that are specific to those channels:

1. A long-running tool-calling loop must NOT be killed by an outer
   "whole-conversation" timeout. The only timeout that applies is the
   per-request one *inside* ``call_llm`` (the httpx client timeout). This is the
   regression guard for the 180s ``asyncio.wait_for`` that used to wrap the
   entire ``call_llm`` tool loop and fired on most multi-round messages.

2. When the LLM layer returns one of its error-sentinel strings, the IM user
   must see a friendly message that guides them to send ``/new`` to reset the
   session — never the raw ``[LLM call error] ...`` internals. (Front-end
   WebSocket chat does NOT go through ``_call_agent_llm``, so it is unaffected.)
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

import app.api.feishu as feishu

pytestmark = pytest.mark.asyncio


class _Result:
    """Stand-in for a SQLAlchemy Result with .scalar_one_or_none()."""

    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


def _make_db(agent, model):
    """Minimal AsyncSession double: returns agent then model on .execute()."""
    results = [_Result(agent), _Result(model)]
    state = {"n": 0}

    async def _execute(*_args, **_kwargs):
        i = state["n"]
        state["n"] += 1
        return results[i] if i < len(results) else _Result(None)

    return SimpleNamespace(execute=_execute)


def _make_agent_and_model(request_timeout=None):
    model_id = uuid.uuid4()
    model = SimpleNamespace(
        id=model_id,
        model="test-model",
        enabled=True,
        supports_vision=False,
        request_timeout=request_timeout,
    )
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="测试助手",
        role_description="",
        primary_model_id=model_id,
        fallback_model_id=None,
        context_window_size=100,
    )
    return agent, model


@pytest.fixture(autouse=True)
def _never_expired(monkeypatch):
    monkeypatch.setattr(feishu, "is_agent_expired", lambda _a: False)


def _patch_llm(monkeypatch, fake):
    """Patch BOTH entrypoints so assertions hold no matter which one the
    implementation calls (old code: call_llm; new code: call_llm_with_failover)."""
    monkeypatch.setattr("app.services.llm.call_llm", fake, raising=False)
    monkeypatch.setattr("app.services.llm.call_llm_with_failover", fake, raising=False)


async def test_slow_tool_loop_is_not_killed_by_outer_timeout(monkeypatch):
    """A call that runs longer than ``model.request_timeout`` must still return
    its result. The per-request timeout lives INSIDE call_llm, not as an outer
    wrapper around the whole multi-round tool loop."""
    agent, model = _make_agent_and_model(request_timeout=0.01)

    async def slow_llm(*_args, **_kwargs):
        await asyncio.sleep(0.05)  # 5x the (old) outer budget
        return "任务完成"

    _patch_llm(monkeypatch, slow_llm)

    reply = await feishu._call_agent_llm(
        _make_db(agent, model), agent.id, "你好", session_id=str(agent.id), user_id=agent.id
    )
    assert reply == "任务完成"


@pytest.mark.parametrize(
    "sentinel",
    [
        "[LLM call error] ReadTimeout: stream stalled",
        "[LLM Error] provider returned 500",
        "[Error] Too many tool call rounds",
    ],
)
async def test_llm_error_preserves_original_and_appends_recovery_hint(monkeypatch, sentinel):
    """LLM error sentinels are surfaced to the IM user AS-IS, with a recovery
    hint (guiding /new) appended after them. The original error text must stay
    visible — operators/users need the concrete failure reason."""
    agent, model = _make_agent_and_model()

    async def failing_llm(*_args, **_kwargs):
        return sentinel

    _patch_llm(monkeypatch, failing_llm)

    reply = await feishu._call_agent_llm(
        _make_db(agent, model), agent.id, "你好", session_id=str(agent.id), user_id=agent.id
    )

    assert sentinel in reply, "the original error must be surfaced verbatim"
    assert "/new" in reply, "a recovery hint guiding /new must be appended"
    assert reply != sentinel, "the recovery hint must be appended after the error"


async def test_successful_reply_passes_through(monkeypatch):
    """A normal reply is returned verbatim — wrapping must not touch success."""
    agent, model = _make_agent_and_model()

    async def ok_llm(*_args, **_kwargs):
        return "你好，我可以帮你做什么？"

    _patch_llm(monkeypatch, ok_llm)

    reply = await feishu._call_agent_llm(
        _make_db(agent, model), agent.id, "你好", session_id=str(agent.id), user_id=agent.id
    )
    assert reply == "你好，我可以帮你做什么？"
