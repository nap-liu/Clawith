"""Tests for override CRUD + ACL.

ACL matrix:
- GET /overrides → platform_admin (full list) OR agent creator (?agent_id=<id>, own scope only)
- PUT/DELETE /overrides/tenant/{tenant_id} → platform_admin OR org_admin (of that tenant)
- PUT/DELETE /overrides/agent/{agent_id} → platform_admin OR agent's creator
"""
import uuid
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.tenant import Tenant
from app.models.agent import Agent
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.core.security import create_access_token

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def client():
    from app.main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _make_user(role: str, tenant_id=None) -> tuple[User, str]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        if tenant_id is None and role == "org_admin":
            tenant = Tenant(name=f"T_{suffix}", slug=f"t-{suffix}")
            db.add(tenant)
            await db.flush()
            tenant_id = tenant.id
        identity = Identity(
            username=f"u_{suffix}", email=f"u_{suffix}@x.local", password_hash="x",
            is_platform_admin=(role == "platform_admin"),
        )
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="U", role=role,
                    is_active=True, tenant_id=tenant_id)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user, create_access_token(str(user.id), role)


async def _make_server() -> MCPServer:
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        srv = MCPServer(
            name=f"s_{suffix}", display_name="s",
            base_url_template=f"https://s-{suffix}.example", headers_template={},
            system_prompt_block="PLATFORM",
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        return srv


async def test_get_overrides_requires_platform_admin(client):
    srv = await _make_server()
    _, member_token = await _make_user("member")
    r = await client.get(
        f"/api/admin/mcp-servers/{srv.id}/overrides",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert r.status_code == 403


async def test_get_overrides_returns_grouped(client):
    srv = await _make_server()
    t_id = uuid.uuid4()
    a_id = uuid.uuid4()
    async with async_session() as db:
        db.add_all([
            MCPServerOverride(
                mcp_server_id=srv.id, scope_type="tenant", scope_id=t_id,
                system_prompt_block="T",
            ),
            MCPServerOverride(
                mcp_server_id=srv.id, scope_type="agent", scope_id=a_id,
                system_prompt_block="A",
            ),
        ])
        await db.commit()

    _, admin_token = await _make_user("platform_admin")
    r = await client.get(
        f"/api/admin/mcp-servers/{srv.id}/overrides",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["tenant"]) >= 1
    assert any(t["scope_id"] == str(t_id) for t in body["tenant"])
    assert any(a["scope_id"] == str(a_id) for a in body["agent"])


async def test_put_tenant_override_creates_then_updates(client):
    srv = await _make_server()
    user, admin_token = await _make_user("platform_admin")
    t_id = uuid.uuid4()
    # PUT (create)
    r = await client.put(
        f"/api/admin/mcp-servers/{srv.id}/overrides/tenant/{t_id}",
        json={"system_prompt_block": "TENANT-BLOCK"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["system_prompt_block"] == "TENANT-BLOCK"

    # PUT (update — same scope_id) is idempotent upsert
    r2 = await client.put(
        f"/api/admin/mcp-servers/{srv.id}/overrides/tenant/{t_id}",
        json={"system_prompt_block": "TENANT-BLOCK-V2"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r2.status_code == 200
    assert r2.json()["system_prompt_block"] == "TENANT-BLOCK-V2"
    assert r2.json()["id"] == r.json()["id"]  # same row, not a new one


async def test_put_tenant_override_requires_platform_or_matching_org_admin(client):
    srv = await _make_server()
    other_tenant_id = uuid.uuid4()
    # org_admin from a different tenant should be 403
    _, other_org_token = await _make_user("org_admin")  # creates own tenant
    r = await client.put(
        f"/api/admin/mcp-servers/{srv.id}/overrides/tenant/{other_tenant_id}",
        json={"system_prompt_block": "X"},
        headers={"Authorization": f"Bearer {other_org_token}"},
    )
    assert r.status_code == 403


async def test_put_agent_override_requires_agent_creator_or_platform_admin(client):
    srv = await _make_server()
    # Make creator + agent
    creator, _ = await _make_user("member")
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        agent = Agent(name=f"A_{suffix}", creator_id=creator.id)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        agent_id = agent.id

    # Some other random user — should be 403
    _, other_token = await _make_user("member")
    r = await client.put(
        f"/api/admin/mcp-servers/{srv.id}/overrides/agent/{agent_id}",
        json={"system_prompt_block": "AG"},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert r.status_code == 403

    # Creator can do it
    creator_token = create_access_token(str(creator.id), "member")
    r2 = await client.put(
        f"/api/admin/mcp-servers/{srv.id}/overrides/agent/{agent_id}",
        json={"system_prompt_block": "AG"},
        headers={"Authorization": f"Bearer {creator_token}"},
    )
    assert r2.status_code == 200
    assert r2.json()["system_prompt_block"] == "AG"


async def test_get_overrides_agent_creator_can_read_own_agent_scope(client):
    """Agent creator can GET /overrides?agent_id=<id> and sees only their own agent's rows."""
    srv = await _make_server()
    creator, _ = await _make_user("member")
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        agent = Agent(name=f"A_{suffix}", creator_id=creator.id)
        db.add(agent)
        await db.flush()
        # Override for this agent
        db.add(MCPServerOverride(
            mcp_server_id=srv.id, scope_type="agent", scope_id=agent.id,
            system_prompt_block="CREATOR-BLOCK",
        ))
        # Another agent's override (should NOT be visible to the creator)
        db.add(MCPServerOverride(
            mcp_server_id=srv.id, scope_type="agent", scope_id=uuid.uuid4(),
            system_prompt_block="OTHER",
        ))
        await db.commit()
        await db.refresh(agent)
        agent_id = agent.id

    creator_token = create_access_token(str(creator.id), "member")
    r = await client.get(
        f"/api/admin/mcp-servers/{srv.id}/overrides",
        params={"agent_id": str(agent_id)},
        headers={"Authorization": f"Bearer {creator_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tenant"] == []
    assert len(body["agent"]) == 1
    assert body["agent"][0]["scope_id"] == str(agent_id)
    assert body["agent"][0]["system_prompt_block"] == "CREATOR-BLOCK"


async def test_get_overrides_non_admin_without_agent_id_is_403(client):
    """Non-admin calling GET /overrides without agent_id must get 403."""
    srv = await _make_server()
    _, member_token = await _make_user("member")
    r = await client.get(
        f"/api/admin/mcp-servers/{srv.id}/overrides",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert r.status_code == 403


async def test_delete_override_clears_row(client):
    srv = await _make_server()
    _, admin_token = await _make_user("platform_admin")
    t_id = uuid.uuid4()
    # Create
    await client.put(
        f"/api/admin/mcp-servers/{srv.id}/overrides/tenant/{t_id}",
        json={"system_prompt_block": "X"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    # Delete
    r = await client.delete(
        f"/api/admin/mcp-servers/{srv.id}/overrides/tenant/{t_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 204

    # Verify gone
    async with async_session() as db:
        rows = (await db.execute(
            select(MCPServerOverride).where(
                MCPServerOverride.mcp_server_id == srv.id,
                MCPServerOverride.scope_type == "tenant",
                MCPServerOverride.scope_id == t_id,
            )
        )).scalars().all()
        assert rows == []
