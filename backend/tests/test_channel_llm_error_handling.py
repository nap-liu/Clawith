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

from app.services import channel_llm

pytestmark = pytest.mark.asyncio


class _Result:
    """Stand-in for a SQLAlchemy Result with .scalar_one_or_none()."""

    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


def _make_db(agent, model, fallback_model=None):
    """Minimal AsyncSession double for the normal channel runtime-model path."""
    results = [_Result(agent), _Result(model)]
    if fallback_model is not None:
        results.append(_Result(fallback_model))
    state = {"n": 0}

    async def _execute(*_args, **_kwargs):
        i = state["n"]
        state["n"] += 1
        return results[i] if i < len(results) else _Result(None)

    async def _get(*_args, **_kwargs):
        # No persisted ChatSession override in the common fixture. Tests that
        # exercise a concrete message/session replace this with their own
        # AsyncMock below, matching AsyncSession.get's current production path.
        return None

    return SimpleNamespace(execute=_execute, get=_get)


def _make_model(*, model_name="test-model", request_timeout=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=None,
        provider="openai",
        model=model_name,
        api_key_encrypted="test-key",
        base_url=None,
        label=model_name,
        max_tokens_per_day=None,
        enabled=True,
        supports_vision=False,
        temperature=None,
        request_timeout=request_timeout,
        max_output_tokens=None,
        context_window=32000,
        compact_trigger_ratio=0.85,
        keep_recent_turns=8,
        compact_summary_max_tokens=2000,
    )


def _make_agent_and_model(request_timeout=None):
    model = _make_model(request_timeout=request_timeout)
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="测试助手",
        role_description="",
        primary_model_id=model.id,
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


async def test_channel_turn_persists_accumulated_session_usage(monkeypatch):
    from app.services import session_token_usage
    from app.services.token_tracker import TokenUsage

    agent, model = _make_agent_and_model()
    anchor_id = uuid.uuid4()
    session_id = str(uuid.uuid4())
    persist = AsyncMock(return_value=None)
    monkeypatch.setattr(session_token_usage, "persist_turn_token_usage", persist)

    async def ok_llm(*_args, on_usage=None, **_kwargs):
        assert on_usage is not None
        await on_usage(
            TokenUsage(
                total_tokens=1000,
                input_tokens=900,
                output_tokens=100,
                cache_read_tokens=600,
                cache_eligible_input_tokens=900,
            )
        )
        await on_usage(
            TokenUsage(
                total_tokens=300,
                input_tokens=200,
                output_tokens=100,
                cache_read_tokens=100,
                cache_eligible_input_tokens=200,
            )
        )
        return "完成"

    _patch_llm(monkeypatch, ok_llm)

    reply = await channel_llm._call_agent_llm(
        _make_db(agent, model),
        agent.id,
        "执行任务",
        session_id=session_id,
        user_id=agent.id,
        turn_anchor_id=anchor_id,
    )

    assert reply == "完成"
    persist.assert_awaited_once()
    kwargs = persist.await_args.kwargs
    assert kwargs["session_id"] == session_id
    assert kwargs["turn_anchor_id"] == anchor_id
    assert kwargs["usage"].total_tokens == 1300
    assert kwargs["usage"].cache_read_tokens == 700
    assert kwargs["usage"].cache_eligible_input_tokens == 1100


async def test_scene_context_is_forwarded_to_shared_llm_caller(monkeypatch):
    from app.services import scene_service

    agent, model = _make_agent_and_model()
    captured = {}
    scene_context = {
        "source_channel": "dingtalk",
        "scene_key": "warranty",
        "scene_revision": 3,
        "scene_system_prompts": [{"id": "policy", "content": "Use warranty policy."}],
    }

    async def fake_scene_context(*_args, **_kwargs):
        return scene_context

    async def fake_llm(*_args, **kwargs):
        captured.update(kwargs)
        return "已处理"

    monkeypatch.setattr(scene_service, "load_turn_scene_context", fake_scene_context)
    _patch_llm(monkeypatch, fake_llm)

    reply = await channel_llm._call_agent_llm(
        _make_db(agent, model),
        agent.id,
        "处理售后",
        session_id=str(uuid.uuid4()),
        user_id=agent.id,
        turn_anchor_id=uuid.uuid4(),
    )

    assert reply == "已处理"
    assert captured["channel_context"] == scene_context


