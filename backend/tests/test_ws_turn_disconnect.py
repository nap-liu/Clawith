"""Web-chat turn lifecycle vs. transport lifecycle (`_await_turn_with_abort`).

Regression guards for the "agent gave no reply" bug: a web turn that is mid
tool-loop when the browser WebSocket drops used to be *cancelled*, discarding the
assistant reply (only the incremental tool rows survived). The turn is a unit of
work and must run to completion + persist regardless of the socket — parity with
the IM / trigger channels, which are connection-independent.

These tests pin the drive-loop invariant directly (no live socket needed):

- explicit user **abort** → cancel the turn, return the partial + stop marker
- client **disconnect** → DO NOT cancel; let the turn finish, return its real reply
- normal **completion** → return the reply as "completed"
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect

from app.api.websocket import WebSocketChatHandler, _await_turn_with_abort
from app.services.conversation_turn_lifecycle import ConversationTurnSnapshot
from app.services.workload_capacity import WorkloadCapacity, WorkloadKind
from app.services.workload_capacity import WorkloadOverloadedError

pytestmark = pytest.mark.asyncio


async def test_disconnect_does_not_cancel_turn_and_keeps_reply():
    """A mid-turn disconnect must let the turn finish and surface its real reply.

    This is the core regression: previously the disconnect path called
    ``llm_task.cancel()`` + re-raised, so the assistant reply (line ~939 persist)
    never ran. Now the task runs to completion and its result is returned for
    the caller to persist."""

    async def _turn():
        await asyncio.sleep(0.05)  # still "running" when the client drops
        return "最终回复"

    task = asyncio.create_task(_turn())

    async def _recv():
        # Simulate the browser connection dropping mid-turn.
        raise WebSocketDisconnect(code=1006)

    resp, outcome = await _await_turn_with_abort(task, _recv, [])

    assert outcome == "disconnected"
    assert resp == "最终回复", "the reply must NOT be lost on disconnect"
    assert task.done() and not task.cancelled(), "the turn must run to completion"


async def test_abort_cancels_turn_and_returns_partial():
    """An explicit user abort still cancels and returns the partial + marker."""

    async def _turn():
        await asyncio.sleep(5)  # long-running; expected to be cancelled
        return "should not reach"

    task = asyncio.create_task(_turn())

    async def _recv():
        return {"type": "abort"}

    resp, outcome = await _await_turn_with_abort(task, _recv, ["写了一半"])

    assert outcome == "aborted"
    assert "写了一半" in resp and "*[Generation stopped]*" in resp
    assert task.cancelled(), "an explicit abort must cancel the turn"


async def test_abort_with_no_partial_returns_stop_marker_only():
    async def _turn():
        await asyncio.sleep(5)
        return "nope"

    task = asyncio.create_task(_turn())

    async def _recv():
        return {"type": "abort"}

    resp, outcome = await _await_turn_with_abort(task, _recv, [])
    assert outcome == "aborted"
    assert resp == "*[Generation stopped]*"


async def test_normal_completion_returns_reply():
    """When the turn finishes before any abort/disconnect, it returns 'completed'."""

    async def _turn():
        return "正常回复"

    task = asyncio.create_task(_turn())

    async def _recv():
        # The client stays quiet; recv never resolves within the turn.
        await asyncio.sleep(10)
        return {}

    resp, outcome = await _await_turn_with_abort(task, _recv, [])
    assert outcome == "completed"
    assert resp == "正常回复"


async def test_external_control_plane_cancel_is_normalized_as_abort():
    async def _turn():
        await asyncio.sleep(5)
        return "nope"

    task = asyncio.create_task(_turn())

    async def _recv():
        await asyncio.sleep(10)
        return {}

    asyncio.get_running_loop().call_later(0.01, task.cancel)
    resp, outcome = await _await_turn_with_abort(task, _recv, ["partial"])

    assert outcome == "aborted"
    assert resp == "partial\n\n*[Generation stopped]*"


async def test_completed_commit_wins_cancel_race_and_still_sends_done(monkeypatch):
    handler = WebSocketChatHandler(
        websocket=SimpleNamespace(),
        agent_id=uuid.uuid4(),
        token="test",
        session_id=str(uuid.uuid4()),
    )
    handler.user_id = uuid.uuid4()
    handler.tenant_id = uuid.uuid4()
    handler.conv_id = handler.session_id_param
    handler.conversation = [{"role": "user", "content": "hello"}]
    capacity = WorkloadCapacity(
        global_limit=1,
        tenant_limit=1,
        category_limits={kind: 1 for kind in WorkloadKind},
        default_timeout_seconds=0.01,
        instance_id="websocket-test",
    )
    monkeypatch.setattr("app.api.websocket.get_workload_capacity", lambda: capacity)

    async def run_llm_inside_capacity(*_args, **_kwargs):
        snapshot = await capacity.snapshot()
        assert snapshot.categories["interactive"].active == 0
        return "completed reply", [], [], "completed", True

    handler._run_llm_and_stream = run_llm_inside_capacity
    handler._safe_send = AsyncMock()
    running_snapshot = ConversationTurnSnapshot(
        anchor_id=uuid.uuid4(), generation=1, revision=1, status="running"
    )
    completed_snapshot = ConversationTurnSnapshot(
        anchor_id=running_snapshot.anchor_id,
        generation=1,
        revision=2,
        status="completed",
    )
    handler._transition_turn = AsyncMock(return_value=running_snapshot)
    handler._publish_turn_lifecycle = AsyncMock()
    handler._load_turn_snapshot = AsyncMock(return_value=completed_snapshot)
    save_calls: list[uuid.UUID] = []

    async def save_with_commit_race(*_args, message_id, **_kwargs):
        save_calls.append(message_id)
        if len(save_calls) == 1:
            # Model a commit that succeeded server-side just before the outer
            # task received cancellation. The retry sees the same durable id.
            raise asyncio.CancelledError
        return False

    monkeypatch.setattr(handler, "_save_assistant_reply", save_with_commit_race)

    disposition = await handler._execute_web_turn(
        effective_llm_model=SimpleNamespace(),
        is_onboarding_trigger=False,
        onboarding_claim=None,
        turn_anchor_id=uuid.uuid4(),
        turn_snapshot=running_snapshot,
        task_match=None,
    )

    assert disposition == "continue"
    assert save_calls[0] == save_calls[1]
    assert handler.conversation[-1] == {
        "role": "assistant",
        "content": "completed reply",
    }
    handler._safe_send.assert_awaited_once_with(
        {
            "type": "done",
            "role": "assistant",
            "content": "completed reply",
            "message_id": str(save_calls[0]),
            "event_kind": "turn_terminal",
            "turn": completed_snapshot.to_client_dict(),
        }
    )


async def test_failed_onboarding_closes_hidden_turn_before_skip_event():
    handler = WebSocketChatHandler(
        websocket=SimpleNamespace(),
        agent_id=uuid.uuid4(),
        token="test",
        session_id=str(uuid.uuid4()),
    )
    handler.user_id = uuid.uuid4()
    handler.conv_id = handler.session_id_param
    handler.conversation = [{"role": "user", "content": "Please begin the onboarding."}]
    anchor_id = uuid.uuid4()
    running = ConversationTurnSnapshot(anchor_id, 1, 1, "running")
    failed = ConversationTurnSnapshot(anchor_id, 1, 2, "failed")
    handler._run_llm_and_stream = AsyncMock(
        return_value=("", [], [], "failed", False)
    )
    handler._transition_turn = AsyncMock(return_value=failed)
    handler._publish_turn_lifecycle = AsyncMock()
    handler._safe_send = AsyncMock()

    disposition = await handler._execute_web_turn(
        effective_llm_model=SimpleNamespace(),
        is_onboarding_trigger=True,
        onboarding_claim=None,
        turn_anchor_id=anchor_id,
        turn_snapshot=running,
        task_match=None,
    )

    assert disposition == "continue"
    handler._transition_turn.assert_awaited_once_with(anchor_id, "failed")
    handler._safe_send.assert_awaited_once_with(
        {
            "type": "onboarding_skipped",
            "reason": "generation_failed",
            "agent_id": str(handler.agent_id),
            "event_kind": "turn_terminal",
            "turn": failed.to_client_dict(),
        }
    )


async def test_capacity_rejection_never_admits_durable_turn(monkeypatch):
    handler = WebSocketChatHandler(
        websocket=SimpleNamespace(),
        agent_id=uuid.uuid4(),
        token="test",
        session_id=str(uuid.uuid4()),
    )
    handler.user_id = uuid.uuid4()
    handler.tenant_id = uuid.uuid4()
    handler.conv_id = handler.session_id_param
    handler.conversation = [{"role": "user", "content": "hello"}]
    capacity = WorkloadCapacity(
        global_limit=1,
        tenant_limit=1,
        category_limits={kind: 1 for kind in WorkloadKind},
        default_timeout_seconds=0.01,
        instance_id="websocket-capacity-rejection",
    )
    monkeypatch.setattr("app.api.websocket.get_workload_capacity", lambda: capacity)
    async with capacity.slot(WorkloadKind.INTERACTIVE, handler.tenant_id):
        with pytest.raises(WorkloadOverloadedError):
            await capacity.acquire(
                WorkloadKind.INTERACTIVE,
                handler.tenant_id,
            )


async def test_pre_admission_rejection_is_not_a_turn_terminal(monkeypatch):
    from app.services.quota_guard import QuotaExceeded

    handler = WebSocketChatHandler(
        websocket=SimpleNamespace(),
        agent_id=uuid.uuid4(),
        token="test",
        session_id=str(uuid.uuid4()),
    )
    handler.user_id = uuid.uuid4()
    handler.conv_id = handler.session_id_param
    handler.current_client_message_id = "optimistic-user-1"
    current_snapshot = ConversationTurnSnapshot(
        anchor_id=uuid.uuid4(),
        generation=7,
        revision=3,
        status="running",
    )
    handler._load_turn_snapshot = AsyncMock(return_value=current_snapshot)
    handler._safe_send = AsyncMock()

    async def reject_quota(_user_id):
        raise QuotaExceeded("quota reached")

    monkeypatch.setattr("app.api.websocket.check_conversation_quota", reject_quota)

    assert await handler._check_quotas() is False
    handler._safe_send.assert_awaited_once_with(
        {
            "type": "error",
            "content": "⚠️ quota reached",
            "rejected_message_id": "optimistic-user-1",
            "event_kind": "turn_rejected",
            "turn": current_snapshot.to_client_dict(),
        }
    )


async def test_websocket_turn_wires_parent_subagent_drain_into_round_hook(monkeypatch):
    """A live Web turn must expose the shared parent-event drain to the caller."""

    class _Result:
        @staticmethod
        def scalar_one_or_none():
            return SimpleNamespace()

    class _Db:
        @staticmethod
        async def execute(_statement):
            return _Result()

    class _DbContext:
        async def __aenter__(self):
            return _Db()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = str(uuid.uuid4())
    anchor_id = uuid.uuid4()

    async def quiet_receive():
        await asyncio.sleep(10)
        return {}

    handler = WebSocketChatHandler(
        websocket=SimpleNamespace(receive_json=quiet_receive),
        agent_id=agent_id,
        token="test",
        session_id=session_id,
    )
    handler.user_id = user_id
    handler.conv_id = session_id
    handler.agent_name = "Parent"
    handler.conversation = []
    handler._update_activity_and_quota = AsyncMock()

    drain_calls: list[dict] = []

    async def fake_drain(**kwargs):
        drain_calls.append(kwargs)
        return [{"role": "user", "content": "late child result"}]

    captured_round_input: list[dict] = []

    async def fake_llm(**kwargs):
        captured_round_input.extend(await kwargs["before_round"](0))
        return "merged web reply"

    async def no_onboarding(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.api.websocket.async_session", lambda: _DbContext())
    monkeypatch.setattr("app.api.websocket.resolve_onboarding_prompt", no_onboarding)
    monkeypatch.setattr("app.api.websocket.call_llm_with_failover", fake_llm)
    monkeypatch.setattr("app.services.subagent_runtime.drain_parent_subagent_events", fake_drain)

    result = await handler._run_llm_and_stream(
        SimpleNamespace(model="test-model"),
        False,
        turn_anchor_id=anchor_id,
    )

    assert result[0] == "merged web reply"
    assert result[3] == "completed"
    assert captured_round_input == [{"role": "user", "content": "late child result"}]
    assert drain_calls == [
        {
            "parent_session_id": session_id,
            "active_turn_anchor_id": anchor_id,
            "execution_agent_id": agent_id,
            "execution_user_id": user_id,
        }
    ]
