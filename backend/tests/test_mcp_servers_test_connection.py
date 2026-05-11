"""POST /api/admin/mcp-servers/{id}/test-connection: triggers an
``initialize`` handshake against the configured MCP server, captures
the resulting ``instructions`` and updates the row.
"""
import uuid
from unittest.mock import AsyncMock, patch
import pytest
import httpx
from sqlalchemy import select
from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.mcp_server import MCPServer
from app.core.security import create_access_token

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_admin() -> str:
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(username=f"a_{suffix}", email=f"a_{suffix}@x.local",
                            password_hash="x", is_platform_admin=True)
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="A",
                    role="platform_admin", is_active=True)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return create_access_token(str(user.id), "platform_admin")


@pytest.fixture
async def client():
    from app.main import app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_test_connection_captures_instructions(client):
    token = await _make_admin()
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        srv = MCPServer(
            name=f"tc_{suffix}", display_name="tc",
            base_url_template=f"https://tc-{suffix}.example",
            headers_template={},
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        srv_id = srv.id

    # Mock MCPClient list_tools — capture the instructions response
    fake_client = AsyncMock()
    fake_client.server_instructions = "TEST INSTRUCTIONS"
    fake_client.server_info = {"name": "TestServer", "version": "1.0"}

    async def fake_list_tools():
        return []

    fake_client.list_tools = fake_list_tools

    with patch("app.api.mcp_servers.MCPClient", return_value=fake_client):
        r = await client.post(
            f"/api/admin/mcp-servers/{srv_id}/test-connection",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["instructions"] == "TEST INSTRUCTIONS"
    assert body["server_info"]["name"] == "TestServer"

    # DB row should be updated
    async with async_session() as db:
        srv2 = (await db.execute(select(MCPServer).where(MCPServer.id == srv_id))).scalar_one()
        assert srv2.instructions == "TEST INSTRUCTIONS"
        assert srv2.instructions_captured_at is not None


async def test_test_connection_handles_failure(client):
    token = await _make_admin()
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        srv = MCPServer(
            name=f"tcf_{suffix}", display_name="tcf",
            base_url_template=f"https://tcf-{suffix}.example",
            headers_template={},
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        srv_id = srv.id

    fake_client = AsyncMock()

    async def fake_list_tools_raises():
        raise RuntimeError("connection refused")

    fake_client.list_tools = fake_list_tools_raises

    with patch("app.api.mcp_servers.MCPClient", return_value=fake_client):
        r = await client.post(
            f"/api/admin/mcp-servers/{srv_id}/test-connection",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert "connection refused" in body["error"]
