"""Capacity coverage for native Agent-to-Agent Gateway turns."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api import gateway as gateway_api
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.gateway_message import GatewayMessage, GatewaySendReceipt
from app.models.llm import LLMModel
from app.models.org import AgentAgentRelationship
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.schemas.schemas import GatewaySendMessageRequest
from app.services.workload_capacity import (
    WorkloadCapacity,
    WorkloadKind,
)
from app.services.redis_lease_lock import (
    RedisLeaseBusyError,
    RedisLeaseLostError,
    RedisLeaseUnavailableError,
)


@pytest.fixture(autouse=True)
async def _dispose_engine_between_cases():
    await engine.dispose()
    yield
    await engine.dispose()


def _capacity(*, timeout_seconds: float = 0.02) -> WorkloadCapacity:
    return WorkloadCapacity(
        global_limit=1,
        tenant_limit=1,
        category_limits={kind: 1 for kind in WorkloadKind},
        default_timeout_seconds=timeout_seconds,
        instance_id="gateway-capacity-test",
    )


async def _seed_native_pair(
    *,
    source_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
    separate_target_owner: bool = False,
) -> tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]:
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        tenant = Tenant(name=f"Gateway capacity {suffix}", slug=f"gateway-capacity-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"gateway_capacity_{suffix}",
            email=f"gateway_capacity_{suffix}@test.local",
            password_hash="test",
        )
        db.add(identity)
        await db.flush()
        owner = User(
            tenant_id=tenant.id,
            identity_id=identity.id,
            display_name="Gateway Capacity Owner",
            role="member",
            is_active=True,
        )
        db.add(owner)
        await db.flush()
        target_owner = owner
        if separate_target_owner:
            target_identity = Identity(
                username=f"gateway_target_{suffix}",
                email=f"gateway_target_{suffix}@test.local",
                password_hash="test",
            )
            db.add(target_identity)
            await db.flush()
            target_owner = User(
                tenant_id=tenant.id,
                identity_id=target_identity.id,
                display_name="Gateway Target Owner",
                role="member",
                is_active=True,
            )
            db.add(target_owner)
            await db.flush()
        api_key = f"gateway-capacity-key-{suffix}"
        model = LLMModel(
            tenant_id=tenant.id,
            provider="openai",
            model="gateway-test-model",
            api_key_encrypted="unused",
            label="Gateway test model",
            enabled=True,
            context_window=128000,
        )
        db.add(model)
        await db.flush()
        source = Agent(
            id=source_id or uuid.uuid4(),
            tenant_id=tenant.id,
            creator_id=owner.id,
            name=f"Gateway Source {suffix}",
            agent_type="openclaw",
            api_key_hash=api_key,
            status="idle",
            access_mode="company",
        )
        target = Agent(
            id=target_id or uuid.uuid4(),
            tenant_id=tenant.id,
            creator_id=target_owner.id,
            name=f"Gateway Target {suffix}",
            agent_type="native",
            primary_model_id=model.id,
            status="idle",
            access_mode="company",
        )
        db.add_all([source, target])
        await db.flush()
        db.add(
            AgentAgentRelationship(
                agent_id=source.id,
                target_agent_id=target.id,
                relation="collaborator",
                created_by_user_id=owner.id,
            )
        )
        await db.commit()
        return api_key, target.id, tenant.id, source.id


async def _native_background_args(
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    *,
    content: str,
    source_event_id: str,
) -> tuple[str, ...]:
    async with async_session() as db:
        source = await db.get(Agent, source_id)
        target = await db.get(Agent, target_id)
        assert source is not None and target is not None
        return (
            str(source.id),
            source.name,
            str(target.id),
            target.name,
            str(target.primary_model_id),
            target.role_description or "",
            str(target.creator_id),
            content,
            source_event_id,
        )


@pytest.mark.asyncio
async def test_concurrent_native_gateway_messages_merge_into_one_durable_turn(
    monkeypatch,
) -> None:
    _api_key, target_id, _tenant_id, source_id = await _seed_native_pair()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    provider_calls = 0
    injected: list[dict] = []

    async def fake_call_llm(**kwargs):
        nonlocal provider_calls
        provider_calls += 1
        first_started.set()
        await release_first.wait()
        injected.extend(await kwargs["before_round"](0))
        return "one merged gateway reply"

    monkeypatch.setattr("app.services.llm.call_llm", fake_call_llm)
    first_args = await _native_background_args(
        source_id,
        target_id,
        content="first gateway message",
        source_event_id=f"gateway-first-{uuid.uuid4()}",
    )
    second_args = await _native_background_args(
        source_id,
        target_id,
        content="second gateway message",
        source_event_id=f"gateway-second-{uuid.uuid4()}",
    )

    first = asyncio.create_task(gateway_api._send_to_agent_background(*first_args))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second = asyncio.create_task(gateway_api._send_to_agent_background(*second_args))
    await asyncio.wait_for(second, timeout=1)
    release_first.set()
    await asyncio.wait_for(first, timeout=1)

    assert provider_calls == 1
    assert len(injected) == 1
    assert "second gateway message" in injected[0]["content"]
    async with async_session() as db:
        session = await db.scalar(
            select(ChatSession).where(
                ChatSession.source_channel == "agent",
                ChatSession.agent_id.in_([source_id, target_id]),
                ChatSession.peer_agent_id.in_([source_id, target_id]),
            )
        )
        assert session is not None
        rows = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(session.id))
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )
        gateway_replies = list(
            (
                await db.execute(
                    select(GatewayMessage).where(
                        GatewayMessage.agent_id == source_id,
                        GatewayMessage.sender_agent_id == target_id,
                    )
                )
            ).scalars()
        )
    assert len([row for row in rows if row.role == "user"]) == 2
    assert rows[1].message_meta["turn_inbox_state"] == "delivered"
    assert [row.content for row in rows if row.role == "assistant"] == [
        "one merged gateway reply"
    ]
    assert [row.content for row in gateway_replies] == ["one merged gateway reply"]


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

    monkeypatch.setattr("app.services.llm.call_llm", fake_call_llm)
    first_args = await _native_background_args(
        source_id,
        target_id,
        content="first identity message",
        source_event_id=f"gateway-identity-{uuid.uuid4()}",
    )
    first = asyncio.create_task(gateway_api._send_to_agent_background(*first_args))
    await asyncio.wait_for(first_started.wait(), timeout=1)

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
async def test_concurrent_standard_native_a2a_different_user_queues_next_turn(
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

    async def fake_failover(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        started.set()
        await release.wait()
        return "first isolated identity reply"

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
    assert "queued for the next durable conversation turn" in second

    async with async_session() as db:
        queued = await db.scalar(
            select(ChatMessage).where(
                ChatMessage.content == queued_text,
                ChatMessage.role == "user",
            )
        )
        assert queued is not None
        assert queued.message_meta["turn_inbox_mode"] == "next_turn"
        queued.message_meta = {
            **dict(queued.message_meta or {}),
            "turn_inbox_state": "cancelled",
        }
        await db.commit()

    release.set()
    await asyncio.wait_for(first, timeout=1)
    assert provider_calls == 1


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
        scheduled.append,
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
async def test_recovered_standard_a2a_kicks_and_completes_promoted_next_turn(
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
        first_scheduled.append,
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
    assert "queued for the next durable conversation turn" in second_result

    recovery_calls = 0
    second_started = asyncio.Event()

    async def recovered_llm(*_args, **_kwargs):
        nonlocal recovery_calls
        recovery_calls += 1
        if recovery_calls == 2:
            second_started.set()
        return f"chain recovery reply {recovery_calls}"

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", recovered_llm)
    monkeypatch.setattr(
        turn_inbox,
        "schedule_durable_turn_resume",
        real_schedule,
    )
    assert await turn_recovery.resume_turn(first_scheduled[0]) is True
    await asyncio.wait_for(second_started.wait(), timeout=2)

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
                and len(rows) == 2
            ):
                break
        await asyncio.sleep(0.05)

    assert recovery_calls == 2
    assert session is not None
    assert conversation_turn_snapshot_for_session(session).status == "completed"
    assert [row.content for row in rows] == [
        "chain recovery reply 1",
        "chain recovery reply 2",
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

    monkeypatch.setattr("app.services.llm.call_llm", fail_call_llm)
    scheduled: list[ChatMessage] = []
    lease_released = False

    class FakeLease:
        async def release(self):
            nonlocal lease_released
            lease_released = True

    monkeypatch.setattr(
        gateway_api,
        "_schedule_gateway_turn_recovery",
        scheduled.append,
    )
    args = await _native_background_args(
        source_id,
        target_id,
        content="recover this accepted gateway message",
        source_event_id=f"gateway-recovery-{uuid.uuid4()}",
    )
    await gateway_api._run_gateway_native_turn_with_lease(FakeLease(), *args)
    assert lease_released is True
    assert len(scheduled) == 1

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
        assert scheduled[0].id == anchor.id

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
        await turn_recovery.resume_turn(anchor)
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
    assert await turn_recovery.resume_turn(anchor) is True

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
async def test_native_gateway_turn_uses_project_tenant_capacity_without_db_held_wait(
    monkeypatch,
) -> None:
    api_key, target_id, tenant_id, _source_id = await _seed_native_pair()
    capacity = _capacity()
    started = asyncio.Event()

    async with async_session() as request_db:

        class InspectingCapacity:
            async def acquire(self, kind, resolved_tenant_id):
                assert not request_db.in_transaction()
                return await capacity.acquire(kind, resolved_tenant_id)

        monkeypatch.setattr(
            gateway_api,
            "get_workload_capacity",
            lambda: InspectingCapacity(),
        )

        async def fake_background(*_args):
            snapshot = await capacity.snapshot()
            assert snapshot.categories[WorkloadKind.PROJECT.value].active == 1
            assert snapshot.categories[WorkloadKind.INTERACTIVE.value].active == 0
            assert snapshot.tenants[str(tenant_id)].active == 1
            started.set()

        monkeypatch.setattr(gateway_api, "_send_to_agent_background", fake_background)
        response = await gateway_api.send_message(
            GatewaySendMessageRequest(agent_id=target_id, content="capacity admitted"),
            x_api_key=api_key,
            x_idempotency_key=None,
            db=request_db,
        )

    assert response["status"] == "accepted"
    await asyncio.wait_for(started.wait(), timeout=1)
    for _ in range(20):
        snapshot = await capacity.snapshot()
        if snapshot.global_capacity.active == 0:
            break
        await asyncio.sleep(0)
    assert snapshot.global_capacity.active == 0
    assert snapshot.categories[WorkloadKind.PROJECT.value].completed_total == 1


@pytest.mark.asyncio
async def test_native_gateway_overload_returns_durable_retryable_503_before_acceptance(
    monkeypatch,
) -> None:
    api_key, target_id, tenant_id, source_id = await _seed_native_pair()
    capacity = _capacity(timeout_seconds=0.01)
    blocker = await capacity.acquire(WorkloadKind.PROJECT, tenant_id)
    monkeypatch.setattr(gateway_api, "get_workload_capacity", lambda: capacity)
    calls = 0

    async def fake_background(*_args):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(gateway_api, "_send_to_agent_background", fake_background)
    body = GatewaySendMessageRequest(agent_id=target_id, content="capacity rejected")
    idempotency_key = f"gateway-overload-{uuid.uuid4()}"

    async with async_session() as db:
        with pytest.raises(HTTPException) as exc_info:
            await gateway_api.send_message(
                body,
                x_api_key=api_key,
                x_idempotency_key=idempotency_key,
                db=db,
            )
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["retryable"] is True
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
        assert receipt.response_payload["__http_status"] == 503
        assert receipt.response_payload["detail"]["retryable"] is True

        with pytest.raises(HTTPException) as replay_exc:
            await gateway_api.send_message(
                body,
                x_api_key=api_key,
                x_idempotency_key=idempotency_key,
                db=db,
            )
    assert replay_exc.value.status_code == 503
    assert replay_exc.value.detail["code"] == "gateway_capacity_busy"
    assert calls == 0

    snapshot = await capacity.snapshot()
    assert snapshot.categories[WorkloadKind.PROJECT.value].rejected_total == 1
    await blocker.release()


@pytest.mark.asyncio
async def test_concurrent_native_gateway_turns_respect_project_capacity(
    monkeypatch,
) -> None:
    api_key, target_id, tenant_id, _source_id = await _seed_native_pair()
    capacity = _capacity(timeout_seconds=0.01)
    monkeypatch.setattr(gateway_api, "get_workload_capacity", lambda: capacity)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    async def fake_background(*_args):
        nonlocal calls
        calls += 1
        first_started.set()
        await release_first.wait()

    monkeypatch.setattr(gateway_api, "_send_to_agent_background", fake_background)

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
        with pytest.raises(HTTPException) as second_exc:
            await gateway_api.send_message(
                GatewaySendMessageRequest(agent_id=target_id, content="second turn"),
                x_api_key=api_key,
                x_idempotency_key=None,
                db=second_db,
            )
    assert second_exc.value.status_code == 503
    assert second_exc.value.detail["retryable"] is True
    assert calls == 1

    snapshot = await capacity.snapshot()
    assert snapshot.categories[WorkloadKind.PROJECT.value].active == 1
    assert snapshot.categories[WorkloadKind.PROJECT.value].high_watermark == 1
    assert snapshot.categories[WorkloadKind.PROJECT.value].rejected_total == 1
    assert snapshot.tenants[str(tenant_id)].active == 1

    release_first.set()
    for _ in range(20):
        snapshot = await capacity.snapshot()
        if snapshot.global_capacity.active == 0:
            break
        await asyncio.sleep(0)
    assert snapshot.global_capacity.active == 0
    assert snapshot.categories[WorkloadKind.PROJECT.value].completed_total == 1
