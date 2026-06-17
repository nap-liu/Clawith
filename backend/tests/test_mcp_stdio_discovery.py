"""Task 8 tests — test-connection stdio discovery.

Verifies:
- POST /{id}/test-connection on a stdio server routes through
  SandboxMcpHost.ensure_registered + SandboxMcpHubClient.list_tools.
- Returns success=True with tool count in server_info.
- http servers still use MCPClient (no regression).
- Discovered tools are persisted as Tool rows (idempotent).
"""
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
from sqlalchemy import select

from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.mcp_server import MCPServer
from app.models.tool import Tool
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


# ---------------------------------------------------------------------------
# NEW: stdio discovery persists Tool rows (agent-assignable)
# ---------------------------------------------------------------------------

async def _make_stdio_server_and_admin() -> tuple[str, uuid.UUID, str]:
    """Create an admin user + stdio MCPServer; return (token, srv.id, srv.name)."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(
            username=f"tooladm_{suffix}", email=f"tooladm_{suffix}@x.local",
            password_hash="x", is_platform_admin=True,
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id, display_name="ToolAdmin",
            role="platform_admin", is_active=True,
        )
        db.add(user)
        await db.flush()
        srv = MCPServer(
            name=f"persist_{suffix}", display_name="persist",
            base_url_template="",
            headers_template={},
            transport="stdio",
            command_template="npx",
            args_template=["-y", "some-mcp-pkg"],
            env_template={},
        )
        db.add(srv)
        await db.commit()
        await db.refresh(user)
        await db.refresh(srv)
        token = create_access_token(str(user.id), "platform_admin")
        return token, srv.id, srv.name


async def test_stdio_discovery_persists_tool_rows(client):
    """test-connection on a stdio server must create Tool rows for each discovered tool.

    Asserts:
    - 3 Tool rows exist after discovery (type='mcp', source='admin',
      mcp_server_id set, mcp_tool_name matching raw tool name).
    - Running test-connection a second time does NOT create duplicates (still 3 rows).
    """
    token, srv_id, srv_name = await _make_stdio_server_and_admin()

    fake_tools = [
        {"name": "alpha", "description": "Alpha tool", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "beta", "description": "Beta tool", "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}}}},
        {"name": "gamma", "description": "Gamma tool", "inputSchema": {}},
    ]

    class _Settings:
        SANDBOX_API_URL = "http://sandbox:8080"
        SANDBOX_API_KEY = "test-key"

    def _run_discovery():
        return client.post(
            f"/api/admin/mcp-servers/{srv_id}/test-connection",
            headers={"Authorization": f"Bearer {token}"},
        )

    for run_index in range(2):
        with patch("app.api.mcp_servers.SandboxMcpHost") as MockHost, \
             patch("app.api.mcp_servers.SandboxMcpHubClient") as MockHub, \
             patch("app.config.get_settings", return_value=_Settings()):
            mock_host_inst = MagicMock()
            mock_host_inst.ensure_registered = AsyncMock(return_value=f"{srv_name}__disc")
            mock_host_inst.deregister = AsyncMock()
            MockHost.return_value = mock_host_inst

            mock_hub_inst = MagicMock()
            mock_hub_inst.list_tools = AsyncMock(return_value=fake_tools)
            MockHub.return_value = mock_hub_inst

            r = await _run_discovery()

        assert r.status_code == 200, f"run {run_index}: {r.text}"
        body = r.json()
        assert body["success"] is True, f"run {run_index}: {body}"
        assert body["server_info"]["tool_count"] == 3, f"run {run_index}: {body}"

        # Verify Tool rows in DB
        async with async_session() as db:
            result = await db.execute(
                select(Tool).where(Tool.mcp_server_id == srv_id)
            )
            rows = result.scalars().all()

        assert len(rows) == 3, (
            f"run {run_index}: expected 3 Tool rows, got {len(rows)}: "
            f"{[r.mcp_tool_name for r in rows]}"
        )

        raw_names = {r.mcp_tool_name for r in rows}
        assert raw_names == {"alpha", "beta", "gamma"}, f"run {run_index}: {raw_names}"

        for row in rows:
            assert row.type == "mcp", f"run {run_index}: type={row.type}"
            assert row.source == "admin", f"run {run_index}: source={row.source}"
            assert row.mcp_server_id == srv_id, f"run {run_index}: mcp_server_id mismatch"
            assert row.mcp_server_name == srv_name, f"run {run_index}: mcp_server_name={row.mcp_server_name}"
            # Name scheme: mcp_<srv.name>_<raw_tool_name>
            assert row.name == f"mcp_{srv_name}_{row.mcp_tool_name}", (
                f"run {run_index}: unexpected name {row.name!r}"
            )


async def test_stdio_discovery_tool_name_resolvable_by_execute_mcp_tool(client):
    """Tool.name persisted by stdio discovery must match what _execute_mcp_tool
    resolves on the primary lookup path (Tool.name == tool_name, type='mcp').

    This is a static structural check: we confirm the name scheme
    ``mcp_{srv.name}_{raw_tool_name}`` is what gets stored, which is what
    _execute_mcp_tool's primary SELECT (Tool.name == tool_name) expects.
    """
    token, srv_id, srv_name = await _make_stdio_server_and_admin()

    fake_tools = [
        {"name": "do_thing", "description": "Does a thing", "inputSchema": {}},
    ]

    class _Settings:
        SANDBOX_API_URL = "http://sandbox:8080"
        SANDBOX_API_KEY = "test-key"

    with patch("app.api.mcp_servers.SandboxMcpHost") as MockHost, \
         patch("app.api.mcp_servers.SandboxMcpHubClient") as MockHub, \
         patch("app.config.get_settings", return_value=_Settings()):
        mock_host_inst = MagicMock()
        mock_host_inst.ensure_registered = AsyncMock(return_value=f"{srv_name}__disc2")
        mock_host_inst.deregister = AsyncMock()
        MockHost.return_value = mock_host_inst

        mock_hub_inst = MagicMock()
        mock_hub_inst.list_tools = AsyncMock(return_value=fake_tools)
        MockHub.return_value = mock_hub_inst

        r = await client.post(
            f"/api/admin/mcp-servers/{srv_id}/test-connection",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert r.json()["success"] is True

    async with async_session() as db:
        expected_name = f"mcp_{srv_name}_do_thing"
        row = (await db.execute(
            select(Tool).where(Tool.name == expected_name, Tool.type == "mcp")
        )).scalar_one_or_none()

    assert row is not None, f"Tool with name={expected_name!r} not found"
    assert row.mcp_tool_name == "do_thing"
    assert row.mcp_server_id == srv_id
