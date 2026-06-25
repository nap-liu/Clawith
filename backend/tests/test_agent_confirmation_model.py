"""Smoke test for AgentConfirmation model — create, read-back, default values."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database import async_session, engine, Base
from app.models.agent_confirmation import AgentConfirmation
from app.models.agent import Agent  # noqa: F401 — needed so FK resolves in create_all
from app.models.user import User, Identity  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _setup_table():
    """Create agent_confirmations table if it doesn't exist yet (idempotent)."""
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c, tables=[AgentConfirmation.__table__], checkfirst=True
            )
        )
    yield
    await engine.dispose()


async def _make_agent() -> uuid.UUID:
    """Minimal helper: seed a Tenant + Identity + User + Agent, return agent.id."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        tenant = Tenant(name=f"t_{suffix}", slug=f"t-{suffix}")
        db.add(tenant)
        await db.flush()

        identity = Identity(username=f"u_{suffix}", email=f"{suffix}@t.local", password_hash="x")
        db.add(identity)
        await db.flush()

        user = User(identity_id=identity.id, display_name="U", role="member", is_active=True, tenant_id=tenant.id)
        db.add(user)
        await db.flush()

        agent = Agent(name=f"Agent_{suffix}", creator_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id


async def test_create_and_read_confirmation():
    agent_id = await _make_agent()
    async with async_session() as db:
        c = AgentConfirmation(
            agent_id=agent_id,
            conversation_id="conv-1",
            title="创建采购单 PO-1",
            summary="金额 ¥48,500",
            action={"tool": "sql_execute", "args": {"sql": "INSERT ..."}},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        db.add(c)
        await db.commit()
        await db.refresh(c)

        assert c.id is not None
        assert c.status == "pending"
        assert c.risk_level == "medium"
        assert c.source_channel == "web"
        assert c.action["tool"] == "sql_execute"
        assert c.created_at is not None

        # Read back from DB
        result = await db.execute(select(AgentConfirmation).where(AgentConfirmation.id == c.id))
        fetched = result.scalar_one_or_none()
        assert fetched is not None
        assert fetched.title == "创建采购单 PO-1"
        assert fetched.summary == "金额 ¥48,500"
        assert fetched.action["args"]["sql"] == "INSERT ..."
