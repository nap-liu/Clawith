"""Tests for confirmation-card merge into session history + non-web text fallback.

Test 1 — History merge:
  Build a session with a user message, an AgentConfirmation (pending, created
  between the two messages), and an assistant message created after the
  confirmation. Call get_session_messages directly (direct-handler pattern —
  no ASGI / no `from app.main import app`), assert:
  - The confirmation item appears in the list with role=="confirmation"
  - It is ordered between the user message and the assistant message
  - status is "pending"
  Then update the confirmation to "executed", re-fetch, assert status=="executed".

Test 2 — Non-web fallback does not raise:
  Call create_confirmation with source_channel="feishu". Patch _broadcast and
  _get_agent_tenant_id so no real network calls happen. Assert the returned
  AgentConfirmation row is valid and no exception is raised.
"""

from __future__ import annotations

import uuid
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api.chat_sessions import get_session_messages
from app.database import async_session, engine, Base
from app.models.agent import Agent  # noqa: F401 — FK dep
from app.models.agent_confirmation import AgentConfirmation
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction  # noqa: F401 — FK dep for chat_messages.compacted_into
from app.models.chat_session import ChatSession
from app.models.participant import Participant  # noqa: F401 — FK dep for chat_sessions.participant_id
from app.models.tenant import Tenant  # noqa: F401
from app.models.user import Identity, User  # noqa: F401
from app.services import confirmation_service

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# DB setup: create all required tables once per test run (idempotent)
# ---------------------------------------------------------------------------

_TABLES = [
    Tenant.__table__,
    Identity.__table__,
    User.__table__,
    Agent.__table__,
    Participant.__table__,
    ChatSession.__table__,
    ChatCompaction.__table__,
    ChatMessage.__table__,
    AgentConfirmation.__table__,
]


