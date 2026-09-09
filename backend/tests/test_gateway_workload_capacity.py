"""Capacity coverage for native Agent-to-Agent Gateway turns."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api import gateway as gateway_api
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.gateway_message import GatewayMessage, GatewaySendReceipt
from app.schemas.schemas import GatewaySendMessageRequest
from app.services.workload_capacity import (
    WorkloadKind,
)
from app.services.redis_lease_lock import (
    RedisLeaseBusyError,
    RedisLeaseLostError,
    RedisLeaseUnavailableError,
)
from gateway_workload_capacity_support import (
    _capacity,
    _dispose_engine_between_cases,  # noqa: F401 - pytest autouse fixture
    _native_background_args,
    _seed_native_pair,
)





@pytest.mark.asyncio
async def test_gateway_same_creator_different_execution_agent_queues_next_turn(
    monkeypatch,
) -> None:
    from app.services.chat_history import ingest_incoming_chat_message

    _api_key, target_id, _tenant_id, source_id = await _seed_native_pair()
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def fake_call_llm(**_kwargs):
        first_started.set()
        await release_first.wait()
        return "first identity reply"

    monkeypatch.setattr("app.services.llm.call_llm_with_failover", fake_call_llm)
    first_args = await _native_background_args(
        source_id,
        target_id,
        content="first identity message",
        source_event_id=f"gateway-identity-{uuid.uuid4()}",
    )
    first = asyncio.create_task(gateway_api._send_to_agent_background(*first_args))
    await asyncio.wait_for(first_started.wait(), timeout=5)

    async with async_session() as db:
        session = await db.scalar(
            select(ChatSession).where(
                ChatSession.source_channel == "agent",
                ChatSession.agent_id.in_([source_id, target_id]),
                ChatSession.peer_agent_id.in_([source_id, target_id]),
            )
        )
        target = await db.get(Agent, target_id)
        assert session is not None and target is not None
        reverse = await ingest_incoming_chat_message(
            db,
            session=session,
            agent_id=session.agent_id,
            user_id=target.creator_id,
            content="same creator but different execution agent",
            source_channel="agent",
            provider_event_id=f"gateway-reverse-{uuid.uuid4()}",
            channel_config_id="gateway-direct",
            actor_ref=str(target_id),
            message_meta={"execution_agent_id": str(source_id)},
        )
        await db.commit()
        assert reverse.queued_to_running_turn is True
        assert reverse.message.message_meta["turn_inbox_mode"] == "next_turn"
        reverse.message.message_meta = {
            **dict(reverse.message.message_meta or {}),
            "turn_inbox_state": "cancelled",
        }
        await db.commit()

    release_first.set()
    await asyncio.wait_for(first, timeout=1)


@pytest.mark.asyncio
async def test_standard_native_a2a_consult_persists_execution_identity_and_lifecycle(
    monkeypatch,
) -> None:
    from app.services.agent_tools import _send_message_to_agent
    from app.services.conversation_turn_lifecycle import (
        conversation_turn_snapshot_for_session,
    )
    from app.services.turn_recovery import _validated_execution_agent_id

    low_source_id = uuid.UUID(int=uuid.uuid4().int >> 8)
    high_target_id = uuid.UUID(
        int=(0xFF << 120) | (uuid.uuid4().int & ((1 << 120) - 1))
    )
    _api_key, target_id, _tenant_id, source_id = await _seed_native_pair(
        source_id=low_source_id,
        target_id=high_target_id,
        separate_target_owner=True,
    )

    async def fake_failover(**_kwargs):
        return "standard durable A2A reply"

    monkeypatch.setattr(
        "app.services.llm.call_llm_with_failover",
        fake_failover,
    )
    async with async_session() as db:
        source = await db.get(Agent, source_id)
        target = await db.get(Agent, target_id)
        assert source is not None and target is not None
        assert source.creator_id != target.creator_id
        owner_id = source.creator_id

    result = await _send_message_to_agent(
        source_id,
        {
            "agent_id": str(target_id),
            "message": "standard A2A durable identity",
            "msg_type": "consult",
        },
        user_id=owner_id,
    )
    assert "standard durable A2A reply" in result

    async with async_session() as db:
        session = await db.scalar(
            select(ChatSession).where(
                ChatSession.source_channel == "agent",
                ChatSession.agent_id == source_id,
                ChatSession.peer_agent_id == target_id,
            )
        )
        assert session is not None
        snapshot = conversation_turn_snapshot_for_session(session)
        assert snapshot.status == "completed"
        anchor = await db.get(ChatMessage, snapshot.anchor_id)
        assert anchor is not None
        assert anchor.message_meta["execution_agent_id"] == str(target_id)
        assert await _validated_execution_agent_id(db, anchor) == target_id
        reply = await db.scalar(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(session.id),
                ChatMessage.role == "assistant",
            )
        )
        assert reply is not None
        assert reply.sender_agent_id == target_id


@pytest.mark.asyncio
async def test_concurrent_standard_native_a2a_consult_merges_into_running_turn(
    monkeypatch,
) -> None:
    from app.services.agent_tools import _send_message_to_agent

    _api_key, target_id, _tenant_id, source_id = await _seed_native_pair()
    async with async_session() as db:
        source = await db.get(Agent, source_id)
        assert source is not None
        owner_id = source.creator_id

    started = asyncio.Event()
    release = asyncio.Event()
    provider_calls = 0
    injected: list[dict] = []

    async def fake_failover(**kwargs):
        nonlocal provider_calls
        provider_calls += 1
        started.set()
        await release.wait()
        injected.extend(await kwargs["before_round"](0))
        return "standard merged reply"

    monkeypatch.setattr(
        "app.services.llm.call_llm_with_failover",
        fake_failover,
    )
    first = asyncio.create_task(
        _send_message_to_agent(
            source_id,
            {
                "agent_id": str(target_id),
                "message": "first standard consult",
                "msg_type": "consult",
            },
            user_id=owner_id,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    second = await _send_message_to_agent(
        source_id,
        {
            "agent_id": str(target_id),
            "message": "second standard consult",
            "msg_type": "consult",
        },
        user_id=owner_id,
    )
    release.set()
    first_result = await asyncio.wait_for(first, timeout=1)

    assert "merged into the current durable conversation turn" in second
    assert "standard merged reply" in first_result
    assert provider_calls == 1
    assert len(injected) == 1
    assert "second standard consult" in injected[0]["content"]


@pytest.mark.asyncio
async def test_concurrent_standard_native_a2a_different_user_joins_current_turn(
    monkeypatch,
) -> None:
    from app.services.agent_tools import _send_message_to_agent

    _api_key, target_id, _tenant_id, source_id = await _seed_native_pair(
        separate_target_owner=True,
    )
    async with async_session() as db:
        source = await db.get(Agent, source_id)
        target = await db.get(Agent, target_id)
        assert source is not None and target is not None
        first_user_id = source.creator_id
        second_user_id = target.creator_id

    started = asyncio.Event()
    release = asyncio.Event()
    provider_calls = 0
    queued_text = f"different execution user {uuid.uuid4()}"
    injected = []

    async def fake_failover(**kwargs):
        nonlocal provider_calls
        provider_calls += 1
        started.set()
        await release.wait()
        injected.extend(await kwargs["before_round"](0))
        return "shared turn reply"

    monkeypatch.setattr(
        "app.services.llm.call_llm_with_failover",
        fake_failover,
    )
    first = asyncio.create_task(
        _send_message_to_agent(
            source_id,
            {
                "agent_id": str(target_id),
                "message": "first execution user",
                "msg_type": "consult",
            },
            user_id=first_user_id,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    second = await _send_message_to_agent(
        source_id,
        {
            "agent_id": str(target_id),
            "message": queued_text,
            "msg_type": "consult",
        },
        user_id=second_user_id,
    )
    assert "merged into the current durable conversation turn" in second

    async with async_session() as db:
        queued = await db.scalar(
            select(ChatMessage).where(
                ChatMessage.content == queued_text,
                ChatMessage.role == "user",
            )
        )
        assert queued is not None
        assert queued.message_meta["turn_inbox_mode"] == "current_turn"

    release.set()
    await asyncio.wait_for(first, timeout=1)
    assert provider_calls == 1
    assert len(injected) == 1
    assert queued_text in injected[0]["content"]


@pytest.mark.parametrize(
    "lease_error",
    [
        RedisLeaseBusyError("busy"),
        RedisLeaseUnavailableError("unavailable"),
        RedisLeaseLostError("lost"),
    ],
)
@pytest.mark.asyncio
async def test_standard_native_a2a_lease_failure_schedules_and_resumes(
    monkeypatch,
    lease_error: Exception,
) -> None:
    from app.services import turn_recovery
    from app.services.agent_tools import _send_message_to_agent
    from app.services.conversation_turn_lifecycle import (
        conversation_turn_snapshot_for_session,
    )

    _api_key, target_id, _tenant_id, source_id = await _seed_native_pair(
        separate_target_owner=True,
    )
    async with async_session() as db:
        source = await db.get(Agent, source_id)
        assert source is not None
        owner_id = source.creator_id

    async def fail_failover(**_kwargs):
        raise lease_error

    scheduled: list[ChatMessage] = []
    monkeypatch.setattr(
        "app.services.llm.call_llm_with_failover",
        fail_failover,
    )
    monkeypatch.setattr(
        "app.services.turn_inbox.schedule_durable_turn_resume",
        AsyncMock(side_effect=scheduled.append),
    )
    result = await _send_message_to_agent(
        source_id,
        {
            "agent_id": str(target_id),
            "message": "resume standard consult after lease failure",
            "msg_type": "consult",
        },
        user_id=owner_id,
    )
    assert "Message send error" in result
    assert len(scheduled) == 1

    async def recovered_llm(*_args, **_kwargs):
        return "standard lease recovery reply"

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", recovered_llm)
    assert await turn_recovery.resume_turn(scheduled[0]) is True

    async with async_session() as db:
        session = await db.get(
            ChatSession,
            uuid.UUID(str(scheduled[0].conversation_id)),
        )
        assert session is not None
        assert conversation_turn_snapshot_for_session(session).status == "completed"
        reply = await db.scalar(
            select(ChatMessage).where(
                ChatMessage.conversation_id == scheduled[0].conversation_id,
                ChatMessage.role == "assistant",
            )
        )
        assert reply is not None
        assert reply.sender_agent_id == target_id
        assert reply.content == "standard lease recovery reply"


@pytest.mark.asyncio
async def test_recovered_standard_a2a_consumes_followup_in_the_same_turn(
    monkeypatch,
) -> None:
    from app.services import turn_inbox, turn_recovery
    from app.services.agent_tools import _send_message_to_agent
    from app.services.conversation_turn_lifecycle import (
        conversation_turn_snapshot_for_session,
    )

    _api_key, target_id, _tenant_id, source_id = await _seed_native_pair(
        separate_target_owner=True,
    )
    async with async_session() as db:
        source = await db.get(Agent, source_id)
        target = await db.get(Agent, target_id)
        assert source is not None and target is not None
        first_user_id = source.creator_id
        second_user_id = target.creator_id

    async def fail_failover(**_kwargs):
        raise RedisLeaseUnavailableError("initial lease unavailable")

    first_scheduled: list[ChatMessage] = []
    real_schedule = turn_inbox.schedule_durable_turn_resume
    monkeypatch.setattr(
        "app.services.llm.call_llm_with_failover",
        fail_failover,
    )
    monkeypatch.setattr(
        turn_inbox,
        "schedule_durable_turn_resume",
        AsyncMock(side_effect=first_scheduled.append),
    )
    first_result = await _send_message_to_agent(
        source_id,
        {
            "agent_id": str(target_id),
            "message": "recover chain first turn",
            "msg_type": "consult",
        },
        user_id=first_user_id,
    )
    assert "Message send error" in first_result
    assert len(first_scheduled) == 1

    second_result = await _send_message_to_agent(
        source_id,
        {
            "agent_id": str(target_id),
            "message": "recover chain promoted turn",
            "msg_type": "consult",
        },
        user_id=second_user_id,
    )
    assert "merged into the current durable conversation turn" in second_result

    recovery_calls = 0
    injected = []

    async def recovered_llm(*_args, **_kwargs):
        nonlocal recovery_calls
        recovery_calls += 1
        anchor = first_scheduled[0]
        injected.extend(await turn_inbox.drain_turn_inbox(
            session_id=anchor.conversation_id, active_turn_anchor_id=anchor.id,
            execution_agent_id=anchor.agent_id, execution_user_id=first_user_id,
        ))
        return f"chain recovery reply {recovery_calls}"

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", recovered_llm)
    monkeypatch.setattr(
        turn_inbox,
        "schedule_durable_turn_resume",
        real_schedule,
    )
    assert await turn_recovery.resume_turn(first_scheduled[0]) is True

    for _ in range(40):
        async with async_session() as db:
            session = await db.get(
                ChatSession,
                uuid.UUID(str(first_scheduled[0].conversation_id)),
            )
            rows = list(
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id
                            == first_scheduled[0].conversation_id,
                            ChatMessage.role == "assistant",
                        )
                        .order_by(ChatMessage.created_at, ChatMessage.id)
                    )
                ).scalars()
            )
            if (
                session is not None
                and conversation_turn_snapshot_for_session(session).status
                == "completed"
                and len(rows) == 1
            ):
                break
        await asyncio.sleep(0.05)

    assert recovery_calls == 1
    assert len(injected) == 1
    assert "recover chain promoted turn" in injected[0]["content"]
    assert session is not None
    assert conversation_turn_snapshot_for_session(session).status == "completed"
    assert [row.content for row in rows] == [
        "chain recovery reply 1",
    ]


@pytest.mark.parametrize(
    "lease_error",
    [
        RedisLeaseBusyError("busy"),
        RedisLeaseUnavailableError("unavailable"),
        RedisLeaseLostError("lost"),
    ],
)
@pytest.mark.asyncio
async def test_native_gateway_lease_failure_remains_durably_recoverable(
    monkeypatch,
    lease_error: Exception,
) -> None:
    from app.services import turn_recovery
    from app.services.conversation_turn_lifecycle import (
        conversation_turn_snapshot_for_session,
    )

    _api_key, target_id, _tenant_id, source_id = await _seed_native_pair()

    async def fail_call_llm(**_kwargs):
        raise lease_error

    monkeypatch.setattr("app.services.llm.call_llm_with_failover", fail_call_llm)
    args = await _native_background_args(
        source_id,
        target_id,
        content="recover this accepted gateway message",
        source_event_id=f"gateway-recovery-{uuid.uuid4()}",
    )
    await gateway_api._send_to_agent_background(*args)

    async with async_session() as db:
        session = await db.scalar(
            select(ChatSession).where(
                ChatSession.source_channel == "agent",
                ChatSession.agent_id.in_([source_id, target_id]),
                ChatSession.peer_agent_id.in_([source_id, target_id]),
            )
        )
        assert session is not None
        snapshot = conversation_turn_snapshot_for_session(session)
        assert snapshot.status == "running" and snapshot.anchor_id is not None
        anchor = await db.get(ChatMessage, snapshot.anchor_id)
        assert anchor is not None
        from app.services.turn_recovery_scanner import _load_recoverable_anchors

        candidates = await _load_recoverable_anchors(db, include_legacy=False)
        assert anchor.id in {candidate.id for candidate in candidates}

    async def recovered_llm(*_args, **_kwargs):
        return "recovered gateway reply"

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", recovered_llm)
    real_upsert = turn_recovery._upsert_gateway_direct_reply

    async def fail_gateway_outbox(*_args, **_kwargs):
        raise ConnectionError("gateway outbox unavailable")

    monkeypatch.setattr(
        turn_recovery,
        "_upsert_gateway_direct_reply",
        fail_gateway_outbox,
    )
    with pytest.raises(ConnectionError):
        await turn_recovery.resume_startup_anchor(anchor)
    async with async_session() as db:
        failed_session = await db.get(ChatSession, session.id)
        terminal_reply = await db.scalar(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(session.id),
                ChatMessage.role == "assistant",
            )
        )
    assert conversation_turn_snapshot_for_session(failed_session).status == "running"
    assert terminal_reply is None

    monkeypatch.setattr(
        turn_recovery,
        "_upsert_gateway_direct_reply",
        real_upsert,
    )
    assert await turn_recovery.resume_startup_anchor(anchor) is True

    async with async_session() as db:
        session = await db.get(ChatSession, session.id)
        gateway_reply = await db.get(
            GatewayMessage,
            uuid.UUID(anchor.message_meta["gateway_direct_reply"]["message_id"]),
        )
    assert conversation_turn_snapshot_for_session(session).status == "completed"
    assert gateway_reply is not None
    assert gateway_reply.agent_id == source_id
    assert gateway_reply.sender_agent_id == target_id
    assert gateway_reply.content == "recovered gateway reply"


@pytest.mark.asyncio
async def test_native_gateway_turn_uses_shared_tenant_capacity_without_db_held_wait(
    monkeypatch,
) -> None:
    api_key, target_id, tenant_id, _source_id = await _seed_native_pair()
    capacity = _capacity()
    started = asyncio.Event()

    async with async_session() as request_db:

        class InspectingCapacity:
            @asynccontextmanager
            async def slot(self, kind, resolved_tenant_id):
                assert not request_db.in_transaction()
                async with capacity.slot(kind, resolved_tenant_id):
                    yield

        monkeypatch.setattr(
            "app.services.turn_recovery.get_workload_capacity",
            lambda: InspectingCapacity(),
        )

        async def fake_provider(**_kwargs):
            snapshot = await capacity.snapshot()
            assert snapshot.categories[WorkloadKind.BACKGROUND.value].active == 1
            assert snapshot.categories[WorkloadKind.INTERACTIVE.value].active == 0
            assert snapshot.tenants[str(tenant_id)].active == 1
            started.set()
            return "capacity reply"

        monkeypatch.setattr("app.services.llm.call_llm_with_failover", fake_provider)
        response = await gateway_api.send_message(
            GatewaySendMessageRequest(agent_id=target_id, content="capacity admitted"),
            x_api_key=api_key,
            x_idempotency_key=None,
            db=request_db,
        )

    assert response["status"] == "accepted"
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(asyncio.gather(*gateway_api._background_tasks), timeout=2)
    snapshot = await capacity.snapshot()
    assert snapshot.global_capacity.active == 0
    assert snapshot.categories[WorkloadKind.BACKGROUND.value].completed_total == 1


@pytest.mark.asyncio
async def test_native_gateway_accepts_under_capacity_pressure_and_recovers_same_anchor(
    monkeypatch,
) -> None:
    api_key, target_id, tenant_id, source_id = await _seed_native_pair()
    capacity = _capacity(timeout_seconds=0.01)
    blocker = await capacity.acquire(WorkloadKind.PROJECT, tenant_id)
    monkeypatch.setattr("app.services.turn_recovery.get_workload_capacity", lambda: capacity)
    calls = 0

    async def fake_provider(**_kwargs):
        nonlocal calls
        calls += 1
        return "accepted work recovered"

    monkeypatch.setattr("app.services.llm.call_llm_with_failover", fake_provider)
    body = GatewaySendMessageRequest(agent_id=target_id, content="capacity deferred")
    idempotency_key = f"gateway-overload-{uuid.uuid4()}"

    async with async_session() as db:
        accepted = await gateway_api.send_message(
            body, x_api_key=api_key, x_idempotency_key=idempotency_key, db=db,
        )
    assert accepted["status"] == "accepted"
    await asyncio.wait_for(asyncio.gather(*gateway_api._background_tasks), timeout=2)
    assert calls == 0

    async with async_session() as db:
        receipt = await db.scalar(
            select(GatewaySendReceipt).where(
                GatewaySendReceipt.source_agent_id == source_id,
                GatewaySendReceipt.idempotency_key == idempotency_key,
            )
        )
        assert receipt is not None
        assert receipt.status == "completed"
        assert receipt.response_payload == accepted
        replay = await gateway_api.send_message(
            body, x_api_key=api_key, x_idempotency_key=idempotency_key, db=db,
        )
        anchors = list(await db.scalars(select(ChatMessage).where(
            ChatMessage.role == "user",
            ChatMessage.message_meta["gateway_direct_reply"]["agent_id"].as_string() == str(source_id),
        )))
    assert replay == accepted
    assert len(anchors) == 1 and anchors[0].message_meta["turn_status"] == "running"
    assert calls == 0

    snapshot = await capacity.snapshot()
    assert snapshot.categories[WorkloadKind.BACKGROUND.value].rejected_total == 1
    await blocker.release()
    from app.services.turn_recovery_startup import resume_startup_anchor

    assert await resume_startup_anchor(anchors[0]) is True
    assert calls == 1
    async with async_session() as db:
        reply = await db.get(GatewayMessage, uuid.UUID(anchors[0].message_meta["gateway_direct_reply"]["message_id"]))
        assert reply.content == "accepted work recovered"


@pytest.mark.asyncio
async def test_concurrent_native_gateway_inputs_share_one_execution_capacity(
    monkeypatch,
) -> None:
    api_key, target_id, tenant_id, _source_id = await _seed_native_pair()
    capacity = _capacity(timeout_seconds=0.01)
    monkeypatch.setattr("app.services.turn_recovery.get_workload_capacity", lambda: capacity)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    injected = []

    async def fake_provider(**kwargs):
        nonlocal calls
        calls += 1
        first_started.set()
        await release_first.wait()
        injected.extend(await kwargs["before_round"](0))
        return "merged gateway reply"

    monkeypatch.setattr("app.services.llm.call_llm_with_failover", fake_provider)

    async with async_session() as first_db:
        first_response = await gateway_api.send_message(
            GatewaySendMessageRequest(agent_id=target_id, content="first turn"),
            x_api_key=api_key,
            x_idempotency_key=None,
            db=first_db,
        )
    assert first_response["status"] == "accepted"
    await asyncio.wait_for(first_started.wait(), timeout=1)

    async with async_session() as second_db:
        second_response = await gateway_api.send_message(
            GatewaySendMessageRequest(agent_id=target_id, content="second turn"),
            x_api_key=api_key, x_idempotency_key=None, db=second_db,
        )
    assert second_response["status"] == "accepted"
    assert calls == 1

    snapshot = await capacity.snapshot()
    assert snapshot.categories[WorkloadKind.BACKGROUND.value].active == 1
    assert snapshot.categories[WorkloadKind.BACKGROUND.value].high_watermark == 1
    assert snapshot.categories[WorkloadKind.BACKGROUND.value].rejected_total == 0
    assert snapshot.tenants[str(tenant_id)].active == 1

    release_first.set()
    await asyncio.wait_for(asyncio.gather(*gateway_api._background_tasks), timeout=2)
    snapshot = await capacity.snapshot()
    assert snapshot.global_capacity.active == 0
    assert snapshot.categories[WorkloadKind.BACKGROUND.value].completed_total == 1
    assert calls == 1 and len(injected) == 1
    assert "second turn" in injected[0]["content"]
