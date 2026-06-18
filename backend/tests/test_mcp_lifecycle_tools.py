from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.database import async_session, engine
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


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


async def _seed_user(tenant_id=None) -> User:
    async with async_session() as db:
        s = uuid.uuid4().hex[:12]
        ident = Identity(username=f"u_{s}", email=f"{s}@t.local", password_hash="x")
        db.add(ident)
        await db.flush()
        u = User(identity_id=ident.id, display_name="U", role="member", is_active=True, tenant_id=tenant_id)
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
        a = Agent(name=name, creator_id=creator.id, tenant_id=creator.tenant_id,
                  agent_type="native", access_mode=access_mode, status="idle")
        db.add(a)
        await db.flush()
        db.add(Participant(type="agent", ref_id=a.id, display_name=a.name))
        await db.commit()
        await db.refresh(a)
        return a


async def _mark_system(agent):
    from app.models.agent import Agent

    async with async_session() as db:
        await db.execute(update(Agent).where(Agent.id == agent.id).values(is_system=True))
        await db.commit()


async def _reload(agent):
    from app.models.agent import Agent

    async with async_session() as db:
        return (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()


async def test_delete_agent_requires_confirm():
    from app.mcp_server.tools_lifecycle import delete_agent_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    out = await delete_agent_impl(_ctx(token), str(agent.id))
    assert "confirm=True" in out or "confirm=true" in out.lower()
    assert "✅" not in out

    a = await _reload(agent)
    assert a.is_deleted is False


async def test_delete_agent_soft_deletes_and_hides():
    from app.mcp_server.tools import _resolve_visible_agent
    from app.mcp_server.tools_lifecycle import delete_agent_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    out = await delete_agent_impl(_ctx(token), str(agent.id), confirm=True)
    assert "✅" in out

    a = await _reload(agent)
    assert a.is_deleted is True
    assert a.deleted_at is not None

    # Soft-deleted agent is now hidden from visibility resolution.
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
        resolved = await _resolve_visible_agent(db, u, str(agent.id))
    assert resolved is None


async def test_restore_agent_brings_it_back():
    from app.mcp_server.tools import _resolve_visible_agent
    from app.mcp_server.tools_lifecycle import delete_agent_impl, restore_agent_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    await delete_agent_impl(_ctx(token), str(agent.id), confirm=True)
    assert (await _reload(agent)).is_deleted is True

    out = await restore_agent_impl(_ctx(token), agent=str(agent.id))
    assert "✅" in out

    a = await _reload(agent)
    assert a.is_deleted is False
    assert a.deleted_at is None

    # Visible again.
    async with async_session() as db:
        u = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
        resolved = await _resolve_visible_agent(db, u, str(agent.id))
    assert resolved is not None
    assert resolved.id == agent.id


async def test_delete_agent_system_blocked():
    from app.mcp_server.tools_lifecycle import delete_agent_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    await _mark_system(agent)
    token = await _pat(user, scope="write")

    out = await delete_agent_impl(_ctx(token), str(agent.id), confirm=True)
    assert "❌ 系统 agent" in out

    a = await _reload(agent)
    assert a.is_deleted is False


async def test_stop_agent_confirm():
    from app.mcp_server.tools_lifecycle import stop_agent_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    out = await stop_agent_impl(_ctx(token), str(agent.id))
    assert "confirm=True" in out or "confirm=true" in out.lower()
    assert "✅" not in out

    out2 = await stop_agent_impl(_ctx(token), str(agent.id), confirm=True)
    assert "✅" in out2
