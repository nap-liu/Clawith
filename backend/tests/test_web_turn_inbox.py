"""Web and H5 feed the shared durable inbox while one turn owns the Session."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api import websocket as api
from app.api.websocket_inbox_ops import receive_followup, receive_turn_message
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.conversation_turn_lifecycle import conversation_turn_snapshot_for_session, transition_conversation_turn
from app.services.subagent_runtime_parent_round import build_parent_subagent_before_round
from tests.test_subagent_runtime import _make_context

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def isolate_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def running_handler(monkeypatch, channel="web"):
    agent_id, user_id, session_id, anchor_id = await _make_context()
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        session = await db.get(ChatSession, session_id)
        session.source_channel = channel
        anchor = await db.get(ChatMessage, anchor_id)
        anchor.message_meta = {"model_id": str(agent.primary_model_id), "reasoning_effort": "low"}
        snapshot = await transition_conversation_turn(db, agent_id=agent_id,
            conversation_id=str(session_id), turn_anchor_id=anchor_id, status="running")
        await db.commit()
    handler = api.WebSocketChatHandler(AsyncMock(), agent_id, "unused", channel=channel)
    handler.conv_id = str(session_id)
    handler.user_id = user_id
    handler.tenant_id = agent.tenant_id
    handler.history_messages = []
    handler.conversation = [{"role": "user", "content": "parent request"}]
    handler._safe_send = AsyncMock()
    handler._check_quotas = AsyncMock(return_value=True)
    monkeypatch.setattr(api, "publish_conversation_turn_event", AsyncMock())
    monkeypatch.setattr(api, "maybe_mark_session_read_for_active_viewer", AsyncMock())
    return handler, snapshot


@pytest.mark.parametrize("channel", ["web", "miniprogram", "wechat_miniprogram"])
async def test_same_socket_followup_consumed_once_before_turn_completes(monkeypatch, channel):
    handler, snapshot = await running_handler(monkeypatch, channel)
    frames = asyncio.Queue()
    message_id = str(uuid.uuid4())
    frame = {"message_id": message_id, "content": "Additional requirement", "attachments": []}
    await frames.put(frame)
    await frames.put(frame)
    accepted = asyncio.Event()
    received = 0
    observed = []

    async def on_message(data):
        nonlocal received
        await receive_followup(api, handler, data)
        received += 1
        if received == 2:
            accepted.set()

    async def model_turn():
        await asyncio.wait_for(accepted.wait(), timeout=5)
        hook = build_parent_subagent_before_round(parent_session_id=handler.conv_id,
            active_turn_anchor_id=snapshot.anchor_id, execution_agent_id=handler.agent_id,
            execution_user_id=handler.user_id, include_turn_inbox=True)
        observed.extend(await hook(1))
        assert await hook(2) == []
        return "Requirement incorporated"

    reply, outcome = await api._await_turn_with_abort(asyncio.create_task(model_turn()),
        frames.get, [], on_message=on_message)
    assert outcome == "completed"
    assert reply == "Requirement incorporated"
    assert observed == [{"role": "user", "content": "Additional requirement"}]
    assert handler.conversation == [{"role": "user", "content": "parent request"}]
    async with async_session() as db:
        rows = (await db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == handler.conv_id,
            ChatMessage.content == "Additional requirement"))).all()
        assert len(rows) == 1
        assert rows[0].message_meta["turn_inbox_state"] == "delivered"
        assert rows[0].message_meta["turn_inbox_anchor_id"] == str(snapshot.anchor_id)
        assert rows[0].message_meta["reasoning_effort"] == "low"
        session = await db.get(ChatSession, uuid.UUID(handler.conv_id))
        assert conversation_turn_snapshot_for_session(session) == snapshot


async def test_continue_during_active_turn_remains_a_control_command(monkeypatch):
    handler, snapshot = await running_handler(monkeypatch)
    await receive_followup(api, handler, {"content": "/continue", "message_id": str(uuid.uuid4())})
    async with async_session() as db:
        rows = (await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == handler.conv_id,
        ))).all()
        assert not any(row.role == "user" and row.content == "/continue" for row in rows)
        replies = [row for row in rows if (row.message_meta or {}).get("command_action") == "continue_busy"]
        assert len(replies) == 1
        assert replies[0].message_meta["turn_control_only"] is True
        session = await db.get(ChatSession, uuid.UUID(handler.conv_id))
        assert conversation_turn_snapshot_for_session(session) == snapshot


async def test_other_socket_admission_uses_same_generation(monkeypatch):
    handler, snapshot = await running_handler(monkeypatch)
    new_id, consumed, _, _, _, received_snapshot = await handler._save_user_message(
        "From another tab", "", "", False, client_message_id=str(uuid.uuid4()), attachments=[])
    assert consumed
    assert new_id != snapshot.anchor_id
    assert received_snapshot == snapshot
    assert handler.last_ingest_result.queued_to_running_turn


async def test_late_followup_promotes_after_terminal_commit(monkeypatch):
    handler, snapshot = await running_handler(monkeypatch)
    resume = []
    monkeypatch.setattr("app.services.turn_inbox.schedule_durable_turn_resume", resume.append)
    await receive_followup(api, handler, {"message_id": str(uuid.uuid4()), "content": "Late requirement"})
    await handler._save_assistant_reply("First reply", [], turn_anchor_id=snapshot.anchor_id)
    assert len(resume) == 1
    assert resume[0].content == "Late requirement"
    async with async_session() as db:
        session = await db.get(ChatSession, uuid.UUID(handler.conv_id))
        promoted = conversation_turn_snapshot_for_session(session)
        assert promoted.anchor_id == resume[0].id
        assert promoted.generation == snapshot.generation + 1
        assert promoted.status == "running"


async def test_read_only_and_changed_session_identity_cannot_append(monkeypatch):
    handler, _ = await running_handler(monkeypatch)
    handler.read_only = True
    await receive_followup(api, handler, {"content": "Not authorized"})
    assert handler._safe_send.await_count == 0
    handler.read_only = False
    handler.user_id = uuid.uuid4()
    with pytest.raises(PermissionError):
        await handler._save_user_message("Not authorized", "", "", False)
    async with async_session() as db:
        assert not (await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == handler.conv_id, ChatMessage.content == "Not authorized"))).all()


async def test_rejected_attachment_does_not_interrupt_active_turn(monkeypatch):
    handler, snapshot = await running_handler(monkeypatch)
    client_id = str(uuid.uuid4())
    await receive_followup(api, handler, {
        "message_id": client_id, "content": "Invalid attachment", "attachments": "invalid",
    })
    event = handler.websocket.send_json.call_args.args[0]
    assert event["event_kind"] == "turn_rejected"
    assert event["rejected_message_id"] == client_id
    assert event["turn"] == snapshot.to_client_dict()
    async with async_session() as db:
        assert not (await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == handler.conv_id, ChatMessage.content == "Invalid attachment"))).all()


async def test_stop_cancels_pending_web_followup(monkeypatch):
    from app.services.turn_control import stop_session_turn_tree

    handler, snapshot = await running_handler(monkeypatch)
    monkeypatch.setattr("app.services.turn_control_bus.publish_turn_tree_stopped", AsyncMock())
    monkeypatch.setattr("app.services.turn_control.publish_conversation_turn_event", AsyncMock())
    await receive_followup(api, handler, {"content": "Pending requirement"})
    result = await stop_session_turn_tree(agent_id=handler.agent_id, session_id=handler.conv_id,
        reason="User stopped", expected_anchor_id=snapshot.anchor_id, expected_generation=snapshot.generation)
    assert result.stopped
    async with async_session() as db:
        row = await db.scalar(select(ChatMessage).where(
            ChatMessage.conversation_id == handler.conv_id, ChatMessage.content == "Pending requirement"))
        assert row.message_meta["turn_inbox_state"] == "cancelled"
        session = await db.get(ChatSession, uuid.UUID(handler.conv_id))
        assert conversation_turn_snapshot_for_session(session).status == "cancelled"


async def test_next_web_root_reloads_consumed_followup_from_history(monkeypatch):
    handler, snapshot = await running_handler(monkeypatch)
    await receive_followup(api, handler, {"content": "Remember this requirement"})
    hook = build_parent_subagent_before_round(parent_session_id=handler.conv_id,
        active_turn_anchor_id=snapshot.anchor_id, execution_agent_id=handler.agent_id,
        execution_user_id=handler.user_id, include_turn_inbox=True)
    await hook(1)
    await handler._save_assistant_reply("Previous reply", [], turn_anchor_id=snapshot.anchor_id)
    handler.websocket.receive_json.return_value = {"content": "Next question", "message_id": str(uuid.uuid4())}
    handler._load_scene_manifest = AsyncMock()
    handler._resolve_effective_model = AsyncMock(return_value=None)
    handler._wait_for_normal_turn_onboarding = AsyncMock()
    handler._enqueue_project_subagent_message = AsyncMock(return_value=False)
    handler._execute_web_turn = AsyncMock(return_value="disconnect")
    await handler.message_loop()
    contents = [message["content"] for message in handler.conversation]
    assert contents.count("Remember this requirement") == 1
    assert contents.count("Previous reply") == 1
    assert contents[-1] == "Next question"


async def test_send_detected_disconnect_keeps_model_running():
    from starlette.websockets import WebSocket, WebSocketDisconnect

    socket = WebSocket({"type": "websocket"},
        receive=AsyncMock(return_value={"type": "websocket.connect"}),
        send=AsyncMock(side_effect=[None, OSError("Peer closed")]))
    await socket.accept()
    with pytest.raises(WebSocketDisconnect):
        await socket.send_json({"type": "chunk", "content": "Partial"})
    task = asyncio.create_task(asyncio.sleep(0.01, result="Durable reply"))
    reply, outcome = await api._await_turn_with_abort(task,
        lambda: receive_turn_message(socket), ["Partial"])
    assert (reply, outcome) == ("Durable reply", "disconnected")
    assert not task.cancelled()
