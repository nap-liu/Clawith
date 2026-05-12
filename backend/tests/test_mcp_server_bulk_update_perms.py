"""PUT /api/tools/mcp-server must reject non-owner / non-admin callers."""
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.tenant import Tenant
from app.models.mcp_server import MCPServer
from app.models.tool import Tool
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


async def _mk_server_and_tool(creator_id: uuid.UUID, tenant_id: uuid.UUID | None = None) -> tuple[uuid.UUID, str]:
    suffix = uuid.uuid4().hex[:6]
    name = f"srv_{suffix}"
    async with async_session() as db:
        srv = MCPServer(
            name=name,
            display_name=name,
            base_url_template="https://x.example",
            tenant_id=tenant_id,
            created_by_user_id=creator_id,
        )
        db.add(srv)
        await db.flush()
        tool = Tool(
            name=f"t_{suffix}",
            display_name="t",
            type="mcp",
            mcp_server_name=name,
            mcp_server_url="https://x.example",
            mcp_server_id=srv.id,
            mcp_tool_name="x",
            tenant_id=tenant_id,
        )
        db.add(tool)
        await db.commit()
        return srv.id, name


async def _put(client: AsyncClient, server_name: str, token: str, url: str = "https://updated.example") -> int:
    resp = await client.put(
        "/api/tools/mcp-server",
        headers={"Authorization": f"Bearer {token}"},
        json={"server_name": server_name, "server_url": url},
    )
    return resp.status_code


async def test_owner_can_update():
    owner_id, owner_token = await _mk_user("member")
    _, name = await _mk_server_and_tool(owner_id)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _put(client, name, owner_token) == 200


async def test_non_owner_member_forbidden():
    owner_id, _ = await _mk_user("member")
    _, name = await _mk_server_and_tool(owner_id)
    _, intruder = await _mk_user("member")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _put(client, name, intruder) == 403


async def test_platform_admin_can_update_any():
    owner_id, _ = await _mk_user("member")
    _, name = await _mk_server_and_tool(owner_id)
    _, pa = await _mk_user("platform_admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _put(client, name, pa) == 200


async def test_same_tenant_org_admin_can_update():
    tid = await _mk_tenant()
    owner_id, _ = await _mk_user("member", tenant_id=tid)
    _, name = await _mk_server_and_tool(owner_id, tenant_id=tid)
    _, orga = await _mk_user("org_admin", tenant_id=tid)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _put(client, name, orga) == 200


async def test_cross_tenant_org_admin_forbidden():
    owner_tid = await _mk_tenant()
    other_tid = await _mk_tenant()
    owner_id, _ = await _mk_user("member", tenant_id=owner_tid)
    _, name = await _mk_server_and_tool(owner_id, tenant_id=owner_tid)
    _, foreign = await _mk_user("org_admin", tenant_id=other_tid)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _put(client, name, foreign) == 403