@pytest.fixture(autouse=True)
async def _setup_tables():
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=_TABLES, checkfirst=True)
        )
    yield
    await engine.dispose()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_agent_and_user():
    """Create Tenant + Identity + User + Agent; return (agent, user)."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        tenant = Tenant(name=f"t_{suffix}", slug=f"t-{suffix}")
        db.add(tenant)
        await db.flush()

        identity = Identity(username=f"u_{suffix}", email=f"{suffix}@t.local", password_hash="x")
        db.add(identity)
        await db.flush()

        user = User(
            identity_id=identity.id,
            display_name="Tester",
            role="org_admin",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()

        agent = Agent(name=f"Agent_{suffix}", creator_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.commit()
        await db.refresh(user)
        await db.refresh(agent)
        return agent, user


async def _seed_session(agent, user) -> ChatSession:
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            source_channel="web",
            title="Test session",
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session


def _ts(offset_seconds: float) -> datetime.datetime:
    """Return a UTC datetime offset_seconds from a fixed base for ordering."""
    base = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    return base + datetime.timedelta(seconds=offset_seconds)


async def _seed_message(agent, user, session, role: str, content: str, ts: datetime.datetime) -> ChatMessage:
    async with async_session() as db:
        msg = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role=role,
            content=content,
            conversation_id=str(session.id),
            created_at=ts,
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        return msg


async def _seed_confirmation(agent, session, ts: datetime.datetime) -> AgentConfirmation:
    async with async_session() as db:
        c = AgentConfirmation(
            agent_id=agent.id,
            conversation_id=str(session.id),
            chat_session_id=session.id,
            source_channel="web",
            title="Test op",
            summary="Summary",
            action={"tool": "sql_execute", "args": {}},
            risk_level="high",
            status="pending",
            expires_at=_ts(9999),
            created_at=ts,
        )
        db.add(c)
        await db.commit()
        await db.refresh(c)
        return c


# ---------------------------------------------------------------------------
# Test 1 — history merge: confirmation appears in-order + status is live
# ---------------------------------------------------------------------------


async def test_confirmation_merged_into_history_in_order():
    agent, user = await _seed_agent_and_user()
    session = await _seed_session(agent, user)

    # t=0 user message, t=1 confirmation, t=2 assistant message
    await _seed_message(agent, user, session, "user", "Hello", _ts(0))
    conf = await _seed_confirmation(agent, session, _ts(1))
    await _seed_message(agent, user, session, "assistant", "Hi", _ts(2))

    current_user = SimpleNamespace(
        id=user.id,
        role="org_admin",
        tenant_id=user.tenant_id,
        is_active=True,
    )

    async def _fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    with patch("app.api.chat_sessions.check_agent_access", side_effect=_fake_check_agent_access):
        async with async_session() as db:
            result = await get_session_messages(
                agent_id=agent.id,
                session_id=session.id,
                limit=50,
                before=None,
                current_user=current_user,
                db=db,
            )

    roles = [m["role"] for m in result]
    assert "confirmation" in roles, f"Expected confirmation in {roles}"

    # Exactly one confirmation item
    conf_items = [m for m in result if m["role"] == "confirmation"]
    assert len(conf_items) == 1
    ci = conf_items[0]
    assert ci["status"] == "pending"
    assert ci["confirmation_id"] == str(conf.id)

    # Ordering: user < confirmation < assistant
    idx_user = next(i for i, m in enumerate(result) if m["role"] == "user")
    idx_conf = next(i for i, m in enumerate(result) if m["role"] == "confirmation")
    idx_asst = next(i for i, m in enumerate(result) if m["role"] == "assistant")
    assert idx_user < idx_conf < idx_asst, f"Expected user<conf<assistant, got indices {idx_user},{idx_conf},{idx_asst}"


async def test_confirmation_status_reflects_db_update():
    """Re-fetching after updating to 'executed' shows updated status."""
    agent, user = await _seed_agent_and_user()
    session = await _seed_session(agent, user)

    await _seed_message(agent, user, session, "user", "Hello", _ts(0))
    conf = await _seed_confirmation(agent, session, _ts(1))
    await _seed_message(agent, user, session, "assistant", "Hi", _ts(2))

    # Update confirmation status to executed
    async with async_session() as db:
        from sqlalchemy import select as _select
        row = await db.execute(_select(AgentConfirmation).where(AgentConfirmation.id == conf.id))
        c = row.scalar_one()
        c.status = "executed"
        await db.commit()

    current_user = SimpleNamespace(
        id=user.id,
        role="org_admin",
        tenant_id=user.tenant_id,
        is_active=True,
    )

    async def _fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    with patch("app.api.chat_sessions.check_agent_access", side_effect=_fake_check_agent_access):
        async with async_session() as db:
            result = await get_session_messages(
                agent_id=agent.id,
                session_id=session.id,
                limit=50,
                before=None,
                current_user=current_user,
                db=db,
            )

    conf_items = [m for m in result if m["role"] == "confirmation"]
    assert len(conf_items) == 1
    assert conf_items[0]["status"] == "executed"


# ---------------------------------------------------------------------------
# Test 1c — pagination: a card in an older page appears on that page only (B2)
# ---------------------------------------------------------------------------


async def test_confirmation_merge_respects_pagination():
    """A confirmation older than the newest page must NOT be re-injected into
    every page — it belongs to exactly one page (B2 regression guard)."""
    agent, user = await _seed_agent_and_user()
    session = await _seed_session(agent, user)

    # m0(t0) m1(t1) [conf t1.5] m2(t2) m3(t3)
    await _seed_message(agent, user, session, "user", "m0", _ts(0))
    await _seed_message(agent, user, session, "assistant", "m1", _ts(1))
    conf = await _seed_confirmation(agent, session, _ts(1.5))
    await _seed_message(agent, user, session, "user", "m2", _ts(2))
    await _seed_message(agent, user, session, "assistant", "m3", _ts(3))

    current_user = SimpleNamespace(
        id=user.id, role="org_admin", tenant_id=user.tenant_id, is_active=True
    )

    async def _fake_check_agent_access(_db, _user, _agent_id):
        return agent, "manage"

    with patch("app.api.chat_sessions.check_agent_access", side_effect=_fake_check_agent_access):
        async with async_session() as db:
            page1 = await get_session_messages(
                agent_id=agent.id, session_id=session.id,
                limit=2, before=None, current_user=current_user, db=db,
            )
        # Newest page = m2,m3; the older confirmation (t1.5) must NOT appear here.
        assert [m["role"] for m in page1] == ["user", "assistant"], page1
        before_cursor = page1[0]["created_at"]  # oldest row of page 1 → next cursor

        with patch("app.api.chat_sessions.check_agent_access", side_effect=_fake_check_agent_access):
            async with async_session() as db:
                page2 = await get_session_messages(
                    agent_id=agent.id, session_id=session.id,
                    limit=2, before=before_cursor, current_user=current_user, db=db,
                )

    # Older page = m0,m1 + the confirmation, exactly once.
    conf_items = [m for m in page2 if m["role"] == "confirmation"]
    assert len(conf_items) == 1, page2
    assert conf_items[0]["confirmation_id"] == str(conf.id)


# ---------------------------------------------------------------------------
# Test 2 — non-web fallback does not raise
# ---------------------------------------------------------------------------


async def test_create_confirmation_non_web_channel_does_not_raise():
    """create_confirmation with source_channel='feishu' must not raise."""
    agent, user = await _seed_agent_and_user()

    _PATCH_BCAST = "app.services.confirmation_service._broadcast"
    # _get_agent_tenant_id is lazily imported from agent_tools inside create_confirmation;
    # patch it at the source module so the lazy import resolves to the mock.
    _PATCH_TENANT = "app.services.agent_tools._get_agent_tenant_id"

    with (
        patch(_PATCH_BCAST, new=AsyncMock()),
        patch(_PATCH_TENANT, new=AsyncMock(return_value=None)),
    ):
        c = await confirmation_service.create_confirmation(
            agent_id=agent.id,
            conversation_id="conv-feishu-test",
            chat_session_id=None,
            source_channel="feishu",
            title="Feishu op",
            summary="Summary",
            action=None,
            risk_level="medium",
            requested_by_user_id=user.id,
        )

    assert c is not None
    assert c.status == "pending"
    assert c.source_channel == "feishu"
    assert str(c.agent_id) == str(agent.id)