async def test_current_channel_attachment_keeps_live_text_and_uses_structured_path(monkeypatch):
    agent, model = _make_agent_and_model()
    anchor_id = uuid.uuid4()
    session_id = str(uuid.uuid4())
    db = _make_db(agent, model)
    anchor = SimpleNamespace(
        agent_id=agent.id,
        conversation_id=session_id,
        message_meta={
            "source_channel": "feishu",
            "display_content": "",
            "attachments": [{
                "display_name": "photo.png",
                "path": "workspace/uploads/photo.png",
                "kind": "image",
            }],
        },
    )
    db.get = AsyncMock(
        side_effect=lambda model_cls, _object_id: (
            anchor if model_cls.__name__ == "ChatMessage" else None
        )
    )
    captured = {}

    async def fake_llm(*_args, **kwargs):
        captured.update(kwargs)
        return "已处理"

    _patch_llm(monkeypatch, fake_llm)

    reply = await channel_llm._call_agent_llm(
        db,
        agent.id,
        "<sender>张三</sender>\n请看图[image_data:data:image/png;base64,bGVnYWN5]",
        session_id=session_id,
        user_id=agent.id,
        turn_anchor_id=anchor_id,
    )

    assert reply == "已处理"
    assert captured["messages"][-1] == {
        "role": "user",
        "content": "<sender>张三</sender>\n请看图",
        "attachments": [{
            "display_name": "photo.png",
            "path": "workspace/uploads/photo.png",
            "kind": "image",
        }],
    }


async def test_context_limit_uses_short_im_reset_message(monkeypatch):
    agent, model = _make_agent_and_model()

    async def blocked_llm(*_args, **_kwargs):
        return "上下文过长，请新开会话。"

    _patch_llm(monkeypatch, blocked_llm)

    reply = await channel_llm._call_agent_llm(
        _make_db(agent, model),
        agent.id,
        "继续",
        session_id=str(agent.id),
        user_id=agent.id,
    )

    assert "单独发送一条消息" in reply
    assert "/new" in reply
    assert "不要在同一条消息中添加其他文字或附件" in reply


async def test_channel_does_not_consult_or_write_persistent_context_termination(monkeypatch):
    """Old termination rows are audit-only and cannot brick later turns."""
    agent, model = _make_agent_and_model()
    llm = AsyncMock(return_value="继续成功")
    monkeypatch.setattr("app.services.llm.call_llm_with_failover", llm, raising=False)
    get_termination = AsyncMock(return_value="must not block")
    terminate = AsyncMock(return_value="must not write")
    monkeypatch.setattr(
        "app.services.llm.session_context_guard.get_session_context_termination",
        get_termination,
    )
    monkeypatch.setattr(
        "app.services.llm.session_context_guard.terminate_session_context",
        terminate,
    )

    reply = await channel_llm._call_agent_llm(
        _make_db(agent, model),
        agent.id,
        "继续处理",
        session_id=str(agent.id),
        user_id=agent.id,
    )

    assert reply == "继续成功"
    get_termination.assert_not_awaited()
    terminate.assert_not_awaited()


