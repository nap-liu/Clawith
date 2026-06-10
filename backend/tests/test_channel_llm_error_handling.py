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
from unittest.mock import AsyncMock

import pytest

import app.services.channel_llm as channel_llm

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
    monkeypatch.setattr(channel_llm, "is_agent_expired", lambda _a: False)


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

    reply = await channel_llm._call_agent_llm(
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

    reply = await channel_llm._call_agent_llm(
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

    reply = await channel_llm._call_agent_llm(
        _make_db(agent, model), agent.id, "你好", session_id=str(agent.id), user_id=agent.id
    )
    assert reply == "你好，我可以帮你做什么？"


async def test_im_turn_broadcasts_events_to_web_session(monkeypatch):
    """An IM-driven turn mirrors its live stream to web clients viewing the SAME
    session, so a DingTalk/Feishu conversation updates in real time in the web UI
    (not only on reload). Verifies `_call_agent_llm` broadcasts chunk/thinking/
    tool_call/done via the WebSocket `ConnectionManager.send_to_session` for the
    turn's session_id — the cross-channel half of the live-broadcast fix."""
    agent, model = _make_agent_and_model()

    import app.api.websocket as ws_mod

    sent: list[tuple[str, str]] = []

    async def _fake_send_to_session(agent_id, session_id, payload):
        sent.append((session_id, payload.get("type")))

    monkeypatch.setattr(ws_mod.manager, "send_to_session", _fake_send_to_session)
    # Keep this a pure wiring test — no real DB writes / compaction.
    monkeypatch.setattr("app.services.chat_history.persist_tool_call", AsyncMock(), raising=False)
    monkeypatch.setattr(
        "app.services.llm.compactor.maybe_precompact_prompt", AsyncMock(return_value=False), raising=False
    )

    async def fake_llm(*_args, **kwargs):
        await kwargs["on_thinking"]("想一下")
        await kwargs["on_chunk"]("昨天销售")
        await kwargs["on_tool_call"](
            {"name": "sql_execute", "call_id": "1", "args": {"conn": "secret"}, "status": "done", "result": "ok"}
        )
        return "昨天销售额 5050"

    monkeypatch.setattr("app.services.llm.call_llm_with_failover", fake_llm, raising=False)

    reply = await channel_llm._call_agent_llm(
        _make_db(agent, model), agent.id, "看销售", session_id="sess-123", user_id=agent.id
    )

    assert reply == "昨天销售额 5050"
    types = [t for _sid, t in sent]
    sids = {sid for sid, _t in sent}
    assert sids == {"sess-123"}, "every broadcast must target the turn's session"
    for expected in ("thinking", "chunk", "tool_call", "done"):
        assert expected in types, f"web viewer must receive the {expected!r} event of an IM turn"


async def test_broadcast_channel_user_message_emits_event(monkeypatch):
    """The inbound IM user message is mirrored to web viewers of the session with
    its clean content + sender, so the user's own bubble appears live (not only on
    reload). Pins the event shape the frontend handler consumes."""
    import app.api.websocket as ws_mod

    captured: list = []

    async def _fake_send_to_session(agent_id, session_id, payload):
        captured.append((agent_id, session_id, payload))

    monkeypatch.setattr(ws_mod.manager, "send_to_session", _fake_send_to_session)

    await channel_llm.broadcast_channel_user_message(
        "agent-1", "sess-9", content="只看 report 的数据", sender_name="刘喜", user_id="u-7"
    )

    assert len(captured) == 1
    agent_id, session_id, payload = captured[0]
    assert agent_id == "agent-1" and session_id == "sess-9"
    assert payload["type"] == "channel_user_message"
    assert payload["content"] == "只看 report 的数据"
    assert payload["sender_name"] == "刘喜"
    assert payload["user_id"] == "u-7"


async def test_broadcast_channel_user_message_no_session_noop(monkeypatch):
    import app.api.websocket as ws_mod

    captured: list = []

    async def _fake(*_a, **_k):
        captured.append(1)

    monkeypatch.setattr(ws_mod.manager, "send_to_session", _fake)
    await channel_llm.broadcast_channel_user_message("a", "", content="x")
    assert captured == [], "no session_id → no broadcast"


async def test_im_turn_without_session_does_not_broadcast(monkeypatch):
    """No session_id (transient call) → no web broadcast, and no crash."""
    agent, model = _make_agent_and_model()

    import app.api.websocket as ws_mod

    sent: list[int] = []

    async def _fake_send_to_session(*_a, **_k):
        sent.append(1)

    monkeypatch.setattr(ws_mod.manager, "send_to_session", _fake_send_to_session)
    monkeypatch.setattr("app.services.chat_history.persist_tool_call", AsyncMock(), raising=False)

    async def fake_llm(*_a, **kwargs):
        await kwargs["on_chunk"]("x")
        return "ok"

    monkeypatch.setattr("app.services.llm.call_llm_with_failover", fake_llm, raising=False)

    reply = await channel_llm._call_agent_llm(
        _make_db(agent, model), agent.id, "hi", session_id="", user_id=agent.id
    )
    assert reply == "ok"
    assert sent == [], "no session → no broadcast"
