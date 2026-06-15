"""Task 7 tests — _execute_mcp_tool stdio routing.

Verifies:
- transport=stdio tools route through SandboxMcpHost + SandboxMcpHubClient
- transport=http tools still route through MCPClient (no stdio host called)
"""
import uuid
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.tenant import Tenant
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tool import Tool, AgentTool

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_stdio_fixture():
    """Insert a stdio MCPServer + Tool + Agent; return (agent_id, user_id, tool_name)."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        tenant = Tenant(name=f"T_{suffix}", slug=f"t-{suffix}")
        db.add(tenant)
        await db.flush()

        identity = Identity(
            username=f"u_{suffix}", email=f"u-{suffix}@x.local", password_hash="x"
        )
        db.add(identity)
        await db.flush()

        user = User(
            identity_id=identity.id, display_name="U",
            role="member", is_active=True, tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()

        agent = Agent(name=f"A_{suffix}", creator_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.flush()

        srv = MCPServer(
            name=f"yx_{suffix}", display_name="yx",
            base_url_template="",
            headers_template={},
            transport="stdio",
            command_template="npx",
            args_template=["-y", "alibabacloud-devops-mcp-server"],
            env_template={"YUNXIAO_ACCESS_TOKEN": "tok-literal"},
        )
        db.add(srv)
        await db.flush()

        tool = Tool(
            name=f"mcp_yx_{suffix}",
            display_name="yx",
            type="mcp",
            mcp_server_url="",
            mcp_server_id=srv.id,
            mcp_tool_name="get_current_user",
        )
        db.add(tool)
        await db.flush()

        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()

        return agent.id, user.id, f"mcp_yx_{suffix}"


class _SettingsWithSandbox:
    """Settings stub with a non-empty SANDBOX_API_URL for tests that exercise the stdio path."""
    SANDBOX_API_URL = "http://sandbox:8080"
    SANDBOX_API_KEY = "test-key"
    DEBUG = False


async def test_stdio_routes_through_sandbox():
    """stdio transport: ensure_registered called, call_tool result returned."""
    agent_id, user_id, tool_name = await _make_stdio_fixture()

    with patch("app.services.agent_tools.SandboxMcpHost") as MockHost, \
         patch("app.services.agent_tools.SandboxMcpHubClient") as MockHub, \
         patch("app.services.agent_tools.get_settings", return_value=_SettingsWithSandbox()):
        mock_host_inst = MagicMock()
        mock_host_inst.ensure_registered = AsyncMock(return_value="yx__abc123456789")
        MockHost.return_value = mock_host_inst

        mock_hub_inst = MagicMock()
        mock_hub_inst.call_tool = AsyncMock(return_value="CALLED")
        MockHub.return_value = mock_hub_inst

        from app.services.agent_tools import _execute_mcp_tool

        result = await _execute_mcp_tool(
            tool_name, {}, agent_id=agent_id, user_id=user_id, session_id="s1"
        )

    assert result == "CALLED"
    mock_host_inst.ensure_registered.assert_awaited_once()
    # call_tool should be called with the hub entry name, the MCP tool name, and args
    mock_hub_inst.call_tool.assert_awaited_once_with("yx__abc123456789", "get_current_user", {})


async def test_stdio_ensure_registered_receives_rendered_cfg():
    """ensure_registered receives rendered command/args/env (no placeholders left)."""
    agent_id, user_id, tool_name = await _make_stdio_fixture()

    with patch("app.services.agent_tools.SandboxMcpHost") as MockHost, \
         patch("app.services.agent_tools.SandboxMcpHubClient") as MockHub, \
         patch("app.services.agent_tools.get_settings", return_value=_SettingsWithSandbox()):
        mock_host_inst = MagicMock()
        mock_host_inst.ensure_registered = AsyncMock(return_value="yx__entry")
        MockHost.return_value = mock_host_inst

        mock_hub_inst = MagicMock()
        mock_hub_inst.call_tool = AsyncMock(return_value="OK")
        MockHub.return_value = mock_hub_inst

        from app.services.agent_tools import _execute_mcp_tool

        await _execute_mcp_tool(
            tool_name, {"param": "val"}, agent_id=agent_id, user_id=user_id
        )

    # Verify ensure_registered was called with (server_name, str(agent_id), cfg_dict)
    call_args = mock_host_inst.ensure_registered.call_args
    assert call_args is not None
    positional = call_args[0]
    assert positional[1] == str(agent_id)
    cfg_arg = positional[2]
    assert cfg_arg["command"] == "npx"
    assert cfg_arg["args"] == ["-y", "alibabacloud-devops-mcp-server"]
    assert cfg_arg["env"]["YUNXIAO_ACCESS_TOKEN"] == "tok-literal"


async def _make_http_fixture():
    """Insert an http MCPServer + Tool + Agent."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(
            username=f"u_{suffix}", email=f"u-{suffix}@x.local", password_hash="x"
        )
        db.add(identity)
        await db.flush()

        user = User(
            identity_id=identity.id, display_name="U",
            role="member", is_active=True,
        )
        db.add(user)
        await db.flush()

        agent = Agent(name=f"A_{suffix}", creator_id=user.id)
        db.add(agent)
        await db.flush()

        srv = MCPServer(
            name=f"http_{suffix}", display_name="h",
            base_url_template=f"https://http-{suffix}.example/mcp",
            headers_template={},
            transport="http",
        )
        db.add(srv)
        await db.flush()

        tool = Tool(
            name=f"mcp_http_{suffix}",
            display_name="h",
            type="mcp",
            mcp_server_url=f"https://http-{suffix}.example/mcp",
            mcp_server_id=srv.id,
            mcp_tool_name="search",
        )
        db.add(tool)
        await db.flush()

        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()

        return agent.id, user.id, f"mcp_http_{suffix}"


