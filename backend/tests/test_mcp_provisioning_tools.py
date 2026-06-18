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


async def _seed_admin_user(tenant_id=None) -> "User":
    """Seed a user with org_admin role."""
    async with async_session() as db:
        import uuid as _uuid2
        s = _uuid2.uuid4().hex[:12]
        from app.models.user import Identity, User
        ident = Identity(username=f"admin_{s}", email=f"admin_{s}@t.local", password_hash="x")
        db.add(ident); await db.flush()
        u = User(identity_id=ident.id, display_name="Admin", role="org_admin", is_active=True, tenant_id=tenant_id)
        db.add(u); await db.commit(); await db.refresh(u); return u


async def test_update_agent_sets_many_fields():
    """update_agent_impl with multiple new fields: persists values + reports before→after + revert hint."""
    from app.mcp_server.tools_provisioning import update_agent_impl
    from app.models.agent import Agent
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user, name="OrigName")
    token = await _pat(user, scope="write")

    out = await update_agent_impl(
        _ctx(token),
        agent=str(agent.id),
        name="NewName",
        welcome_message="Hello!",
        autonomy_policy={"read_files": "L1"},
        max_tokens_per_day=5000,
        timezone="Asia/Shanghai",
        heartbeat_enabled=True,
    )

    assert "✅" in out
    # before→after reporting
    assert "→" in out
    # revert hint
    assert "回滚" in out or "↩" in out

    # Verify DB
    async with async_session() as db:
        a = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
    assert a.name == "NewName"
    assert a.welcome_message == "Hello!"
    assert a.autonomy_policy == {"read_files": "L1"}
    assert a.max_tokens_per_day == 5000
    assert a.timezone == "Asia/Shanghai"
    assert a.heartbeat_enabled is True


async def test_update_agent_expires_at_admin_only():
    """Non-admin user cannot set expires_at."""
    from app.mcp_server.tools_provisioning import update_agent_impl
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)  # role=member
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    out = await update_agent_impl(_ctx(token), agent=str(agent.id), expires_at="2030-01-01T00:00:00+00:00")
    # Must deny non-admin
    assert "管理员" in out or "admin" in out.lower() or "❌" in out
    # Must NOT be a success
    assert "✅" not in out


async def test_update_agent_name_syncs_participant():
    """Changing agent name must update the Participant.display_name."""
    from app.mcp_server.tools_provisioning import update_agent_impl
    from app.models.participant import Participant
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user, name="OldParticipantName")
    token = await _pat(user, scope="write")

    out = await update_agent_impl(_ctx(token), agent=str(agent.id), name="NewParticipantName")
    assert "✅" in out

    async with async_session() as db:
        p = (await db.execute(
            select(Participant).where(Participant.type == "agent", Participant.ref_id == agent.id)
        )).scalar_one_or_none()
    assert p is not None
    assert p.display_name == "NewParticipantName"


async def test_create_agent_with_autonomy_and_tokens():
    """create_agent_impl accepts autonomy_policy + max_tokens_per_day + access_mode=private."""
    from app.mcp_server.tools_provisioning import create_agent_impl
    from app.models.agent import Agent
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    token = await _pat(user, scope="write")

    autonomy = {"read_files": "L1", "write_workspace_files": "L2"}
    out = await create_agent_impl(
        _ctx(token),
        name="AutonomyAgent",
        autonomy_policy=autonomy,
        max_tokens_per_day=9999,
        access_mode="private",
    )

    assert "✅" in out
    assert "AutonomyAgent" in out

    async with async_session() as db:
        a = (await db.execute(
            select(Agent).where(Agent.name == "AutonomyAgent", Agent.creator_id == user.id)
        )).scalar_one_or_none()
    assert a is not None
    assert a.autonomy_policy == autonomy
    assert a.max_tokens_per_day == 9999
    assert a.access_mode == "private"
