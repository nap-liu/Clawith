"""PATCH / dry-run / test-connection must accept: platform_admin, server creator,
same-tenant org_admin, same-tenant agent_admin. Reject everyone else."""
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.tenant import Tenant
from app.models.mcp_server import MCPServer
from app.core.security import create_access_token

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _mk_tenant() -> uuid.UUID:
    """Create a real Tenant row (required because users.tenant_id has a FK)."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        tenant = Tenant(name=f"tenant_{suffix}", slug=f"tenant-{suffix}")
        db.add(tenant)
        await db.commit()
        await db.refresh(tenant)
        return tenant.id


async def _mk_user(role: str, tenant_id: uuid.UUID | None = None) -> tuple[uuid.UUID, str]:
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(username=f"u_{suffix}", email=f"u_{suffix}@x.local", password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="U", role=role, is_active=True, tenant_id=tenant_id)
        db.add(user)
        await db.commit()
        await db.refresh(user)
    return user.id, create_access_token(str(user.id), role)


async def _mk_server(creator_id: uuid.UUID, tenant_id: uuid.UUID | None = None) -> uuid.UUID:
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        srv = MCPServer(
            name=f"s_{suffix}",
            display_name="s",
            base_url_template="https://x.example",
            tenant_id=tenant_id,
            created_by_user_id=creator_id,
        )
        db.add(srv)
        await db.commit()
        return srv.id


async def _patch(client: AsyncClient, sid: uuid.UUID, token: str) -> int:
    resp = await client.patch(
        f"/api/admin/mcp-servers/{sid}",
        headers={"Authorization": f"Bearer {token}"},
        json={"display_name": "updated"},
    )
    return resp.status_code


async def test_owner_member_can_patch():
    owner_id, owner_token = await _mk_user("member")
    sid = await _mk_server(owner_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _patch(client, sid, owner_token) == 200


async def test_non_owner_member_is_forbidden():
    owner_id, _ = await _mk_user("member")
    sid = await _mk_server(owner_id)
    _, intruder_token = await _mk_user("member")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _patch(client, sid, intruder_token) == 403


async def test_same_tenant_org_admin_can_patch():
    tid = await _mk_tenant()
    owner_id, _ = await _mk_user("member", tenant_id=tid)
    sid = await _mk_server(owner_id, tenant_id=tid)
    _, orga_token = await _mk_user("org_admin", tenant_id=tid)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _patch(client, sid, orga_token) == 200


async def test_cross_tenant_org_admin_is_forbidden():
    owner_tid = await _mk_tenant()
    other_tid = await _mk_tenant()
    owner_id, _ = await _mk_user("member", tenant_id=owner_tid)
    sid = await _mk_server(owner_id, tenant_id=owner_tid)
    _, foreign_token = await _mk_user("org_admin", tenant_id=other_tid)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _patch(client, sid, foreign_token) == 403


async def test_same_tenant_agent_admin_can_patch():
    tid = await _mk_tenant()
    owner_id, _ = await _mk_user("member", tenant_id=tid)
    sid = await _mk_server(owner_id, tenant_id=tid)
    _, aa_token = await _mk_user("agent_admin", tenant_id=tid)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _patch(client, sid, aa_token) == 200


async def test_platform_admin_can_patch_cross_tenant():
    tid_a = await _mk_tenant()
    tid_b = await _mk_tenant()
    owner_id, _ = await _mk_user("member", tenant_id=tid_a)
    sid = await _mk_server(owner_id, tenant_id=tid_b)
    _, pa_token = await _mk_user("platform_admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _patch(client, sid, pa_token) == 200
