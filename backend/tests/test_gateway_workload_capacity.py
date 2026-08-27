"""Capacity coverage for native Agent-to-Agent Gateway turns."""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api import gateway as gateway_api
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.gateway_message import GatewaySendReceipt
from app.models.org import AgentAgentRelationship
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.schemas.schemas import GatewaySendMessageRequest
from app.services.workload_capacity import (
    WorkloadCapacity,
    WorkloadKind,
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


async def _seed_native_pair() -> tuple[str, uuid.UUID, uuid.UUID, uuid.UUID]:
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
        api_key = f"gateway-capacity-key-{suffix}"
        source = Agent(
            tenant_id=tenant.id,
            creator_id=owner.id,
            name=f"Gateway Source {suffix}",
            agent_type="openclaw",
            api_key_hash=api_key,
            status="idle",
            access_mode="company",
        )
        target = Agent(
            tenant_id=tenant.id,
            creator_id=owner.id,
            name=f"Gateway Target {suffix}",
            agent_type="native",
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
