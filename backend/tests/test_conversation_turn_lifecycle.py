"""Observable PostgreSQL coverage for the normalized conversation lifecycle."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction  # noqa: F401 - resolves ChatMessage FK graph
from app.models.chat_session import ChatSession
from app.models.participant import Participant  # noqa: F401 - resolves ChatSession FK graph
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.conversation_turn_lifecycle import (
    ConversationTurnConflict,
    get_conversation_turn_snapshot,
    transition_conversation_turn,
    with_turn_envelope,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_after_test():
    yield
    await engine.dispose()


async def _make_conversation() -> tuple[uuid.UUID, uuid.UUID, ChatSession]:
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        tenant = Tenant(name=f"turn_{suffix}", slug=f"turn-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"turn_{suffix}",
            email=f"turn_{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="Turn Tester",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(name=f"Turn Agent {suffix}", creator_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.flush()
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="Lifecycle",
            source_channel="web",
            external_conv_id=f"turn-{suffix}",
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return agent.id, user.id, session


async def _add_anchor(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    content: str,
) -> uuid.UUID:
    async with async_session() as db:
        anchor = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            role="user",
            content=content,
            conversation_id=str(session_id),
        )
        db.add(anchor)
        await db.commit()
        await db.refresh(anchor)
        return anchor.id


async def _transition(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    anchor_id: uuid.UUID,
    status: str,
):
    async with async_session() as db:
        snapshot = await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(session_id),
            turn_anchor_id=anchor_id,
            status=status,
        )
        await db.commit()
        return snapshot


async def test_lifecycle_is_durable_versioned_and_terminally_monotonic():
    agent_id, user_id, session = await _make_conversation()
    anchor_id = await _add_anchor(agent_id, user_id, session.id, "first")

    running = await _transition(agent_id, session.id, anchor_id, "running")
    suspended = await _transition(agent_id, session.id, anchor_id, "suspended")
    resumed = await _transition(agent_id, session.id, anchor_id, "running")
    completed = await _transition(agent_id, session.id, anchor_id, "completed")
    duplicate = await _transition(agent_id, session.id, anchor_id, "completed")

    assert (running.generation, running.revision, running.phase) == (1, 1, "active")
    assert (suspended.generation, suspended.revision, suspended.phase) == (1, 2, "suspended")
    assert (resumed.generation, resumed.revision, resumed.phase) == (1, 3, "active")
    assert (completed.generation, completed.revision, completed.phase) == (1, 4, "idle")
    assert duplicate == completed

    with pytest.raises(ValueError, match="terminal conversation turn"):
        await _transition(agent_id, session.id, anchor_id, "running")


async def test_old_terminal_publish_keeps_exact_anchor_after_new_generation_starts(monkeypatch):
    from app.services.conversation_turn_lifecycle import publish_committed_turn_terminal

    agent_id, user_id, session = await _make_conversation()
    first = await _add_anchor(agent_id, user_id, session.id, "first")
    second = await _add_anchor(agent_id, user_id, session.id, "second")

    await _transition(agent_id, session.id, first, "running")
    await _transition(agent_id, session.id, first, "completed")
    current = await _transition(agent_id, session.id, second, "running")

    async with async_session() as db:
        loaded = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(session.id),
        )

    assert current.generation == 2
    assert loaded == current
    assert with_turn_envelope({"type": "thinking"}, loaded, event_kind="turn_stream") == {
        "type": "thinking",
        "event_kind": "turn_stream",
        "turn": {
            "turn_anchor_id": str(second),
            "generation": 2,
            "revision": 1,
            "status": "running",
            "phase": "active",
        },
    }

    published: list[dict] = []

    async def capture(_agent_id: str, _session_id: str, payload: dict):
        published.append(payload)

    monkeypatch.setattr("app.api.websocket.manager.send_to_session", capture)
    await publish_committed_turn_terminal(
        agent_id=agent_id,
        conversation_id=str(session.id),
        turn_anchor_id=first,
        message_id=uuid.uuid4(),
        content="late first terminal",
    )

    assert len(published) == 1
    assert published[0]["content"] == "late first terminal"
    assert published[0]["turn"] == {
        "turn_anchor_id": str(first),
        "generation": 1,
        "revision": 2,
        "status": "completed",
        "phase": "idle",
    }


async def test_concurrent_admission_allows_only_one_active_turn_per_conversation():
    agent_id, user_id, session = await _make_conversation()
    first = await _add_anchor(agent_id, user_id, session.id, "first")
    second = await _add_anchor(agent_id, user_id, session.id, "second")

    outcomes = await asyncio.gather(
        _transition(agent_id, session.id, first, "running"),
        _transition(agent_id, session.id, second, "running"),
        return_exceptions=True,
    )

    snapshots = [item for item in outcomes if not isinstance(item, BaseException)]
    conflicts = [item for item in outcomes if isinstance(item, ConversationTurnConflict)]
    assert len(snapshots) == 1
    assert (snapshots[0].generation, snapshots[0].revision) == (1, 1)
    assert len(conflicts) == 1


async def test_stale_resume_cannot_reclaim_newer_session_pointer():
    agent_id, user_id, session = await _make_conversation()
    first = await _add_anchor(agent_id, user_id, session.id, "first")
    second = await _add_anchor(agent_id, user_id, session.id, "second")
    await _transition(agent_id, session.id, first, "running")
    await _transition(agent_id, session.id, first, "suspended")

    # Model the only dangerous interleaving directly: a newer generation has
    # become current before an old continuation attempts to resume.
    async with async_session() as db:
        second_row = await db.get(ChatMessage, second)
        durable_session = await db.get(ChatSession, session.id)
        second_row.message_meta = {
            "conversation_turn_lifecycle": True,
            "turn_anchor_id": str(second),
            "turn_generation": 2,
            "turn_revision": 1,
            "turn_status": "running",
        }
        durable_session.im_config = {
            **dict(durable_session.im_config or {}),
            "conversation_turn": {
                "turn_anchor_id": str(second),
                "generation": 2,
                "revision": 1,
                "status": "running",
            },
        }
        await db.commit()

    with pytest.raises(ConversationTurnConflict, match="no longer owns"):
        await _transition(agent_id, session.id, first, "running")

    stale_terminal = await _transition(agent_id, session.id, first, "failed")
    async with async_session() as db:
        current = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(session.id),
        )
    assert stale_terminal.status == "failed"
    assert current.anchor_id == second
    assert (current.generation, current.status) == (2, "running")


async def test_terminal_write_and_stop_share_one_lock_order_without_deadlock():
    from app.services.chat_history import mark_turn_cancelled, persist_assistant_reply_row

    agent_id, user_id, session = await _make_conversation()
    anchor_id = await _add_anchor(agent_id, user_id, session.id, "race")
    await _transition(agent_id, session.id, anchor_id, "running")

    async def finish():
        async with async_session() as db:
            message_id = await persist_assistant_reply_row(
                db,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=str(session.id),
                content="finished",
                turn_anchor_id=anchor_id,
            )
            await db.commit()
            return message_id

    async def stop():
        async with async_session() as db:
            result = await mark_turn_cancelled(
                db,
                agent_id=agent_id,
                conversation_id=str(session.id),
                turn_anchor_id=anchor_id,
                reason="race test",
            )
            await db.commit()
            return result

    await asyncio.wait_for(
        asyncio.gather(finish(), stop(), return_exceptions=True),
        timeout=3,
    )
    async with async_session() as db:
        snapshot = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(session.id),
        )
        assistant_count = await db.scalar(
            select(func.count(ChatMessage.id)).where(
                ChatMessage.conversation_id == str(session.id),
                ChatMessage.role == "assistant",
                ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor_id),
            )
        )
    assert snapshot.status in {"completed", "cancelled"}
    assert snapshot.revision == 2
    assert assistant_count == (1 if snapshot.status == "completed" else 0)


async def test_shared_final_writer_publishes_terminal_only_after_commit(monkeypatch):
    from app.api import websocket as websocket_module
    from app.services.chat_history import persist_assistant_reply

    agent_id, user_id, session = await _make_conversation()
    anchor_id = await _add_anchor(agent_id, user_id, session.id, "finish")
    await _transition(agent_id, session.id, anchor_id, "running")
    published: list[dict] = []

    async def capture_after_commit(_agent_id: str, _session_id: str, payload: dict):
        async with async_session() as db:
            assistant = await db.get(ChatMessage, uuid.UUID(payload["message_id"]))
            anchor = await db.get(ChatMessage, anchor_id)
        assert assistant is not None
        assert anchor.message_meta["turn_status"] == "completed"
        published.append(payload)

    monkeypatch.setattr(websocket_module.manager, "send_to_session", capture_after_commit)

    message_id = await persist_assistant_reply(
        async_session,
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=str(session.id),
        content="done",
        turn_anchor_id=anchor_id,
        required=True,
    )

    assert message_id is not None
    assert len(published) == 1
    assert published[0]["type"] == "done"
    assert published[0]["event_kind"] == "turn_terminal"
    assert published[0]["turn"]["generation"] == 1
    assert published[0]["turn"]["revision"] == 2
    assert published[0]["turn"]["phase"] == "idle"


async def test_openclaw_queue_and_report_share_the_same_durable_turn(monkeypatch):
    from app.api import gateway as gateway_api
    from app.api.websocket import WebSocketChatHandler
    from app.models.gateway_message import GatewayMessage
    from app.schemas.schemas import GatewayReportRequest

    agent_id, user_id, session = await _make_conversation()
    anchor_id = await _add_anchor(agent_id, user_id, session.id, "remote")
    handler = WebSocketChatHandler(
        websocket=SimpleNamespace(),
        agent_id=agent_id,
        token="test",
        session_id=str(session.id),
    )
    handler.user_id = user_id
    handler.conv_id = str(session.id)
    handler._safe_send = AsyncMock()
    handler._publish_turn_lifecycle = AsyncMock()

    snapshot = await _transition(agent_id, session.id, anchor_id, "running")
    await handler._route_openclaw(
        "remote",
        turn_anchor_id=anchor_id,
        turn_snapshot=snapshot,
    )

    async with async_session() as db:
        gateway_message = (
            await db.execute(
                select(GatewayMessage).where(
                    GatewayMessage.conversation_id == str(session.id)
                )
            )
        ).scalar_one()
        anchor = await db.get(ChatMessage, anchor_id)
    assert anchor.message_meta["gateway_message_id"] == str(gateway_message.id)
    assert anchor.message_meta["turn_status"] == "running"

    async def fake_gateway_agent(_key, db):
        return await db.get(Agent, agent_id)

    monkeypatch.setattr(gateway_api, "_get_agent_by_key", fake_gateway_agent)
    monkeypatch.setattr(
        "app.services.trigger_runtime.evaluator.match_incoming_chat_message",
        AsyncMock(),
    )
    published: list[dict] = []

    async def capture_after_commit(_agent_id: str, _session_id: str, payload: dict):
        async with async_session() as verify_db:
            verify_anchor = await verify_db.get(ChatMessage, anchor_id)
            terminal = await verify_db.get(ChatMessage, uuid.UUID(payload["message_id"]))
        assert verify_anchor.message_meta["turn_status"] == "completed"
        assert terminal is not None
        published.append(payload)

    monkeypatch.setattr("app.api.websocket.manager.send_to_session", capture_after_commit)
    async with async_session() as db:
        response = await gateway_api.report_result(
            GatewayReportRequest(message_id=gateway_message.id, result="remote done"),
            x_api_key="test",
            db=db,
        )

    assert response == {"status": "ok"}
    assert len(published) == 1
    assert published[0]["event_kind"] == "turn_terminal"
    assert published[0]["turn"]["phase"] == "idle"
