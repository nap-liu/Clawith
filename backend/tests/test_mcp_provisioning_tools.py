from __future__ import annotations
import uuid
import pytest
from types import SimpleNamespace
from sqlalchemy import select
from app.database import async_session, engine
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose(); yield; await engine.dispose()


def _ctx(token):
    h = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=h)))


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t); await db.commit(); await db.refresh(t); return t


async def _seed_user(tenant_id=None) -> User:
    async with async_session() as db:
        s = uuid.uuid4().hex[:12]
        ident = Identity(username=f"u_{s}", email=f"{s}@t.local", password_hash="x")
        db.add(ident); await db.flush()
        u = User(identity_id=ident.id, display_name="U", role="member", is_active=True, tenant_id=tenant_id)
        db.add(u); await db.commit(); await db.refresh(u); return u


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
        db.add(a); await db.flush()
        db.add(Participant(type="agent", ref_id=a.id, display_name=a.name))
        await db.commit(); await db.refresh(a); return a


async def test_create_agent_requires_write_scope():
    from app.mcp_server.tools_provisioning import create_agent_impl
    tenant = await _seed_tenant(); user = await _seed_user(tenant_id=tenant.id)
    token = await _pat(user, scope="read")
    out = await create_agent_impl(_ctx(token), name="Bob")
    assert "需要 write" in out


async def test_create_agent_write_scope_creates():
    from app.mcp_server.tools_provisioning import create_agent_impl
    from app.models.agent import Agent
    tenant = await _seed_tenant(); user = await _seed_user(tenant_id=tenant.id)
    token = await _pat(user, scope="write")
    out = await create_agent_impl(_ctx(token), name="BobCreate", role_description="helper")
    assert "BobCreate" in out
    async with async_session() as db:
        a = (await db.execute(
            select(Agent).where(Agent.name == "BobCreate", Agent.creator_id == user.id)
        )).scalar_one_or_none()
    assert a is not None and a.creator_id == user.id and a.tenant_id == tenant.id


async def test_update_agent_changes_role():
    from app.mcp_server.tools_provisioning import update_agent_impl
    from app.models.agent import Agent
    tenant = await _seed_tenant(); user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")
    out = await update_agent_impl(_ctx(token), agent=str(agent.id), role_description="new role")
    assert "✅" in out
    async with async_session() as db:
        a = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
    assert a.role_description == "new role"


async def test_update_agent_denied_without_manage():
    from app.mcp_server.tools_provisioning import update_agent_impl
    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id)
    other = await _seed_user(tenant_id=tenant.id)
    private_agent = await _seed_agent(owner, name="Sec", access_mode="private")
    token = await _pat(other, scope="write")
    out = await update_agent_impl(_ctx(token), agent=str(private_agent.id), role_description="x")
    assert ("无权" in out) or ("找不到" in out)
