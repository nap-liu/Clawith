"""Durable Gateway send idempotency across every recipient branch."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api import gateway as gateway_api
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.gateway_message import GatewayMessage, GatewaySendReceipt
from app.models.org import AgentAgentRelationship, AgentRelationship
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.schemas.schemas import GatewaySendMessageRequest


@pytest.fixture(autouse=True)
async def _dispose_engine_between_cases():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_source_and_target(target_type: str):
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        tenant = Tenant(name=f"Gateway {suffix}", slug=f"gateway-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"gateway_{suffix}",
            email=f"gateway_{suffix}@test.local",
            password_hash="test",
        )
        db.add(identity)
        await db.flush()
        owner = User(
            tenant_id=tenant.id,
            identity_id=identity.id,
            display_name="Gateway Owner",
            role="member",
            is_active=True,
        )
        db.add(owner)
        await db.flush()
        api_key = f"gateway-key-{suffix}"
        source = Agent(
            tenant_id=tenant.id,
            creator_id=owner.id,
            name=f"Source {suffix}",
            agent_type="openclaw",
            api_key_hash=api_key,
            status="idle",
            access_mode="company",
        )
        db.add(source)
        await db.flush()

        if target_type in {"openclaw", "native"}:
            target = Agent(
                tenant_id=tenant.id,
                creator_id=owner.id,
                name=f"Target {suffix}",
                agent_type=target_type,
                status="idle",
                access_mode="company",
            )
            db.add(target)
            await db.flush()
            db.add(
                AgentAgentRelationship(
                    agent_id=source.id,
                    target_agent_id=target.id,
                    relation="collaborator",
                    created_by_user_id=owner.id,
                )
            )
        else:
            target_identity = Identity(
                username=f"target_{suffix}",
                email=f"target_{suffix}@test.local",
                password_hash="test",
            )
            db.add(target_identity)
            await db.flush()
            target = User(
                tenant_id=tenant.id,
                identity_id=target_identity.id,
                display_name=f"Human {suffix}",
                role="member",
                is_active=True,
            )
            db.add(target)
            await db.flush()
            db.add(
                AgentRelationship(
                    agent_id=source.id,
                    user_id=target.id,
                    relation="collaborator",
                    created_by_user_id=owner.id,
                )
            )
        await db.commit()
        return api_key, source.id, target.id


async def _assert_conflicting_payload_rejected(api_key, target_id, target_field, key):
    conflicting = GatewaySendMessageRequest(
        **{target_field: target_id}, content="different payload", channel="platform" if target_field == "user_id" else None
    )
    async with async_session() as db:
        with pytest.raises(HTTPException) as exc:
            await gateway_api.send_message(
                conflicting,
                x_api_key=api_key,
                x_idempotency_key=key,
                db=db,
            )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_openclaw_queue_send_is_idempotent_and_conflict_safe():
    api_key, source_id, target_id = await _seed_source_and_target("openclaw")
    body = GatewaySendMessageRequest(agent_id=target_id, content="one queued send")
    key = f"queue-{uuid.uuid4()}"
    results = []
    for _ in range(2):
        async with async_session() as db:
            results.append(
                await gateway_api.send_message(
                    body, x_api_key=api_key, x_idempotency_key=key, db=db
                )
            )
    assert results[0] == results[1]
    async with async_session() as db:
        queued = await db.scalar(
            select(func.count(GatewayMessage.id)).where(
                GatewayMessage.agent_id == target_id,
                GatewayMessage.sender_agent_id == source_id,
                GatewayMessage.content == "one queued send",
            )
        )
    assert queued == 1
    await _assert_conflicting_payload_rejected(api_key, target_id, "agent_id", key)


@pytest.mark.asyncio
async def test_native_agent_send_is_scheduled_once_and_conflict_safe(monkeypatch):
    api_key, _source_id, target_id = await _seed_source_and_target("native")
    calls = 0

    async def fake_background(*_args):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(gateway_api, "_send_to_agent_background", fake_background)
    body = GatewaySendMessageRequest(agent_id=target_id, content="one native send")
    key = f"native-{uuid.uuid4()}"
    results = []
    for _ in range(2):
        async with async_session() as db:
            results.append(
                await gateway_api.send_message(
                    body, x_api_key=api_key, x_idempotency_key=key, db=db
                )
            )
        await asyncio.sleep(0)
    assert results[0] == results[1]
    assert calls == 1
    await _assert_conflicting_payload_rejected(api_key, target_id, "agent_id", key)


@pytest.mark.asyncio
async def test_human_send_is_executed_once_and_conflict_safe(monkeypatch):
    from app.services import agent_tools

    api_key, source_id, target_id = await _seed_source_and_target("human")
    calls = 0

    async def fake_platform(agent_id, args):
        nonlocal calls
        calls += 1
        assert agent_id == source_id
        assert args["user_id"] == str(target_id)
        return "sent once"

    monkeypatch.setattr(agent_tools, "_send_platform_message", fake_platform)
    body = GatewaySendMessageRequest(
        user_id=target_id,
        channel="platform",
        content="one human send",
    )
    key = f"human-{uuid.uuid4()}"
    results = []
    for _ in range(2):
        async with async_session() as db:
            results.append(
                await gateway_api.send_message(
                    body, x_api_key=api_key, x_idempotency_key=key, db=db
                )
            )
    assert results[0] == results[1]
    assert calls == 1
    async with async_session() as db:
        receipts = await db.scalar(
            select(func.count(GatewaySendReceipt.id)).where(
                GatewaySendReceipt.source_agent_id == source_id,
                GatewaySendReceipt.idempotency_key == key,
            )
        )
    assert receipts == 1
    await _assert_conflicting_payload_rejected(api_key, target_id, "user_id", key)


@pytest.mark.asyncio
async def test_pending_receipt_never_reexecutes_an_unknown_outcome(monkeypatch):
    from app.services import agent_tools

    api_key, source_id, target_id = await _seed_source_and_target("human")
    body = GatewaySendMessageRequest(
        user_id=target_id,
        channel="platform",
        content="unknown prior outcome",
    )
    key = f"pending-{uuid.uuid4()}"
    async with async_session() as db:
        db.add(
            GatewaySendReceipt(
                source_agent_id=source_id,
                idempotency_key=key,
                request_hash=gateway_api._gateway_send_request_hash(body),
                status="pending",
            )
        )
        await db.commit()

    calls = 0

    async def should_not_send(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return "unexpected"

    monkeypatch.setattr(agent_tools, "_send_platform_message", should_not_send)
    async with async_session() as db:
        with pytest.raises(HTTPException) as exc:
            await gateway_api.send_message(
                body,
                x_api_key=api_key,
                x_idempotency_key=key,
                db=db,
            )
    assert exc.value.status_code == 409
    assert calls == 0


@pytest.mark.asyncio
async def test_concurrent_same_key_claim_queues_exactly_once_across_sessions():
    api_key, source_id, target_id = await _seed_source_and_target("openclaw")
    body = GatewaySendMessageRequest(agent_id=target_id, content="concurrent queued send")
    key = f"concurrent-{uuid.uuid4()}"

    async def send_once():
        async with async_session() as db:
            try:
                return (
                    "ok",
                    await gateway_api.send_message(
                        body,
                        x_api_key=api_key,
                        x_idempotency_key=key,
                        db=db,
                    ),
                )
            except HTTPException as exc:
                assert exc.status_code == 409
                return ("pending", exc.detail)

    results = await asyncio.gather(send_once(), send_once())
    assert sum(kind == "ok" for kind, _payload in results) >= 1
    async with async_session() as db:
        queued = await db.scalar(
            select(func.count(GatewayMessage.id)).where(
                GatewayMessage.agent_id == target_id,
                GatewayMessage.sender_agent_id == source_id,
                GatewayMessage.content == "concurrent queued send",
            )
        )
        receipts = await db.scalar(
            select(func.count(GatewaySendReceipt.id)).where(
                GatewaySendReceipt.source_agent_id == source_id,
                GatewaySendReceipt.idempotency_key == key,
            )
        )
    assert queued == 1
    assert receipts == 1
