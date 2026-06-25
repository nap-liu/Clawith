"""Tests for confirmation_service.create_confirmation and serialize_confirmation_for_display."""

import uuid
import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.database import async_session, engine, Base
from app.models.agent_confirmation import AgentConfirmation
from app.models.agent import Agent  # noqa: F401 — needed so FK resolves in create_all
from app.models.user import User, Identity  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.services import confirmation_service

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


async def _make_agent():
    """Minimal helper: seed a Tenant + Identity + User + Agent, return (agent.id, agent.creator_id)."""
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
        return agent.id, user.id


async def test_create_confirmation_persists_and_broadcasts():
    agent_id, user_id = await _make_agent()

    with patch("app.api.websocket.manager.send_to_session", new=AsyncMock()) as bc:
        c = await confirmation_service.create_confirmation(
            agent_id=agent_id,
            conversation_id="conv-1",
            chat_session_id=None,
            source_channel="web",
            title="创建采购单",
            summary="¥48,500",
            action={"tool": "sql_execute", "args": {"sql": "INSERT ..."}},
            risk_level="high",
            requested_by_user_id=user_id,
        )

    assert c.status == "pending"
    assert c.expires_at is not None
    bc.assert_awaited_once()
    payload = bc.await_args.args[2]
    assert payload["type"] == "confirmation_card"
    assert payload["confirmation_id"] == str(c.id)
    assert payload["title"] == "创建采购单"


def test_serialize_shape():
    agent_id = uuid.uuid4()
    c = AgentConfirmation(
        id=uuid.uuid4(),
        agent_id=agent_id,
        conversation_id="c",
        title="t",
        summary="s",
        action={"tool": "sql_execute", "args": {}},
        status="pending",
        risk_level="medium",
        expires_at=datetime.datetime.now(datetime.timezone.utc),
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )
    out = confirmation_service.serialize_confirmation_for_display(c)
    assert out["role"] == "confirmation"
    assert out["confirmation_id"] == str(c.id)
    assert out["action_preview"] == "sql_execute"
    assert out["status"] == "pending"