async def test_http_server_does_not_call_stdio_host():
    """http transport: MCPClient is used; SandboxMcpHost is NOT called."""
    agent_id, user_id, tool_name = await _make_http_fixture()

    with patch("app.services.agent_tools.SandboxMcpHost") as MockHost, \
         patch("app.services.agent_tools.SandboxMcpHubClient") as MockHub, \
         patch("app.services.mcp_client.MCPClient") as MockMCPClient:
        mock_host_inst = MagicMock()
        mock_host_inst.ensure_registered = AsyncMock(return_value="should-not-be-called")
        MockHost.return_value = mock_host_inst

        mock_mcp_inst = AsyncMock()
        mock_mcp_inst.call_tool = AsyncMock(return_value="HTTP-OK")
        MockMCPClient.return_value = mock_mcp_inst

        from app.services.agent_tools import _execute_mcp_tool

        result = await _execute_mcp_tool(
            tool_name, {}, agent_id=agent_id, user_id=user_id
        )

    assert result == "HTTP-OK"
    # stdio host must NOT have been called for an http server
    mock_host_inst.ensure_registered.assert_not_awaited()
    # MCPClient must have been used
    MockMCPClient.assert_called_once()
    # SandboxMcpHubClient must NOT have been instantiated
    MockHub.assert_not_called()


# ---------------------------------------------------------------------------
# FIX 5: Guard empty SANDBOX_API_URL in stdio branch of _execute_mcp_tool
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Minimal settings stub with empty SANDBOX_API_URL."""
    SANDBOX_API_URL = ""
    SANDBOX_API_KEY = None
    DEBUG = False


async def test_stdio_returns_clear_error_when_sandbox_url_empty():
    """_execute_mcp_tool with stdio transport and no SANDBOX_API_URL must return
    a clear error string without attempting to connect.

    FAILS before FIX 5 because the guard does not exist."""
    agent_id, user_id, tool_name = await _make_stdio_fixture()

    from app.services.agent_tools import _execute_mcp_tool

    with patch("app.services.agent_tools.get_settings", return_value=_FakeSettings()):
        result = await _execute_mcp_tool(
            tool_name, {}, agent_id=agent_id, user_id=user_id, session_id="s1"
        )

    assert "SANDBOX_API_URL" in result, (
        f"Expected SANDBOX_API_URL in error, got: {result!r}"
    )
    assert "stdio" in result.lower() or "MCP" in result, (
        f"Expected stdio/MCP context in error, got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Per-agent working-path isolation: ensure_registered receives cwd
# ---------------------------------------------------------------------------


async def test_stdio_ensure_registered_receives_agent_workspace_cwd():
    """ensure_registered must be called with cwd = the agent's workspace path.

    The expected path is /data/agents/{agent_id} — matching the pattern used by
    code execution (_execute_code). Patch ensure_workspace to avoid actual FS
    access and inspect the cwd kwarg forwarded to ensure_registered.
    """
    from pathlib import Path
    from unittest.mock import AsyncMock as _AsyncMock

    agent_id, user_id, tool_name = await _make_stdio_fixture()
    expected_cwd = f"/data/agents/{agent_id}"

    mock_host_inst = MagicMock()
    mock_host_inst.ensure_registered = AsyncMock(return_value="yx__cwd_entry")
    mock_hub_inst = MagicMock()
    mock_hub_inst.call_tool = AsyncMock(return_value="CWD-OK")

    with patch("app.services.agent_tools.SandboxMcpHost") as MockHost, \
         patch("app.services.agent_tools.SandboxMcpHubClient") as MockHub, \
         patch("app.services.agent_tools.get_settings", return_value=_SettingsWithSandbox()), \
         patch("app.services.agent_tools.ensure_workspace", new=_AsyncMock(return_value=Path(expected_cwd))):
        MockHost.return_value = mock_host_inst
        MockHub.return_value = mock_hub_inst

        from app.services.agent_tools import _execute_mcp_tool

        result = await _execute_mcp_tool(
            tool_name, {}, agent_id=agent_id, user_id=user_id, session_id="s1"
        )

    assert result == "CWD-OK"
    mock_host_inst.ensure_registered.assert_awaited_once()
    call_kwargs = mock_host_inst.ensure_registered.call_args
    # cwd should be passed as a keyword argument
    assert call_kwargs.kwargs.get("cwd") == expected_cwd, (
        f"Expected cwd={expected_cwd!r}, got kwargs={call_kwargs.kwargs}"
    )