async def test_channel_turn_recovery_reloads_prefix_and_complete_durable_current_tail(monkeypatch):
    from app.services.llm.compactor import CompactionResult

    agent, model = _make_agent_and_model()
    model.keep_recent_turns = 8
    fallback_model = _make_model(model_name="fallback-model")
    fallback_model.keep_recent_turns = 12
    agent.fallback_model_id = fallback_model.id
    anchor = uuid.uuid4()
    compact = AsyncMock(return_value=CompactionResult(triggered=True, required=True))
    load_recoverable = AsyncMock(
        return_value=[
            {"role": "user", "content": "<conversation-summary>old</conversation-summary>"},
            {"role": "user", "content": "当前问题原文"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "done-1", "type": "function", "function": {"name": "grep", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "done-1", "content": "durable-result"},
        ]
    )
    monkeypatch.setattr("app.services.llm.compactor.maybe_compact", compact)
    monkeypatch.setattr(
        "app.services.chat_history.load_recoverable_history_for_turn",
        load_recoverable,
    )

    class _SessionContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(channel_llm, "async_session", lambda: _SessionContext())
    recovered = []

    async def fake_llm(*_args, context_recovery=None, primary_model=None, **_kwargs):
        assert context_recovery is not None
        recovered.extend(
                await context_recovery(
                    primary_model,
                    SimpleNamespace(
                            authoritative_prompt_tokens=None,
                            provider_overflow=True,
                            keep_recent_turns_override=12,
                            fits=False,
                    ),
                )
        )
        return "恢复成功"

    monkeypatch.setattr("app.services.llm.call_llm_with_failover", fake_llm, raising=False)

    reply = await channel_llm._call_agent_llm(
        _make_db(agent, model, fallback_model),
        agent.id,
        "当前问题原文",
        session_id=str(agent.id),
        user_id=agent.id,
        history=[{"role": "user", "content": "旧历史"}],
        turn_anchor_id=anchor,
    )

    assert reply == "恢复成功"
    assert recovered == [
        {"role": "user", "content": "<conversation-summary>old</conversation-summary>"},
        {"role": "user", "content": "当前问题原文"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "done-1", "type": "function", "function": {"name": "grep", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "done-1", "content": "durable-result"},
    ]
    assert compact.await_args.kwargs["force_required"] is True
    assert compact.await_args.kwargs["current_anchor_id"] == anchor
    assert compact.await_args.kwargs["keep_recent_turns_override"] == 12


async def test_im_turn_broadcasts_events_to_web_session(monkeypatch):
    """An IM-driven turn mirrors its live stream to web clients viewing the SAME
    session, so a DingTalk/Feishu conversation updates in real time in the web UI
    (not only on reload). The terminal event is deliberately absent here: the
    durable final-reply writer publishes it only after commit."""
    agent, model = _make_agent_and_model()

    import app.api.websocket as ws_mod

    sent: list[tuple[str, str, dict]] = []

    async def _fake_send_to_session(agent_id, session_id, payload):
        sent.append((session_id, payload.get("type"), payload))

    monkeypatch.setattr(ws_mod.manager, "send_to_session", _fake_send_to_session)
    # Keep this a pure wiring test — no real DB writes / compaction.
    monkeypatch.setattr("app.services.chat_history.persist_tool_call", AsyncMock(), raising=False)
    from app.services.llm.compactor import CompactionResult

    monkeypatch.setattr(
        "app.services.llm.compactor.maybe_precompact_prompt",
        AsyncMock(return_value=CompactionResult(triggered=False, required=False)),
        raising=False,
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
        _make_db(agent, model),
        agent.id,
        "看销售",
        session_id="sess-123",
        user_id=agent.id,
        web_broadcast_targets=[
            (
                "project-leader",
                "project-group",
                {
                    "message_id": "project-stream-1",
                    "sender_agent_id": str(agent.id),
                    "sender_name": agent.name,
                },
            )
        ],
    )

    assert reply == "昨天销售额 5050"
    original = [payload for session_id, _type, payload in sent if session_id == "sess-123"]
    mirrored = [payload for session_id, _type, payload in sent if session_id == "project-group"]
    types = [payload["type"] for payload in original]
    assert len(original) == len(mirrored)
    assert all(payload["message_id"] == "project-stream-1" for payload in mirrored)
    assert all(payload["sender_name"] == agent.name for payload in mirrored)
    for expected in ("thinking", "chunk", "tool_call"):
        assert expected in types, f"web viewer must receive the {expected!r} event of an IM turn"
        assert expected in [payload["type"] for payload in mirrored]
    assert "done" not in types


async def test_broadcast_channel_user_message_emits_event(monkeypatch):
    """The inbound IM user message is mirrored to web viewers of the session with
    its clean content + sender, so the user's own bubble appears live (not only on
    reload). Pins the event shape the frontend handler consumes."""
    import app.api.websocket as ws_mod

    captured: list = []

    async def _fake_send_to_session(agent_id, session_id, payload):
        captured.append((agent_id, session_id, payload))

    monkeypatch.setattr(ws_mod.manager, "send_to_session", _fake_send_to_session)

    message = SimpleNamespace(
        id=uuid.uuid4(),
        role="user",
        content="[file:a.jpg]\n只看 report 的数据",
        created_at=None,
        message_meta={"source_channel": "dingtalk"},
        thinking=None,
    )
    await channel_llm.broadcast_channel_user_message(
        "agent-1", "sess-9", message=message, sender_name="刘喜", user_id="u-7"
    )

    assert len(captured) == 1
    agent_id, session_id, payload = captured[0]
    assert agent_id == "agent-1" and session_id == "sess-9"
    assert payload["type"] == "channel_user_message"
    assert payload["id"] == str(message.id)
    assert payload["content"] == "[file:a.jpg]\n只看 report 的数据"
    assert payload["display_content"] == "只看 report 的数据"
    assert [item["display_name"] for item in payload["attachments"]] == ["a.jpg"]
    assert payload["sender_name"] == "刘喜"
    assert payload["user_id"] == "u-7"


async def test_broadcast_channel_user_message_no_session_noop(monkeypatch):
    import app.api.websocket as ws_mod

    captured: list = []

    async def _fake(*_a, **_k):
        captured.append(1)

    monkeypatch.setattr(ws_mod.manager, "send_to_session", _fake)
    message = SimpleNamespace(
        id=uuid.uuid4(), role="user", content="x", created_at=None, message_meta={}, thinking=None
    )
    await channel_llm.broadcast_channel_user_message("a", "", message=message)
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


async def test_all_loop_reaction_hooks_time_out_without_freezing_turn(monkeypatch):
    """Chunk/thinking/tool reaction callbacks share one strict time boundary."""

    agent, model = _make_agent_and_model()
    from app.services import channel_dispatch

    monkeypatch.setattr(channel_dispatch, "CHANNEL_REACTION_HOOK_TIMEOUT_SECONDS", 0.01)
    started: list[str] = []
    cancelled: list[str] = []

    def never_returning(tag: str):
        async def _hook(_value):
            started.append(tag)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(tag)

        return _hook

    async def fake_llm(*_args, **kwargs):
        await kwargs["on_chunk"]("chunk")
        await kwargs["on_thinking"]("thinking")
        await kwargs["on_tool_call"](
            {"name": "read_file", "call_id": "1", "args": {}, "status": "done", "result": "ok"}
        )
        return "turn-completed"

    _patch_llm(monkeypatch, fake_llm)
    reply = await asyncio.wait_for(
        channel_llm._call_agent_llm(
            _make_db(agent, model),
            agent.id,
            "test",
            session_id="",
            user_id=agent.id,
            on_chunk=never_returning("chunk"),
            on_thinking=never_returning("thinking"),
            on_tool_call=never_returning("tool"),
        ),
        timeout=0.5,
    )

    assert reply == "turn-completed"
    await asyncio.sleep(0)
    assert set(started) == {"chunk", "thinking", "tool"}
    assert set(cancelled) == {"chunk", "thinking", "tool"}
