"""End-to-end database tests for exact on_message session correlation."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction  # noqa: F401 - register ChatMessage FK target
from app.models.chat_session import ChatSession
from app.models.participant import Participant  # noqa: F401 - register FK table metadata
from app.models.org import AgentRelationship
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.user import Identity, User


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_agent() -> tuple[Agent, User]:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"onmessage-{suffix}", slug=f"onmessage-{suffix}")
        identity = Identity(
            username=f"onmessage_{suffix}",
            email=f"onmessage_{suffix}@test.local",
            password_hash="x",
        )
        db.add_all([tenant, identity])
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name=f"OnMessage {suffix}",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"OnMessageAgent-{suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add(agent)
        await db.flush()
        db.add(
            AgentRelationship(
                agent_id=agent.id,
                user_id=user.id,
                relation="collaborator",
            )
        )
        await db.commit()
        return agent, user


def _session(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    source_channel: str,
    external_conv_id: str | None,
) -> ChatSession:
    return ChatSession(
        agent_id=agent_id,
        user_id=user_id,
        title=f"{source_channel} session",
        source_channel=source_channel,
        external_conv_id=external_conv_id,
    )


async def test_one_inbound_event_fans_out_once_per_matching_subscription():
    """The idempotency boundary is (trigger, provider event), not remote session."""
    from app.services.chat_history import ingest_incoming_chat_message

    agent, user = await _make_agent()
    async with async_session() as db:
        origin_web = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="web",
            external_conv_id=f"web-{uuid.uuid4()}",
        )
        origin_im = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="feishu",
            external_conv_id=f"feishu_p2p_{uuid.uuid4().hex}",
        )
        remote = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="dingtalk",
            external_conv_id=f"dingtalk_p2p_{uuid.uuid4().hex}",
        )
        db.add_all([origin_web, origin_im, remote])
        await db.flush()

        triggers = []
        for index, origin in enumerate((origin_web, origin_im), start=1):
            context = {
                "name": f"wait-{index}",
                "type": "on_message",
                "reason": f"original reason {index}",
                "focus_ref": f"focus-{index}",
                "config": {"from_user_id": str(user.id)},
            }
            triggers.append(
                AgentTrigger(
                    agent_id=agent.id,
                    name=f"wait-{index}",
                    type="on_message",
                    reason=context["reason"],
                    focus_ref=context["focus_ref"],
                    is_enabled=True,
                    fire_count=0,
                    max_fires=100,
                    config={
                        "from_user_id": str(user.id),
                        "_origin_session_id": str(origin.id),
                        "_origin_source_channel": origin.source_channel,
                        "_origin_external_conv_id": origin.external_conv_id,
                        "_watch_session_id": str(remote.id),
                        "_watch_source_channel": "dingtalk",
                        "_watch_actor_ref": "remote-user-42",
                        "_correlation_mode": "session_event",
                        "_consume_remote": True,
                        "_set_trigger_context": context,
                    },
                )
            )
        db.add_all(triggers)
        await db.commit()

        first = await ingest_incoming_chat_message(
            db,
            session=remote,
            agent_id=agent.id,
            user_id=user.id,
            content="the remote answer",
            source_channel="dingtalk",
            provider_event_id="provider-event-42",
            channel_config_id="bot-account-1",
            actor_ref="remote-user-42",
        )
        await db.commit()

        assert first.created is True
        assert first.consumed_by_onmessage is True
        assert len(first.execution_ids) == 2

        executions = list(
            (
                await db.execute(
                    select(TriggerExecution)
                    .where(TriggerExecution.id.in_(first.execution_ids))
                    .order_by(TriggerExecution.trigger_id)
                )
            ).scalars()
        )
        assert len(executions) == 2
        assert {row.payload["_origin_session_id"] for row in executions} == {
            str(origin_web.id),
            str(origin_im.id),
        }
        assert {row.payload["_trigger_context"]["reason"] for row in executions} == {
            "original reason 1",
            "original reason 2",
        }
        await db.refresh(first.message)
        assert set(first.message.message_meta["onmessage_execution_ids"]) == {
            str(execution_id) for execution_id in first.execution_ids
        }

        duplicate = await ingest_incoming_chat_message(
            db,
            session=remote,
            agent_id=agent.id,
            user_id=user.id,
            content="the remote answer",
            source_channel="dingtalk",
            provider_event_id="provider-event-42",
            channel_config_id="bot-account-1",
            actor_ref="remote-user-42",
        )
        await db.commit()
        execution_count = (
            await db.execute(
                select(func.count())
                .select_from(TriggerExecution)
                .where(TriggerExecution.trigger_id.in_([trigger.id for trigger in triggers]))
            )
        ).scalar_one()

        assert duplicate.created is False
        assert duplicate.consumed_by_onmessage is True
        assert set(duplicate.execution_ids) == set(first.execution_ids)
        assert execution_count == 2


async def test_origin_wake_creates_a_new_idempotent_turn_with_arm_context(monkeypatch):
    """A wake never reuses the old send turn; it appends one real event turn."""
    import app.services.channel_llm as channel_llm
    import app.services.turn_runtime as turn_runtime
    from app.services.trigger_daemon import _resume_origin_session_for_on_message

    agent, user = await _make_agent()
    async with async_session() as db:
        origin = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="web",
            external_conv_id=f"web-{uuid.uuid4()}",
        )
        remote = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="slack",
            external_conv_id=f"slack_{uuid.uuid4().hex}",
        )
        db.add_all([origin, remote])
        await db.flush()
        old_turn = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_user_id=user.id,
            role="user",
            content="the original user turn",
            conversation_id=str(origin.id),
        )
        db.add(old_turn)
        await db.flush()
        old_final = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="assistant",
            content="waiting for the remote reply",
            conversation_id=str(origin.id),
            message_meta={
                "turn_anchor_id": str(old_turn.id),
                "turn_status": "completed",
            },
        )
        matched = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_user_id=user.id,
            role="user",
            content="remote reply payload",
            conversation_id=str(remote.id),
            message_meta={
                "direction": "inbound",
                "source_channel": "slack",
                "actor_ref": "U-remote",
            },
        )
        db.add_all([old_final, matched])
        await db.commit()

    execution_id = uuid.uuid4()
    context = {
        "name": "wait-for-slack",
        "type": "on_message",
        "reason": "continue the exact original workflow",
        "focus_ref": "workflow-7",
        "config": {"from_user_id": str(user.id)},
    }
    trigger = AgentTrigger(
        id=uuid.uuid4(),
        agent_id=agent.id,
        name=context["name"],
        type="on_message",
        reason=context["reason"],
        focus_ref=context["focus_ref"],
        config={
            "_execution_id": str(execution_id),
            "_origin_session_id": str(origin.id),
            "_origin_user_id": str(user.id),
            "_origin_source_channel": "web",
            "_origin_external_conv_id": origin.external_conv_id,
            "_origin_turn_anchor_id": str(old_turn.id),
            "_watch_session_id": str(remote.id),
            "_watch_source_channel": "slack",
            "_matched_session_id": str(remote.id),
            "_matched_message_id": str(matched.id),
            "_matched_from": "Remote User",
            "_trigger_context": context,
        },
    )

    llm_calls: list[list[dict]] = []
    deliveries: list[tuple[str, str]] = []

    async def _fake_llm(_db, _agent_id, _user_text, **kwargs):
        llm_calls.append(kwargs["history"])
        return "origin session handled the reply"

    async def _fake_delivery(*, agent_id, conversation_id, reply, **_kwargs):
        assert agent_id == agent.id
        deliveries.append((conversation_id, reply))
        return True

    monkeypatch.setattr(channel_llm, "_call_agent_llm", _fake_llm)
    monkeypatch.setattr(turn_runtime, "deliver_recovered_reply_to_origin", _fake_delivery)

    await asyncio.gather(
        _resume_origin_session_for_on_message(agent.id, trigger),
        _resume_origin_session_for_on_message(agent.id, trigger),
    )

    assert len(llm_calls) == 1
    assert len(deliveries) == 1
    assert deliveries[0] == (str(origin.id), "origin session handled the reply")

    async with async_session() as db:
        origin_rows = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(origin.id))
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )
    event_rows = [row for row in origin_rows if row.message_meta.get("kind") == "on_message_event"]
    final_rows = [row for row in origin_rows if row.message_meta.get("kind") == "on_message_final"]
    assert len(event_rows) == 1
    assert len(final_rows) == 1
    assert event_rows[0].id != old_turn.id
    assert event_rows[0].created_at >= old_turn.created_at
    assert "continue the exact original workflow" in event_rows[0].content
    assert "remote reply payload" in event_rows[0].content
    assert event_rows[0].message_meta["trigger_context"] == context
    assert final_rows[0].message_meta["origin_delivery_status"] == "delivered"


async def test_execution_claim_and_fire_count_are_atomic_across_retry():
    """Claiming two events counts both once; requeueing them counts neither again."""
    from app.services.trigger_runtime.executions import (
        claim_pending_trigger_executions,
        requeue_trigger_executions,
    )

    agent, _user = await _make_agent()
    execution_source = f"test_{uuid.uuid4().hex[:8]}"
    trigger = AgentTrigger(
        agent_id=agent.id,
        name=f"claim-atomic-{uuid.uuid4().hex[:8]}",
        type="on_message",
        reason="claim atomically",
        config={},
        is_enabled=True,
        fire_count=0,
        max_fires=10,
    )
    async with async_session() as db:
        db.add(trigger)
        await db.flush()
        executions = [
            TriggerExecution(
                trigger_id=trigger.id,
                agent_id=agent.id,
                source=execution_source,
                status="pending",
                idempotency_key=f"event-{index}",
                payload={},
            )
            for index in range(2)
        ]
        db.add_all(executions)
        await db.commit()
        execution_ids = [execution.id for execution in executions]

    first_claim = await claim_pending_trigger_executions(sources=[execution_source])
    assert {execution.id for execution, _trigger in first_claim} == set(execution_ids)
    assert all(getattr(execution, "_is_first_claim", False) for execution, _trigger in first_claim)
    async with async_session() as db:
        stored = await db.get(AgentTrigger, trigger.id)
        assert stored.fire_count == 2

    await requeue_trigger_executions(execution_ids, "retry once")
    retry_claim = await claim_pending_trigger_executions(sources=[execution_source])
    assert {execution.id for execution, _trigger in retry_claim} == set(execution_ids)
    assert all(not getattr(execution, "_is_first_claim", True) for execution, _trigger in retry_claim)
    async with async_session() as db:
        stored = await db.get(AgentTrigger, trigger.id)
        assert stored.fire_count == 2


async def test_openclaw_report_is_one_agent_channel_event_across_retries():
    """The generic agent channel uses the same (trigger, event) boundary."""
    from app.api.gateway import report_result
    from app.models.gateway_message import GatewayMessage
    from app.models.participant import Participant
    from app.schemas.schemas import GatewayReportRequest

    source_agent, user = await _make_agent()
    api_key = f"openclaw-{uuid.uuid4().hex}"
    async with async_session() as db:
        target_agent = Agent(
            name=f"OpenClaw-{uuid.uuid4().hex[:8]}",
            creator_id=user.id,
            tenant_id=source_agent.tenant_id,
            status="idle",
            agent_type="openclaw",
            api_key_hash=api_key,
        )
        db.add(target_agent)
        await db.flush()
        target_participant = Participant(
            type="agent",
            ref_id=target_agent.id,
            display_name=target_agent.name,
        )
        db.add(target_participant)
        session = ChatSession(
            agent_id=source_agent.id,
            peer_agent_id=target_agent.id,
            user_id=user.id,
            title="native to OpenClaw",
            source_channel="agent",
        )
        db.add(session)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=source_agent.id,
            name=f"wait-openclaw-{uuid.uuid4().hex[:8]}",
            type="on_message",
            reason="continue source workflow",
            config={
                "from_agent_id": str(target_agent.id),
                "_watch_session_id": str(session.id),
                "_watch_source_channel": "agent",
                "_watch_actor_ref": str(target_participant.id),
                "_consume_remote": True,
            },
            is_enabled=True,
            fire_count=0,
            max_fires=10,
        )
        gateway_message = GatewayMessage(
            agent_id=target_agent.id,
            sender_agent_id=source_agent.id,
            conversation_id=str(session.id),
            content="remote task",
            status="delivered",
        )
        db.add_all([trigger, gateway_message])
        await db.commit()
        message_id = gateway_message.id
        trigger_id = trigger.id
        target_agent_id = target_agent.id

    request = GatewayReportRequest(message_id=message_id, result="OpenClaw reply")
    for _attempt in range(2):
        async with async_session() as db:
            assert await report_result(request, x_api_key=api_key, db=db) == {"status": "ok"}

    async with async_session() as db:
        inbound_rows = list(
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.external_event_key == f"gateway-report:{message_id}"
                    )
                )
            ).scalars()
        )
        execution_count = (
            await db.execute(
                select(func.count())
                .select_from(TriggerExecution)
                .where(TriggerExecution.trigger_id == trigger_id)
            )
        ).scalar_one()
        reply_count = (
            await db.execute(
                select(func.count())
                .select_from(GatewayMessage)
                .where(
                    GatewayMessage.agent_id == source_agent.id,
                    GatewayMessage.sender_agent_id == target_agent_id,
                    GatewayMessage.conversation_id == str(session.id),
                )
            )
        ).scalar_one()

    assert len(inbound_rows) == 1
    assert inbound_rows[0].message_meta["consumed_by_onmessage"] is True
    assert execution_count == 1
    assert reply_count == 1


async def test_set_trigger_binds_only_the_current_turn_outbound_receipt(monkeypatch):
    """An older send in the same origin session can never steal a new subscription."""
    import app.services.agent_tools as agent_tools

    agent, user = await _make_agent()
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        origin = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="web",
            external_conv_id=f"web-{uuid.uuid4()}",
        )
        old_remote = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="slack",
            external_conv_id=f"slack_{uuid.uuid4().hex}",
        )
        current_remote = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="dingtalk",
            external_conv_id=f"dingtalk_p2p_{uuid.uuid4().hex}",
        )
        db.add_all([origin, old_remote, current_remote])
        await db.flush()
        old_turn = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_user_id=user.id,
            role="user",
            content="old turn",
            conversation_id=str(origin.id),
            created_at=now - timedelta(minutes=5),
        )
        current_turn = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_user_id=user.id,
            role="user",
            content="current turn",
            conversation_id=str(origin.id),
            created_at=now,
            message_meta={
                "direction": "inbound",
                "source_channel": "web",
                "actor_ref": str(user.id),
            },
        )
        db.add_all([old_turn, current_turn])
        await db.flush()
        db.add_all(
            [
                ChatMessage(
                    agent_id=agent.id,
                    user_id=user.id,
                    role="assistant",
                    content="old outbound",
                    conversation_id=str(old_remote.id),
                    created_at=now - timedelta(minutes=4),
                    message_meta={
                        "direction": "outbound",
                        "source_channel": "slack",
                            "actor_ref": "remote-actor",
                            "target_user_id": str(user.id),
                            "target_name": "Remote User",
                        "origin_session_id": str(origin.id),
                        "origin_turn_anchor_id": str(old_turn.id),
                    },
                ),
                ChatMessage(
                    agent_id=agent.id,
                    user_id=user.id,
                    role="assistant",
                    content="current outbound",
                    conversation_id=str(current_remote.id),
                    created_at=now + timedelta(seconds=1),
                    message_meta={
                        "direction": "outbound",
                        "source_channel": "dingtalk",
                            "actor_ref": "remote-actor",
                            "target_user_id": str(user.id),
                            "target_name": "Remote User",
                        "origin_session_id": str(origin.id),
                        "origin_turn_anchor_id": str(current_turn.id),
                        "external_message_id": "provider-outbound-7",
                    },
                ),
            ]
        )
        await db.commit()

    async def _focus(*_args, **_kwargs):
        return "focus-current-turn"

    monkeypatch.setattr(agent_tools, "ensure_focus_item", _focus)
    result = await agent_tools._handle_set_trigger(
        agent.id,
        {
            "name": f"wait-current-{uuid.uuid4().hex[:6]}",
            "type": "on_message",
            "config": {
                "from_user_id": str(user.id),
                "_watch_session_id": str(old_remote.id),
            },
            "reason": "use the current outbound request context",
        },
        session_id=str(origin.id),
        user_id=user.id,
        turn_anchor_id=current_turn.id,
    )
    assert result.startswith("✅")

    async with async_session() as db:
        stored = (
            await db.execute(
                select(AgentTrigger).where(
                    AgentTrigger.agent_id == agent.id,
                    AgentTrigger.reason == "use the current outbound request context",
                )
            )
        ).scalar_one()
    assert stored.config["_watch_session_id"] == str(current_remote.id)
    assert stored.config["_watch_source_channel"] == "dingtalk"
    assert stored.config["_origin_session_id"] == str(origin.id)
    assert stored.config["_origin_actor_ref"] == str(user.id)
    assert stored.config["_outbound_external_message_id"] == "provider-outbound-7"
    assert stored.config["_set_trigger_context"] == {
        "name": stored.name,
        "type": "on_message",
        "reason": "use the current outbound request context",
        "focus_ref": "focus-current-turn",
        "config": {"from_user_id": str(user.id)},
    }
    assert "_watch_session_id" not in stored.config["_set_trigger_context"]["config"]

    # Re-enabling the same subscription must still run the reply-before-arm
    # recovery scan; otherwise a reply persisted while it was disabled is lost.
    async with async_session() as db:
        stored_row = await db.get(AgentTrigger, stored.id)
        stored_row.is_enabled = False
        db.add(
            ChatMessage(
                agent_id=agent.id,
                user_id=user.id,
                sender_user_id=user.id,
                role="user",
                content="reply while trigger was disabled",
                conversation_id=str(current_remote.id),
                created_at=now + timedelta(seconds=2),
                message_meta={
                    "direction": "inbound",
                    "source_channel": "dingtalk",
                    "actor_ref": "remote-actor",
                },
            )
        )
        db.add(
            ChatMessage(
                agent_id=agent.id,
                user_id=user.id,
                sender_user_id=user.id,
                role="user",
                content="second reply before re-arm",
                conversation_id=str(current_remote.id),
                created_at=now + timedelta(seconds=3),
                message_meta={
                    "direction": "inbound",
                    "source_channel": "dingtalk",
                    "actor_ref": "remote-actor",
                },
            )
        )
        await db.commit()

    reenabled = await agent_tools._handle_set_trigger(
        agent.id,
        {
            "name": stored.name,
            "type": "on_message",
            "config": {"from_user_id": str(user.id)},
            "reason": "use the current outbound request context",
        },
        session_id=str(origin.id),
        user_id=user.id,
        turn_anchor_id=current_turn.id,
    )
    assert "re-enabled" in reenabled
    async with async_session() as db:
        recovered = list(
            (
                await db.execute(
                    select(TriggerExecution).where(
                        TriggerExecution.trigger_id == stored.id
                    )
                )
            ).scalars()
        )
    assert len(recovered) == 2
    assert {row.payload["_matched_session_id"] for row in recovered} == {
        str(current_remote.id)
    }
    assert {row.payload["_matched_message"] for row in recovered} == {
        "reply while trigger was disabled",
        "second reply before re-arm",
    }


async def test_legacy_recovery_keeps_stable_event_cursor_after_processing_time():
    """A claim timestamp must not jump over an older, later-committed event."""
    from app.services.trigger_runtime.evaluator import recover_legacy_on_message_events

    agent, user = await _make_agent()
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        session = _session(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="web",
            external_conv_id=f"legacy-{uuid.uuid4()}",
        )
        db.add(session)
        await db.flush()
        trigger = AgentTrigger(
            agent_id=agent.id,
            name=f"legacy-stable-{uuid.uuid4().hex[:8]}",
            type="on_message",
            reason="preserve every event",
            config={
                "from_user_id": str(user.id),
                "_since_ts": (now - timedelta(minutes=10)).isoformat(),
            },
            is_enabled=True,
            fire_count=0,
            max_fires=10,
        )
        first = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_user_id=user.id,
            role="user",
            content="first legacy event",
            conversation_id=str(session.id),
            created_at=now - timedelta(minutes=5),
        )
        db.add_all([trigger, first])
        await db.commit()

    assert await recover_legacy_on_message_events(trigger) == 1

    async with async_session() as db:
        stored = await db.get(AgentTrigger, trigger.id)
        stored.last_fired_at = now + timedelta(minutes=1)
        db.add(
            ChatMessage(
                agent_id=agent.id,
                user_id=user.id,
                sender_user_id=user.id,
                role="user",
                content="committed after scan with older event time",
                conversation_id=str(session.id),
                created_at=now - timedelta(minutes=1),
            )
        )
        await db.commit()

    assert await recover_legacy_on_message_events(trigger) == 1
    async with async_session() as db:
        payloads = list(
            (
                await db.execute(
                    select(TriggerExecution.payload).where(
                        TriggerExecution.trigger_id == trigger.id
                    )
                )
            ).scalars()
        )
    assert {payload["_matched_message"] for payload in payloads} == {
        "first legacy event",
        "committed after scan with older event time",
    }


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
                await db.execute(
                    select(TriggerExecution.payload).where(
                        TriggerExecution.trigger_id == trigger.id
                    )
                )
            ).scalars()
        )
    assert [payload["_matched_message"] for payload in payloads] == [
        "at-cutover legacy event"
    ]


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
            await db.execute(
                select(func.count(TriggerExecution.id)).where(
                    TriggerExecution.trigger_id == trigger.id
                )
            )
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
                select(func.count())
                .select_from(TriggerExecution)
                .where(TriggerExecution.trigger_id == trigger.id)
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
            await db.execute(
                select(ChatMessage).where(ChatMessage.external_event_key == operation_key)
            )
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
