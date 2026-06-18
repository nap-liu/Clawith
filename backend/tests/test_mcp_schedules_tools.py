"""Tests for MCP schedule tools (list/set/delete/run, creator-gated)."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.schedule import AgentSchedule
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


# ── helpers ───────────────────────────────────────────────────────────────────

def _ctx(token):
    h = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=h)))


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def _seed_user(tenant_id=None, role="member") -> User:
    async with async_session() as db:
        s = uuid.uuid4().hex[:12]
        ident = Identity(username=f"u_{s}", email=f"{s}@t.local", password_hash="x")
        db.add(ident)
        await db.flush()
        u = User(identity_id=ident.id, display_name="U", role=role, is_active=True, tenant_id=tenant_id)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u


async def _pat(user, scope="write"):
    from app.services.pat_service import issue_pat

    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="t", scope=scope)
    return token


async def _seed_agent(creator, name="Agent", access_mode="company"):
    from app.models.agent import Agent
    from app.models.participant import Participant

    async with async_session() as db:
        a = Agent(
            name=name,
            creator_id=creator.id,
            tenant_id=creator.tenant_id,
            agent_type="native",
            access_mode=access_mode,
            status="idle",
        )
        db.add(a)
        await db.flush()
        db.add(Participant(type="agent", ref_id=a.id, display_name=a.name))
        await db.commit()
        await db.refresh(a)
        return a


async def _seed_schedule(agent, user, name="daily", cron_expr="0 9 * * *", instruction="brief") -> AgentSchedule:
    from app.services.scheduler import compute_next_run

    async with async_session() as db:
        next_run = compute_next_run(cron_expr)
        s = AgentSchedule(
            agent_id=agent.id,
            name=name,
            instruction=instruction,
            cron_expr=cron_expr,
            is_enabled=True,
            next_run_at=next_run,
            created_by=user.id,
        )
        db.add(s)
        await db.commit()
        await db.refresh(s)
        return s


async def _reload_schedule(schedule_id) -> AgentSchedule | None:
    async with async_session() as db:
        return (
            await db.execute(select(AgentSchedule).where(AgentSchedule.id == schedule_id))
        ).scalar_one_or_none()


# ── tests ─────────────────────────────────────────────────────────────────────

async def test_set_schedule_creates():
    """Creator can create a schedule; DB row is persisted with next_run_at set."""
    from app.mcp_server.tools_schedules import set_agent_schedule_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    out = await set_agent_schedule_impl(
        _ctx(token),
        agent=str(agent.id),
        name="daily",
        cron_expr="0 9 * * *",
        instruction="brief",
    )
    assert "✅" in out, f"Expected ✅ in: {out}"

    # Verify DB row
    async with async_session() as db:
        result = await db.execute(
            select(AgentSchedule).where(AgentSchedule.agent_id == agent.id)
        )
        rows = result.scalars().all()
    assert len(rows) == 1, f"Expected 1 schedule row, got {len(rows)}"
    row = rows[0]
    assert row.cron_expr == "0 9 * * *"
    assert row.next_run_at is not None, "next_run_at should be set for enabled schedule"


async def test_set_schedule_invalid_cron():
    """Invalid cron expression returns ❌ error without creating a row."""
    from app.mcp_server.tools_schedules import set_agent_schedule_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    out = await set_agent_schedule_impl(
        _ctx(token),
        agent=str(agent.id),
        name="bad",
        cron_expr="not a cron",
        instruction="whatever",
    )
    assert "❌" in out
    assert "无效 cron" in out or "cron" in out.lower()

    # No row created
    async with async_session() as db:
        result = await db.execute(
            select(AgentSchedule).where(AgentSchedule.agent_id == agent.id)
        )
        rows = result.scalars().all()
    assert len(rows) == 0, "No schedule row should be created on invalid cron"


async def test_set_schedule_non_creator_denied():
    """A different same-tenant member cannot manage an agent's schedules."""
    from app.mcp_server.tools_schedules import set_agent_schedule_impl

    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id)
    other = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(owner, access_mode="company")  # company-visible
    token = await _pat(other, scope="write")

    out = await set_agent_schedule_impl(
        _ctx(token),
        agent=str(agent.id),
        name="daily",
        cron_expr="0 9 * * *",
        instruction="brief",
    )
    # Should be denied — either creator gate or no manage access
    assert "❌" in out, f"Expected denial, got: {out}"
    # The denial message should contain a relevant Chinese hint
    assert any(kw in out for kw in ("仅创建者", "无权", "找不到")), f"Unexpected denial text: {out}"


async def test_delete_schedule_confirm():
    """delete_agent_schedule requires confirm=True; without it returns guidance; with it removes the row."""
    from app.mcp_server.tools_schedules import delete_agent_schedule_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    sched = await _seed_schedule(agent, user)
    token = await _pat(user, scope="write")

    # Without confirm — should return guidance, not execute
    out_no_confirm = await delete_agent_schedule_impl(
        _ctx(token),
        agent=str(agent.id),
        schedule_id=str(sched.id),
        confirm=False,
    )
    assert "confirm=True" in out_no_confirm or "confirm=true" in out_no_confirm.lower(), (
        f"Expected confirm guidance, got: {out_no_confirm}"
    )
    assert "✅" not in out_no_confirm
    # Row still exists
    row = await _reload_schedule(sched.id)
    assert row is not None, "Schedule should still exist after unconfirmed delete"

    # With confirm=True — should delete
    out_confirm = await delete_agent_schedule_impl(
        _ctx(token),
        agent=str(agent.id),
        schedule_id=str(sched.id),
        confirm=True,
    )
    assert "✅" in out_confirm, f"Expected ✅ after confirmed delete, got: {out_confirm}"
    row = await _reload_schedule(sched.id)
    assert row is None, "Schedule row should be deleted after confirmed delete"


async def test_run_schedule_requires_confirm():
    """run_agent_schedule without confirm returns guidance and does not execute."""
    from app.mcp_server.tools_schedules import run_agent_schedule_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    sched = await _seed_schedule(agent, user)
    token = await _pat(user, scope="write")

    out = await run_agent_schedule_impl(
        _ctx(token),
        agent=str(agent.id),
        schedule_id=str(sched.id),
        confirm=False,
    )
    # Must show confirmation guidance without executing
    assert "confirm=True" in out or "confirm=true" in out.lower(), (
        f"Expected confirm guidance, got: {out}"
    )
    assert "✅" not in out

    # run_count should be unchanged
    row = await _reload_schedule(sched.id)
    assert row is not None
    assert row.run_count == 0, f"run_count should be 0 (no execution), got {row.run_count}"
