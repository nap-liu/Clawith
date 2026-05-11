"""PUT /tools/mcp-server bridges to mcp_servers table when prompt/headers passed."""
import uuid
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.tenant import Tenant
from app.models.tool import Tool
from app.models.mcp_server import MCPServer
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


async def _make_admin_with_mcp_tool() -> tuple[str, str, uuid.UUID]:
    """Returns (token, server_name, tool_id) for a fresh test fixture."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(username=f"a_{suffix}", email=f"a_{suffix}@x.local",
                            password_hash="x", is_platform_admin=True)
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="A",
                    role="platform_admin", is_active=True)
        db.add(user)
        await db.flush()
        server_name = f"mcp_test_{suffix}"
        tool = Tool(
            name=f"mcp_{server_name}_search",
            display_name="search",
            type="mcp",
            mcp_server_url=f"https://test-{suffix}.example/mcp",
            mcp_server_name=server_name,
            mcp_tool_name="search",
            tenant_id=user.tenant_id,
        )
        db.add(tool)
        await db.commit()
        await db.refresh(tool)
        return create_access_token(str(user.id), "platform_admin"), server_name, tool.id


async def test_legacy_payload_unchanged(client):
    """Old payload (no prompt/headers) → bridge still creates mcp_servers row but
    only sets URL, prompt/headers stay default."""
    token, server_name, tool_id = await _make_admin_with_mcp_tool()
    r = await client.put(
        "/api/tools/mcp-server",
        json={"server_name": server_name, "server_url": f"https://updated-{server_name}.example", "api_key": None},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["updated"] >= 1
    assert "mcp_server_id" in body  # bridge always returns it now

    # tool.mcp_server_id should now be linked
    async with async_session() as db:
        tool = (await db.execute(select(Tool).where(Tool.id == tool_id))).scalar_one()
        assert tool.mcp_server_id is not None
        srv = (await db.execute(select(MCPServer).where(MCPServer.id == tool.mcp_server_id))).scalar_one()
        # No prompt set (not provided)
        assert srv.system_prompt_block is None


async def test_payload_with_prompt_writes_to_mcp_servers(client):
    token, server_name, tool_id = await _make_admin_with_mcp_tool()
    r = await client.put(
        "/api/tools/mcp-server",
        json={
            "server_name": server_name,
            "server_url": f"https://srv-{server_name}.example",
            "api_key": "topsecret",
            "system_prompt_block": "Use citations.",
            "headers_template": {"X-User": "${user.email}"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    server_id = r.json()["mcp_server_id"]

    async with async_session() as db:
        srv = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one()
        assert srv.system_prompt_block == "Use citations."
        assert srv.headers_template == {"X-User": "${user.email}"}
        assert srv.credential_template == "topsecret"


async def test_idempotent_upsert(client):
    token, server_name, tool_id = await _make_admin_with_mcp_tool()
    # 1st call — creates
    r1 = await client.put(
        "/api/tools/mcp-server",
        json={"server_name": server_name, "server_url": f"https://idem-{server_name}.example",
              "system_prompt_block": "v1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    id1 = r1.json()["mcp_server_id"]

    # 2nd call — same URL, different prompt → updates same row
    r2 = await client.put(
        "/api/tools/mcp-server",
        json={"server_name": server_name, "server_url": f"https://idem-{server_name}.example",
              "system_prompt_block": "v2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    id2 = r2.json()["mcp_server_id"]
    assert id1 == id2

    async with async_session() as db:
        srv = (await db.execute(select(MCPServer).where(MCPServer.id == id1))).scalar_one()
        assert srv.system_prompt_block == "v2"


async def test_prompt_with_user_placeholder_rejected(client):
    """system_prompt_block containing ${user.*} is rejected with 422."""
    token, server_name, _ = await _make_admin_with_mcp_tool()
    r = await client.put(
        "/api/tools/mcp-server",
        json={
            "server_name": server_name,
            "server_url": f"https://val-{server_name}.example",
            "system_prompt_block": "Hi ${user.email}, here are the docs",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422
    assert "user" in r.json()["detail"].lower() or "${user" in r.json()["detail"]


async def test_headers_with_user_placeholder_allowed(client):
    """headers_template CAN contain ${user.*} — that's the runtime resolve target."""
    token, server_name, _ = await _make_admin_with_mcp_tool()
    r = await client.put(
        "/api/tools/mcp-server",
        json={
            "server_name": server_name,
            "server_url": f"https://hdr-{server_name}.example",
            "headers_template": {"X-User-Email": "${user.email}"},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
