"""Shared capacity fixture and native Agent pair seeds."""

import uuid

import pytest

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.org import AgentAgentRelationship
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.workload_capacity import WorkloadCapacity, WorkloadKind

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
