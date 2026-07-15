"""Supervision targets must use the same canonical identity graph as tools."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.chat_compaction import ChatCompaction  # noqa: F401
from app.models.chat_session import ChatSession
from app.models.org import AgentAgentRelationship, RelationshipSuppression
from app.models.task import Task, TaskLog
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.schemas.schemas import TaskCreate
from app.services.recipient_resolver import RecipientResolutionError
from app.services.supervision_targets import resolve_supervision_target

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_cases():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_agent_graph(*, duplicate_name: bool = False):
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Supervision {suffix}", slug=f"supervision-{suffix}")
        foreign_tenant = Tenant(
            name=f"Foreign {suffix}", slug=f"supervision-foreign-{suffix}"
        )
        db.add_all([tenant, foreign_tenant])
        await db.flush()
        identity = Identity(
            username=f"supervision_{suffix}",
            email=f"supervision_{suffix}@test.local",
            password_hash="test",
        )
        foreign_identity = Identity(
            username=f"supervision_foreign_{suffix}",
            email=f"supervision_foreign_{suffix}@test.local",
            password_hash="test",
        )
        db.add_all([identity, foreign_identity])
        await db.flush()
        owner = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Owner",
            role="member",
            is_active=True,
        )
        foreign_owner = User(
            identity_id=foreign_identity.id,
            tenant_id=foreign_tenant.id,
            display_name="Foreign owner",
            role="member",
            is_active=True,
        )
        db.add_all([owner, foreign_owner])
        await db.flush()
        source = Agent(
            tenant_id=tenant.id,
            creator_id=owner.id,
            name=f"Source {suffix}",
            access_mode="company",
            status="idle",
        )
        target = Agent(
            tenant_id=tenant.id,
            creator_id=owner.id,
            name="Same display name",
            access_mode="company",
            status="idle",
        )
        second = Agent(
            tenant_id=tenant.id,
            creator_id=owner.id,
            name="Same display name" if duplicate_name else "Second target",
            access_mode="company",
            status="idle",
        )
        foreign = Agent(
            tenant_id=foreign_tenant.id,
            creator_id=foreign_owner.id,
            name="Same display name",
            access_mode="company",
            status="idle",
        )
        db.add_all([source, target, second, foreign])
        await db.flush()
        db.add_all(
            [
                AgentAgentRelationship(
                    agent_id=source.id,
                    target_agent_id=target.id,
                    created_by_user_id=owner.id,
                ),
                AgentAgentRelationship(
                    agent_id=source.id,
                    target_agent_id=second.id,
                    created_by_user_id=owner.id,
                ),
            ]
        )
        await db.commit()
        return SimpleNamespace(
            tenant=tenant,
            owner=owner,
            source=source,
            target=target,
            second=second,
            foreign=foreign,
        )


async def test_supervision_schema_rejects_name_execution_and_requires_one_id():
    with pytest.raises(ValidationError):
        TaskCreate(
            title="legacy",
            type="supervision",
            supervision_target_name="Alex",
        )
    with pytest.raises(ValidationError):
        TaskCreate(title="missing", type="supervision")
    with pytest.raises(ValidationError):
        TaskCreate(
            title="ambiguous",
            type="supervision",
            supervision_target_user_id=uuid.uuid4(),
            supervision_target_agent_id=uuid.uuid4(),
        )


async def test_duplicate_names_do_not_affect_exact_agent_id_resolution():
    seeded = await _seed_agent_graph(duplicate_name=True)
    async with async_session() as db:
        first = await resolve_supervision_target(
            db,
            seeded.source.id,
            target_user_id=None,
            target_agent_id=seeded.target.id,
        )
        second = await resolve_supervision_target(
            db,
            seeded.source.id,
            target_user_id=None,
            target_agent_id=seeded.second.id,
        )
    assert first.target_id == seeded.target.id
    assert second.target_id == seeded.second.id
    assert first.display_name == second.display_name == "Same display name"


async def test_cross_tenant_and_suppressed_agent_targets_fail_closed():
    seeded = await _seed_agent_graph()
    async with async_session() as db:
        with pytest.raises(RecipientResolutionError) as cross:
            await resolve_supervision_target(
                db,
                seeded.source.id,
                target_user_id=None,
                target_agent_id=seeded.foreign.id,
            )
        assert cross.value.code == "recipient_not_found"
        db.add(
            RelationshipSuppression(
                agent_id=seeded.source.id,
                target_type="agent",
                target_id=seeded.target.id,
                created_by_user_id=seeded.owner.id,
            )
        )
        await db.commit()
    async with async_session() as db:
        with pytest.raises(RecipientResolutionError) as suppressed:
            await resolve_supervision_target(
                db,
                seeded.source.id,
                target_user_id=None,
                target_agent_id=seeded.target.id,
            )
        assert suppressed.value.code == "relationship_inactive"


async def test_reminder_uses_exact_agent_id_and_target_deletion_fails_closed(monkeypatch):
    from app.services import agent_tools, supervision_reminder

    seeded = await _seed_agent_graph(duplicate_name=True)
    async with async_session() as db:
        task = Task(
            agent_id=seeded.source.id,
            title="Canonical reminder",
            type="supervision",
            created_by=seeded.owner.id,
            supervision_target_agent_id=seeded.target.id,
            supervision_target_name=seeded.target.name,
            remind_schedule="daily",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

    canonical_send = AsyncMock(return_value="✅ exact target delivered")
    monkeypatch.setattr(agent_tools, "_send_message_to_agent", canonical_send)
    await supervision_reminder._send_supervision_reminder(task, seeded.source.name)
    canonical_send.assert_awaited_once()
    assert canonical_send.await_args.args[1]["agent_id"] == str(seeded.target.id)

    async with async_session() as db:
        target = await db.get(Agent, seeded.target.id)
        await db.delete(target)
        await db.commit()
    async with async_session() as db:
        quarantined = await db.get(Task, task.id)
        assert quarantined.supervision_target_agent_id is None

        from app.api.tasks import update_task
        from app.schemas.schemas import TaskUpdate

        current_user = await db.get(User, seeded.owner.id)
        closed = await update_task(
            agent_id=seeded.source.id,
            task_id=task.id,
            data=TaskUpdate(status="done"),
            current_user=current_user,
            db=db,
        )
        assert closed.status == "done"
        await db.commit()

    canonical_send.reset_mock()
    await supervision_reminder._send_supervision_reminder(
        quarantined, seeded.source.name
    )
    canonical_send.assert_not_awaited()
    async with async_session() as db:
        logs = (
            await db.execute(
                select(TaskLog).where(TaskLog.task_id == task.id)
            )
        ).scalars().all()
    assert any("migration_required" in log.content for log in logs)


async def test_canonical_a2a_writer_creates_same_tenant_session():
    from app.services.agent_tools import _send_message_to_agent

    seeded = await _seed_agent_graph()
    async with async_session() as db:
        target = await db.get(Agent, seeded.target.id)
        target.agent_type = "openclaw"
        await db.commit()

    result = await _send_message_to_agent(
        seeded.source.id,
        {
            "agent_id": str(seeded.target.id),
            "message": "canonical A2A smoke",
            "msg_type": "notify",
        },
        user_id=seeded.owner.id,
        origin_session_id=str(uuid.uuid4()),
        tool_call_id="supervision-a2a-smoke",
    )
    assert result.startswith("✅"), result
    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.source_channel == "agent",
                    ChatSession.agent_id.in_([seeded.source.id, seeded.target.id]),
                    ChatSession.peer_agent_id.in_([seeded.source.id, seeded.target.id]),
                )
            )
        ).scalars().first()
        assert session is not None
        assert session.user_id is None
        source = await db.get(Agent, session.agent_id)
        peer = await db.get(Agent, session.peer_agent_id)
        assert source.tenant_id == peer.tenant_id == seeded.tenant.id
