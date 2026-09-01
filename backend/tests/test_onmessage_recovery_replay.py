"""Legacy recovery and recorded replay on-message tests."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from tests.test_onmessage_session_chain import (
    Agent,
    AgentTrigger,
    ChatMessage,
    ChatSession,
    Participant,
    TriggerExecution,
    _dispose_engine_between_tests,
    _make_agent,
    _session,
    async_session,
)

pytestmark = pytest.mark.asyncio


async def test_legacy_upgrade_floor_skips_pre_cutover_history():
    """An upgraded legacy trigger starts at cutover, not its historical cursor."""
    from app.services.trigger_runtime.evaluator import recover_legacy_on_message_events

    agent, user = await _make_agent()
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        session = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="web",
            external_conv_id=f"legacy-upgrade-{uuid.uuid4()}",
        )
        db.add(session)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=agent.id,
            name=f"legacy-upgrade-{uuid.uuid4().hex[:8]}",
            type="on_message",
            reason="do not replay pre-cutover history",
            config={
                "from_user_id": str(user.id),
                "_since_ts": (now - timedelta(minutes=10)).isoformat(),
                "_legacy_scan_floor": now.isoformat(),
            },
            is_enabled=True,
            fire_count=0,
            max_fires=10,
        )
        db.add_all(
            [
                trigger,
                ChatMessage(
                    agent_id=agent.id,
                    user_id=user.id,
                    sender_user_id=user.id,
                    role="user",
                    content="historical legacy event",
                    conversation_id=str(session.id),
                    created_at=now - timedelta(minutes=5),
                ),
                ChatMessage(
                    agent_id=agent.id,
                    user_id=user.id,
                    sender_user_id=user.id,
                    role="user",
                    content="at-cutover legacy event",
                    conversation_id=str(session.id),
                    created_at=now,
                ),
            ]
        )
        await db.commit()

    assert await recover_legacy_on_message_events(trigger) == 1
    async with async_session() as db:
        payloads = list(
            (
                await db.execute(select(TriggerExecution.payload).where(TriggerExecution.trigger_id == trigger.id))
            ).scalars()
        )
    assert [payload["_matched_message"] for payload in payloads] == ["at-cutover legacy event"]


async def test_legacy_recovery_ignores_exact_watch_session_trigger():
    """The compatibility scanner never handles exact session subscriptions."""
    from app.services.trigger_runtime.evaluator import recover_legacy_on_message_events

    agent, user = await _make_agent()
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        session = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="web",
            external_conv_id=f"exact-watch-{uuid.uuid4()}",
        )
        db.add(session)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=agent.id,
            name=f"exact-watch-{uuid.uuid4().hex[:8]}",
            type="on_message",
            reason="exact session path is not a legacy scan",
            config={
                "from_user_id": str(user.id),
                "_watch_session_id": str(session.id),
                "_since_ts": (now - timedelta(minutes=1)).isoformat(),
                "_legacy_scan_floor": now.isoformat(),
            },
            is_enabled=True,
            fire_count=0,
            max_fires=10,
        )
        db.add_all(
            [
                trigger,
                ChatMessage(
                    agent_id=agent.id,
                    user_id=user.id,
                    sender_user_id=user.id,
                    role="user",
                    content="exact session event",
                    conversation_id=str(session.id),
                    created_at=now,
                ),
            ]
        )
        await db.commit()

    assert await recover_legacy_on_message_events(trigger) == 0
    async with async_session() as db:
        execution_count = (
            await db.execute(select(func.count(TriggerExecution.id)).where(TriggerExecution.trigger_id == trigger.id))
        ).scalar_one()
    assert execution_count == 0


async def test_legacy_recovery_serializes_max_fire_capacity():
    """Concurrent scanners cannot enqueue two events into one remaining slot."""
    from app.services.trigger_runtime.evaluator import recover_legacy_on_message_events

    agent, user = await _make_agent()
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        session = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="web",
            external_conv_id=f"legacy-capacity-{uuid.uuid4()}",
        )
        db.add(session)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=agent.id,
            name=f"legacy-capacity-{uuid.uuid4().hex[:8]}",
            type="on_message",
            reason="one remaining slot",
            config={
                "from_user_id": str(user.id),
                "_since_ts": (now - timedelta(minutes=10)).isoformat(),
            },
            is_enabled=True,
            fire_count=0,
            max_fires=1,
        )
        db.add(trigger)
        db.add_all(
            [
                ChatMessage(
                    agent_id=agent.id,
                    user_id=user.id,
                    sender_user_id=user.id,
                    role="user",
                    content=f"capacity event {index}",
                    conversation_id=str(session.id),
                    created_at=now + timedelta(microseconds=index),
                )
                for index in range(2)
            ]
        )
        await db.commit()

    await asyncio.gather(
        recover_legacy_on_message_events(trigger),
        recover_legacy_on_message_events(trigger),
    )
    async with async_session() as db:
        execution_count = (
            await db.execute(
                select(func.count()).select_from(TriggerExecution).where(TriggerExecution.trigger_id == trigger.id)
            )
        ).scalar_one()
    assert execution_count == 1


async def test_origin_completion_barrier_rejects_unrelated_assistant_turn():
    """Only the assistant row for the exact arm-time anchor releases the wake."""
    from app.services.trigger_daemon import (
        RetryableOnMessageError,
        _resume_origin_session_for_on_message,
    )

    agent, user = await _make_agent()
    async with async_session() as db:
        origin = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="web",
            external_conv_id=f"barrier-{uuid.uuid4()}",
        )
        remote = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="slack",
            external_conv_id=f"slack_{uuid.uuid4().hex}",
        )
        db.add_all([origin, remote])
        await db.flush()
        anchor = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_user_id=user.id,
            role="user",
            content="turn that armed the trigger",
            conversation_id=str(origin.id),
        )
        unrelated = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="assistant",
            content="another turn completed first",
            conversation_id=str(origin.id),
        )
        matched = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_user_id=user.id,
            role="user",
            content="fast remote reply",
            conversation_id=str(remote.id),
        )
        db.add_all([anchor, unrelated, matched])
        await db.commit()

    trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name="strict-origin-barrier",
        type="on_message",
        reason="wait for exact origin turn",
        config={
            "_execution_id": str(uuid.uuid4()),
            "_origin_session_id": str(origin.id),
            "_origin_source_channel": "web",
            "_origin_external_conv_id": origin.external_conv_id,
            "_origin_turn_anchor_id": str(anchor.id),
            "_origin_completion_barrier": True,
            "_watch_session_id": str(remote.id),
            "_matched_session_id": str(remote.id),
            "_matched_message_id": str(matched.id),
        },
    )

    with pytest.raises(RetryableOnMessageError):
        await _resume_origin_session_for_on_message(agent.id, trigger)

    async with async_session() as db:
        event_count = (
            await db.execute(
                select(func.count())
                .select_from(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(origin.id),
                    ChatMessage.message_meta["kind"].as_string() == "on_message_event",
                )
            )
        ).scalar_one()
    assert event_count == 0


async def test_openclaw_recorded_replay_queues_gateway_once(monkeypatch):
    """Concurrent replay resumes one recorded operation without double delivery."""
    import app.services.agent_tools as agent_tools
    from app.models.gateway_message import GatewayMessage
    from app.models.org import AgentAgentRelationship

    source, user = await _make_agent()
    tool_call_id = f"delegate-{uuid.uuid4()}"
    async with async_session() as db:
        target = Agent(
            name=f"OpenClaw-{uuid.uuid4().hex[:8]}",
            creator_id=user.id,
            tenant_id=source.tenant_id,
            status="idle",
            agent_type="openclaw",
        )
        db.add(target)
        await db.flush()
        source_participant = Participant(
            type="agent",
            ref_id=source.id,
            display_name=source.name,
        )
        target_participant = Participant(
            type="agent",
            ref_id=target.id,
            display_name=target.name,
        )
        db.add_all([source_participant, target_participant])
        await db.flush()
        db.add(
            AgentAgentRelationship(
                agent_id=source.id,
                target_agent_id=target.id,
                relation="collaborator",
            )
        )
        origin = _session(
            agent_id=source.id,
            user_id=user.id,
            source_channel="web",
            external_conv_id=f"origin-{uuid.uuid4()}",
        )
        pair_session = ChatSession(
            agent_id=min(source.id, target.id, key=str),
            peer_agent_id=max(source.id, target.id, key=str),
            user_id=user.id,
            title=f"{source.name} ↔ {target.name}",
            source_channel="agent",
        )
        db.add_all([origin, pair_session])
        await db.flush()
        anchor = ChatMessage(
            agent_id=source.id,
            user_id=user.id,
            sender_user_id=user.id,
            role="user",
            content="delegate this task",
            conversation_id=str(origin.id),
        )
        db.add(anchor)
        await db.flush()
        operation_key = agent_tools._build_outbound_operation_key(
            agent_id=source.id,
            origin_session_id=str(origin.id),
            tool_call_id=tool_call_id,
            origin_turn_anchor_id=anchor.id,
        )
        receipt = ChatMessage(
            id=uuid.uuid4(),
            agent_id=pair_session.agent_id,
            user_id=user.id,
            sender_agent_id=source.id,
            role="user",
            content="prepare the strict replay report",
            conversation_id=str(pair_session.id),
            participant_id=source_participant.id,
            external_event_key=operation_key,
            message_meta={
                "direction": "outbound",
                "source_channel": "agent",
                "origin_session_id": str(origin.id),
                "origin_source_channel": "web",
                "origin_turn_anchor_id": str(anchor.id),
                "tool_call_id": tool_call_id,
                "actor_ref": str(target_participant.id),
                "target_agent_id": str(target.id),
                "target_name": target.name,
                "delivery_status": "recorded",
            },
        )
        db.add(receipt)
        await db.commit()

    async def _noop_log(*_args, **_kwargs):
        return None

    arm_callback = AsyncMock()
    monkeypatch.setattr(agent_tools, "_arm_a2a_delegate_callback", arm_callback)
    monkeypatch.setattr("app.services.activity_logger.log_activity", _noop_log)
    call_kwargs = {
        "user_id": user.id,
        "origin_session_id": str(origin.id),
        "tool_call_id": tool_call_id,
        "origin_turn_anchor_id": anchor.id,
    }
    results = await asyncio.gather(
        agent_tools._send_message_to_agent(
            source.id,
            {
                "agent_id": str(target.id),
                "message": "prepare the strict replay report",
                "msg_type": "task_delegate",
            },
            **call_kwargs,
        ),
        agent_tools._send_message_to_agent(
            source.id,
            {
                "agent_id": str(target.id),
                "message": "prepare the strict replay report",
                "msg_type": "task_delegate",
            },
            **call_kwargs,
        ),
    )

    assert all(result.startswith("✅") for result in results)
    assert arm_callback.await_count == 1
    async with async_session() as db:
        stored_receipt = (
            await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == operation_key))
        ).scalar_one()
        gateway_count = (
            await db.execute(
                select(func.count())
                .select_from(GatewayMessage)
                .where(
                    GatewayMessage.agent_id == target.id,
                    GatewayMessage.sender_agent_id == source.id,
                    GatewayMessage.conversation_id == str(pair_session.id),
                )
            )
        ).scalar_one()
    assert stored_receipt.message_meta["delivery_status"] == "queued"
    assert gateway_count == 1
