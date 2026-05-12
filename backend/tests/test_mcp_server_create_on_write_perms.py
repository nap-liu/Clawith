"""When PUT /api/tools/mcp-server hits a server_name with NO existing mcp_servers
row (legacy/migration state where upsert_mcp_server_from_tools would create one),
non-admin members must NOT be allowed to silently create the new row.
"""
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


async def _mk_orphan_tool(tenant_id: uuid.UUID | None = None) -> str:
    """Create a Tool row pointing to a server_name that has NO mcp_servers row.
    This simulates the legacy/migration state where PUT /tools/mcp-server would
    auto-create the missing mcp_servers row.
    """
    suffix = uuid.uuid4().hex[:6]
    name = f"orphan_srv_{suffix}"
    async with async_session() as db:
        tool = Tool(
            name=f"t_{suffix}",
            display_name="t",
            type="mcp",
            mcp_server_name=name,
            mcp_server_url="https://orphan.example",
            mcp_tool_name="x",
            tenant_id=tenant_id,
        )
        db.add(tool)
        await db.commit()
    return name


async def _put(
    client: AsyncClient,
    server_name: str,
    token: str,
    tenant_id: uuid.UUID | None = None,
) -> int:
    body: dict = {"server_name": server_name, "server_url": "https://hijacked.example"}
    if tenant_id is not None:
        body["tenant_id"] = str(tenant_id)
    resp = await client.put(
        "/api/tools/mcp-server",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )
    return resp.status_code


async def test_member_cannot_silently_create_mcp_server_row():
    """Member with an orphan-tool 不能通过 PUT 补建 mcp_servers row."""
    tid = await _mk_tenant()
    name = await _mk_orphan_tool(tenant_id=tid)
    _, member_token = await _mk_user("member", tenant_id=tid)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _put(client, name, member_token) == 403

    # Verify no mcp_servers row was created as a side effect
    async with async_session() as db:
        from sqlalchemy import select
        srv = (await db.execute(select(MCPServer).where(MCPServer.name == name))).scalar_one_or_none()
        assert srv is None, "PUT should NOT have created the row"


async def test_org_admin_can_create_in_own_tenant():
    """同 tenant 的 org_admin 可以补建 server row + 拥有它."""
    tid = await _mk_tenant()
    name = await _mk_orphan_tool(tenant_id=tid)
    orga_id, orga_token = await _mk_user("org_admin", tenant_id=tid)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _put(client, name, orga_token) == 200

    # Verify mcp_servers row was created + owned by the org_admin
    async with async_session() as db:
        from sqlalchemy import select
        srv = (await db.execute(
            select(MCPServer).where(MCPServer.tenant_id == tid).order_by(MCPServer.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        assert srv is not None, "PUT should have created the row"
        assert srv.created_by_user_id == orga_id, \
            f"creator should be the org_admin who triggered create-on-write, got {srv.created_by_user_id}"


async def test_platform_admin_can_create_anywhere():
    """platform_admin 可以补建任意 tenant 的 server row（需明确传 tenant_id）."""
    tid = await _mk_tenant()
    name = await _mk_orphan_tool(tenant_id=tid)
    _, pa_token = await _mk_user("platform_admin")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert await _put(client, name, pa_token, tenant_id=tid) == 200
