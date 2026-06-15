"""Task 8 tests — test-connection stdio discovery.

Verifies:
- POST /{id}/test-connection on a stdio server routes through
  SandboxMcpHost.ensure_registered + SandboxMcpHubClient.list_tools.
- Returns success=True with tool count in server_info.
- http servers still use MCPClient (no regression).
"""
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

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


async def _make_admin_token() -> str:
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(
            username=f"adm_{suffix}", email=f"adm_{suffix}@x.local",
            password_hash="x", is_platform_admin=True,
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id, display_name="Admin",
            role="platform_admin", is_active=True,
        )
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


async def test_stdio_discovery_uses_hub(client):
    """test-connection on stdio server calls ensure_registered + list_tools."""
    token = await _make_admin_token()
    suffix = uuid.uuid4().hex[:6]

    async with async_session() as db:
        srv = MCPServer(
            name=f"yx_{suffix}", display_name="yx",
            base_url_template="",
            headers_template={},
            transport="stdio",
            command_template="npx",
            args_template=["-y", "alibabacloud-devops-mcp-server"],
            env_template={"YUNXIAO_ACCESS_TOKEN": "${agent.yunxiao_token}"},
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        srv_id = srv.id

    fake_tools = [
        {"name": "get_current_user", "description": "Get user", "inputSchema": {}},
        {"name": "list_projects", "description": "List projects", "inputSchema": {}},
    ]

    class _Settings:
        SANDBOX_API_URL = "http://sandbox:8080"
        SANDBOX_API_KEY = "test-key"

    with patch("app.api.mcp_servers.SandboxMcpHost") as MockHost, \
         patch("app.api.mcp_servers.SandboxMcpHubClient") as MockHub, \
         patch("app.config.get_settings", return_value=_Settings()):
        mock_host_inst = MagicMock()
        mock_host_inst.ensure_registered = AsyncMock(return_value=f"yx_{suffix}__abc123456")
        mock_host_inst.deregister = AsyncMock(return_value=None)
        MockHost.return_value = mock_host_inst

        mock_hub_inst = MagicMock()
        mock_hub_inst.list_tools = AsyncMock(return_value=fake_tools)
        MockHub.return_value = mock_hub_inst

        r = await client.post(
            f"/api/admin/mcp-servers/{srv_id}/test-connection",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["server_info"]["transport"] == "stdio"
    assert body["server_info"]["tool_count"] == 2
    assert body["server_info"]["hub_entry"] == f"yx_{suffix}__abc123456"

    # ensure_registered called with __discovery__ scope
    call_args = mock_host_inst.ensure_registered.call_args[0]
    assert call_args[1] == "__discovery__"

    # list_tools called with the hub entry
    mock_hub_inst.list_tools.assert_awaited_once_with(f"yx_{suffix}__abc123456")

    # MCPClient must NOT have been called
    # (we patch at module level — verify by checking hub was used)
    assert mock_host_inst.ensure_registered.await_count == 1


async def test_stdio_discovery_error_returns_failure(client):
    """When hub registration fails, test-connection returns success=False."""
    token = await _make_admin_token()
    suffix = uuid.uuid4().hex[:6]

    async with async_session() as db:
        srv = MCPServer(
            name=f"yxe_{suffix}", display_name="yxe",
            base_url_template="",
            headers_template={},
            transport="stdio",
            command_template="npx",
            args_template=["-y", "some-pkg"],
            env_template={},
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        srv_id = srv.id

    class _Settings:
        SANDBOX_API_URL = "http://sandbox:8080"
        SANDBOX_API_KEY = None

    with patch("app.api.mcp_servers.SandboxMcpHost") as MockHost, \
         patch("app.api.mcp_servers.SandboxMcpHubClient"), \
         patch("app.config.get_settings", return_value=_Settings()):
        mock_host_inst = MagicMock()
        mock_host_inst.ensure_registered = AsyncMock(
            side_effect=Exception("sandbox unreachable")
        )
        MockHost.return_value = mock_host_inst

        r = await client.post(
            f"/api/admin/mcp-servers/{srv_id}/test-connection",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False
    assert "sandbox unreachable" in body["error"]


async def test_http_server_still_uses_mcp_client(client):
    """http transport: MCPClient path unchanged after stdio branch added."""
    token = await _make_admin_token()
    suffix = uuid.uuid4().hex[:6]

    async with async_session() as db:
        srv = MCPServer(
            name=f"http_{suffix}", display_name="h",
            base_url_template=f"https://http-{suffix}.example/mcp",
            headers_template={},
            transport="http",
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        srv_id = srv.id

    fake_client = AsyncMock()
    fake_client.server_instructions = "HTTP INSTRUCTIONS"
    fake_client.server_info = {"name": "HttpServer", "version": "1.0"}
    fake_client.list_tools = AsyncMock(return_value=[])

    with patch("app.api.mcp_servers.SandboxMcpHost") as MockHost, \
         patch("app.api.mcp_servers.MCPClient", return_value=fake_client):
        mock_host_inst = MagicMock()
        mock_host_inst.ensure_registered = AsyncMock()
        MockHost.return_value = mock_host_inst

        r = await client.post(
            f"/api/admin/mcp-servers/{srv_id}/test-connection",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["instructions"] == "HTTP INSTRUCTIONS"

    # stdio host must NOT have been called for http transport
    mock_host_inst.ensure_registered.assert_not_awaited()


# ---------------------------------------------------------------------------
# FIX 5: test-connection returns clear error when SANDBOX_API_URL is empty
# ---------------------------------------------------------------------------

async def test_stdio_test_connection_fails_without_sandbox_url(client):
    """test-connection on stdio server with no SANDBOX_API_URL must return
    success=False and a clear error mentioning SANDBOX_API_URL.

    FAILS before FIX 5 because the guard does not exist."""
    token = await _make_admin_token()
    suffix = uuid.uuid4().hex[:6]

    async with async_session() as db:
        srv = MCPServer(
            name=f"nourl_{suffix}", display_name="nourl",
            base_url_template="",
            headers_template={},
            transport="stdio",
            command_template="npx",
            args_template=["-y", "some-pkg"],
            env_template={},
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        srv_id = srv.id

    class _EmptySettings:
        SANDBOX_API_URL = ""
        SANDBOX_API_KEY = None

    with patch("app.config.get_settings", return_value=_EmptySettings()):
        r = await client.post(
            f"/api/admin/mcp-servers/{srv_id}/test-connection",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is False, f"Expected success=False, got: {body}"
    assert "SANDBOX_API_URL" in (body.get("error") or ""), (
        f"Expected SANDBOX_API_URL in error message, got: {body}"
    )


# ---------------------------------------------------------------------------
# FIX 4: test-connection cleans up hub entry after list_tools (deregister)
# ---------------------------------------------------------------------------

async def test_stdio_discovery_deregisters_after_list_tools(client):
    """test-connection must call host.deregister(entry) after list_tools completes.

    FAILS before FIX 4 because deregister call is not in the stdio branch."""
    token = await _make_admin_token()
    suffix = uuid.uuid4().hex[:6]

    async with async_session() as db:
        srv = MCPServer(
            name=f"dereg_{suffix}", display_name="dereg",
            base_url_template="",
            headers_template={},
            transport="stdio",
            command_template="npx",
            args_template=["-y", "alibabacloud-devops-mcp-server"],
            env_template={},
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        srv_id = srv.id

    fake_tools = [{"name": "t", "description": "T", "inputSchema": {}}]

    class _Settings:
        SANDBOX_API_URL = "http://sandbox:8080"
        SANDBOX_API_KEY = None

    with patch("app.api.mcp_servers.SandboxMcpHost") as MockHost, \
         patch("app.api.mcp_servers.SandboxMcpHubClient") as MockHub, \
         patch("app.config.get_settings", return_value=_Settings()):
        mock_host_inst = MagicMock()
        mock_host_inst.ensure_registered = AsyncMock(return_value=f"dereg_{suffix}__abc123456")
        mock_host_inst.deregister = AsyncMock(return_value=None)
        MockHost.return_value = mock_host_inst

        mock_hub_inst = MagicMock()
        mock_hub_inst.list_tools = AsyncMock(return_value=fake_tools)
        MockHub.return_value = mock_hub_inst

        r = await client.post(
            f"/api/admin/mcp-servers/{srv_id}/test-connection",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True

    # deregister must have been called with the hub entry name
    mock_host_inst.deregister.assert_awaited_once_with(f"dereg_{suffix}__abc123456")
