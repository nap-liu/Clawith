"""dry-run with current_user identity and a real agent_id resolves
${agent.name} to the actual agent's name (not empty string).

Also covers the new behavior: tenant_id is auto-inferred from the server
(or the caller) when not explicitly provided.
"""
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.tenant import Tenant
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.core.security import create_access_token

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _mk_tenant() -> uuid.UUID:
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        t = Tenant(name=f"t_{suffix}", slug=f"t-{suffix}")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t.id


async def test_current_user_identity_resolves_agent_name_from_db():
    suffix = uuid.uuid4().hex[:6]
    tid = await _mk_tenant()
    async with async_session() as db:
        identity = Identity(username=f"u_{suffix}", email=f"u_{suffix}@x.local", password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="U", role="platform_admin", is_active=True, tenant_id=tid)
        db.add(user)
        await db.flush()
        agent = Agent(name="MyAgent", creator_id=user.id, tenant_id=tid)
        db.add(agent)
        await db.flush()
        srv = MCPServer(
            name=f"s_{suffix}",
            display_name="s",
            base_url_template="https://srv.example/${agent.name}",
            system_prompt_block="hello ${agent.name}",
            tenant_id=tid,
            created_by_user_id=user.id,
        )
        db.add(srv)
        await db.commit()
        server_id, agent_id = srv.id, agent.id

    token = create_access_token(str(user.id), "platform_admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Note: tenant_id deliberately omitted — server's tenant should be inferred
        resp = await client.post(
            f"/api/admin/mcp-servers/{server_id}/dry-run",
            headers={"Authorization": f"Bearer {token}"},
            json={"identity": "current_user", "scope": "agent", "agent_id": str(agent_id)},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["resolved_url"] == "https://srv.example/MyAgent", body
    assert "hello MyAgent" in body["resolved_prompt"], body


async def test_org_admin_can_dry_run_same_tenant_agent_override():
    """Phase 2 verify: org_admin (not agent creator) can preview an agent's
    override in their own tenant via dry-run + can read the server detail."""
    suffix = uuid.uuid4().hex[:6]
    tid = await _mk_tenant()
    async with async_session() as db:
        # creator = a regular member (not the caller)
        creator_id_holder = Identity(username=f"c_{suffix}", email=f"c_{suffix}@x.local", password_hash="x")
        db.add(creator_id_holder)
        await db.flush()
        creator = User(identity_id=creator_id_holder.id, display_name="C", role="member", is_active=True, tenant_id=tid)
        db.add(creator)
        await db.flush()
        agent = Agent(name="X", creator_id=creator.id, tenant_id=tid)
        db.add(agent)
        await db.flush()
        srv = MCPServer(
            name=f"s_{suffix}",
            display_name="s",
            base_url_template="https://srv.example",
            tenant_id=tid,
            created_by_user_id=creator.id,
        )
        db.add(srv)
        await db.flush()
        # caller = org_admin in the same tenant, NOT the creator
        oa_id_h = Identity(username=f"oa_{suffix}", email=f"oa_{suffix}@x.local", password_hash="x")
        db.add(oa_id_h)
        await db.flush()
        oa = User(identity_id=oa_id_h.id, display_name="OA", role="org_admin", is_active=True, tenant_id=tid)
        db.add(oa)
        await db.commit()
        server_id, agent_id, oa_id = srv.id, agent.id, oa.id

    token = create_access_token(str(oa_id), "org_admin")
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET server detail must succeed (was platform-admin-only pre-phase2)
        get_resp = await client.get(f"/api/admin/mcp-servers/{server_id}", headers=headers)
        assert get_resp.status_code == 200, get_resp.text

        # dry-run for an agent the org_admin doesn't personally own
        dr_resp = await client.post(
            f"/api/admin/mcp-servers/{server_id}/dry-run",
            headers=headers,
            json={"identity": "current_user", "scope": "agent", "agent_id": str(agent_id)},
        )
        assert dr_resp.status_code == 200, dr_resp.text

        # Override list/put on someone else's agent — same-tenant org_admin OK
        list_resp = await client.get(
            f"/api/admin/mcp-servers/{server_id}/overrides?agent_id={agent_id}",
            headers=headers,
        )
        assert list_resp.status_code == 200, list_resp.text
