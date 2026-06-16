"""Tests for agent self-install stdio MCP feature."""
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.tenant import Tenant
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tool import Tool, AgentTool
from sqlalchemy import select

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_agent() -> tuple[uuid.UUID, uuid.UUID, str]:
    """Create tenant + identity + user + agent. Return (agent_id, tenant_id, agent_id_str)."""
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
        await db.commit()
        await db.refresh(agent)
        await db.refresh(tenant)
        return agent.id, tenant.id, str(agent.id)


# ── Task 1: get_or_create_agent_stdio_server ─────────────────────────────────

async def test_agent_stdio_server_name_is_deterministic_and_agent_scoped():
    agent_id, tenant_id, _ = await _make_agent()
    cfg = {"command": "npx", "args": ["-y", "alibabacloud-devops-mcp-server"], "env": {"T": "x"}}

    from app.services.mcp_server_service import get_or_create_agent_stdio_server

    async with async_session() as db:
        srv1 = await get_or_create_agent_stdio_server(db, agent_id, tenant_id, cfg)
        srv1_id = srv1.id
        srv1_name = srv1.name
        await db.commit()

    async with async_session() as db:
        srv2 = await get_or_create_agent_stdio_server(db, agent_id, tenant_id, cfg)
        srv2_id = srv2.id
        await db.commit()

    assert srv1_id == srv2_id                       # idempotent
    assert srv1.transport == "stdio"
    agent8 = str(agent_id).replace("-", "")[:8]
    assert srv1_name.endswith(f"-a{agent8}")        # 带 agent 短码隔离


async def test_two_agents_same_pkg_get_different_servers():
    agent1_id, tenant_id, _ = await _make_agent()
    agent2_id, _, _ = await _make_agent()
    cfg = {"command": "npx", "args": ["-y", "some-pkg"], "env": {}}

    from app.services.mcp_server_service import get_or_create_agent_stdio_server

    async with async_session() as db:
        srv1 = await get_or_create_agent_stdio_server(db, agent1_id, tenant_id, cfg)
        srv2 = await get_or_create_agent_stdio_server(db, agent2_id, tenant_id, cfg)
        await db.commit()
        srv1_name = srv1.name
        srv2_name = srv2.name
        srv1_id = srv1.id
        srv2_id = srv2.id

    assert srv1_id != srv2_id
    assert srv1_name != srv2_name


# ── Task 2: import_mcp_stdio_direct ──────────────────────────────────────────

class _SettingsWithSandbox:
    SANDBOX_API_URL = "http://sandbox:8080"
    SANDBOX_API_KEY = "test-key"


async def test_import_mcp_stdio_direct_discovers_and_assigns():
    agent_id, _, _ = await _make_agent()

    with patch("app.services.resource_discovery.get_settings", return_value=_SettingsWithSandbox()), \
         patch("app.services.resource_discovery.SandboxMcpHost") as MockHost, \
         patch("app.services.resource_discovery.SandboxMcpHubClient") as MockHub, \
         patch("app.services.resource_discovery.ensure_workspace") as mock_ws:
        mock_host_inst = MagicMock()
        mock_host_inst.ensure_registered = AsyncMock(return_value="entry__abc")
        mock_host_inst.deregister = AsyncMock(return_value=None)
        MockHost.return_value = mock_host_inst

        mock_hub_inst = MagicMock()
        mock_hub_inst.list_tools = AsyncMock(return_value=[
            {"name": "list_repositories", "description": "list repos", "inputSchema": {"type": "object"}}
        ])
        MockHub.return_value = mock_hub_inst

        from pathlib import Path
        mock_ws.return_value = Path("/tmp/fake-ws")

        from app.services.resource_discovery import import_mcp_stdio_direct
        parsed = {"transport": "stdio", "command": "npx",
                  "args": ["-y", "alibabacloud-devops-mcp-server"], "env": {"YUNXIAO_ACCESS_TOKEN": "tok"}}
        result = await import_mcp_stdio_direct(agent_id, parsed)

    assert "list_repositories" in result or "✅" in result

    # Find the tool scoped to this agent's server (name ends with -a{agent8})
    agent8 = str(agent_id).replace("-", "")[:8]
    async with async_session() as db:
        rows = (await db.execute(
            select(Tool).where(
                Tool.mcp_tool_name == "list_repositories",
                Tool.mcp_server_name.like(f"%-a{agent8}"),
            )
        )).scalars().all()
        assert len(rows) == 1, f"Expected exactly 1 tool row for this agent, got {len(rows)}"
        t = rows[0]
        at = (await db.execute(select(AgentTool).where(
            AgentTool.agent_id == agent_id, AgentTool.tool_id == t.id))).scalar_one_or_none()
        assert at is not None
        assert at.source == "user_installed"


async def test_import_mcp_stdio_direct_no_sandbox_url():
    agent_id, _, _ = await _make_agent()

    class _NoSandbox:
        SANDBOX_API_URL = ""
        SANDBOX_API_KEY = None

    with patch("app.services.resource_discovery.get_settings", return_value=_NoSandbox()):
        from app.services.resource_discovery import import_mcp_stdio_direct
        result = await import_mcp_stdio_direct(agent_id, {"transport": "stdio", "command": "npx", "args": [], "env": {}})

    assert "SANDBOX_API_URL" in result or "❌" in result


async def test_import_mcp_stdio_direct_registration_error():
    agent_id, _, _ = await _make_agent()

    with patch("app.services.resource_discovery.get_settings", return_value=_SettingsWithSandbox()), \
         patch("app.services.resource_discovery.SandboxMcpHost") as MockHost, \
         patch("app.services.resource_discovery.SandboxMcpHubClient"), \
         patch("app.services.resource_discovery.ensure_workspace") as mock_ws:
        mock_host_inst = MagicMock()
        mock_host_inst.ensure_registered = AsyncMock(side_effect=Exception("sandbox unreachable"))
        MockHost.return_value = mock_host_inst

        from pathlib import Path
        mock_ws.return_value = Path("/tmp/fake-ws")

        from app.services.resource_discovery import import_mcp_stdio_direct
        result = await import_mcp_stdio_direct(agent_id, {"transport": "stdio", "command": "npx", "args": [], "env": {}})

    assert "❌" in result
    assert "sandbox unreachable" in result


async def test_import_mcp_stdio_direct_list_tools_error():
    agent_id, _, _ = await _make_agent()

    with patch("app.services.resource_discovery.get_settings", return_value=_SettingsWithSandbox()), \
         patch("app.services.resource_discovery.SandboxMcpHost") as MockHost, \
         patch("app.services.resource_discovery.SandboxMcpHubClient") as MockHub, \
         patch("app.services.resource_discovery.ensure_workspace") as mock_ws:
        mock_host_inst = MagicMock()
        mock_host_inst.ensure_registered = AsyncMock(return_value="entry__abc")
        mock_host_inst.deregister = AsyncMock()
        MockHost.return_value = mock_host_inst

        mock_hub_inst = MagicMock()
        mock_hub_inst.list_tools = AsyncMock(side_effect=Exception("npx failed"))
        MockHub.return_value = mock_hub_inst

        from pathlib import Path
        mock_ws.return_value = Path("/tmp/fake-ws")

        from app.services.resource_discovery import import_mcp_stdio_direct
        result = await import_mcp_stdio_direct(agent_id, {"transport": "stdio", "command": "npx", "args": [], "env": {}})

    assert "❌" in result
    assert "npx failed" in result


async def test_import_mcp_stdio_direct_zero_tools():
    agent_id, _, _ = await _make_agent()

    with patch("app.services.resource_discovery.get_settings", return_value=_SettingsWithSandbox()), \
         patch("app.services.resource_discovery.SandboxMcpHost") as MockHost, \
         patch("app.services.resource_discovery.SandboxMcpHubClient") as MockHub, \
         patch("app.services.resource_discovery.ensure_workspace") as mock_ws:
        mock_host_inst = MagicMock()
        mock_host_inst.ensure_registered = AsyncMock(return_value="entry__abc")
        mock_host_inst.deregister = AsyncMock()
        MockHost.return_value = mock_host_inst

        mock_hub_inst = MagicMock()
        mock_hub_inst.list_tools = AsyncMock(return_value=[])
        MockHub.return_value = mock_hub_inst

        from pathlib import Path
        mock_ws.return_value = Path("/tmp/fake-ws")

        from app.services.resource_discovery import import_mcp_stdio_direct
        result = await import_mcp_stdio_direct(agent_id, {"transport": "stdio", "command": "npx", "args": [], "env": {}})

    assert "⚠️" in result or "0" in result


# ── Task 4: idempotency (reinstall same agent same pkg) ──────────────────────

async def test_import_mcp_stdio_direct_idempotent():
    """Same agent installing same package twice → no duplicate Tool rows."""
    agent_id, _, _ = await _make_agent()
    fake_tools = [
        {"name": "do_thing", "description": "does a thing", "inputSchema": {}},
    ]

    async def _do_install():
        with patch("app.services.resource_discovery.get_settings", return_value=_SettingsWithSandbox()), \
             patch("app.services.resource_discovery.SandboxMcpHost") as MockHost, \
             patch("app.services.resource_discovery.SandboxMcpHubClient") as MockHub, \
             patch("app.services.resource_discovery.ensure_workspace") as mock_ws:
            mock_host_inst = MagicMock()
            mock_host_inst.ensure_registered = AsyncMock(return_value="entry__abc")
            mock_host_inst.deregister = AsyncMock()
            MockHost.return_value = mock_host_inst

            mock_hub_inst = MagicMock()
            mock_hub_inst.list_tools = AsyncMock(return_value=fake_tools)
            MockHub.return_value = mock_hub_inst

            from pathlib import Path
            mock_ws.return_value = Path("/tmp/fake-ws")

            from app.services.resource_discovery import import_mcp_stdio_direct
            return await import_mcp_stdio_direct(
                agent_id,
                {"transport": "stdio", "command": "npx", "args": ["-y", "same-pkg"], "env": {}}
            )

    result1 = await _do_install()
    result2 = await _do_install()

    assert "✅" in result1
    assert "✅" in result2

    # Tool rows should not be duplicated
    async with async_session() as db:
        rows = (await db.execute(select(Tool).where(Tool.mcp_tool_name == "do_thing"))).scalars().all()
        # Filter to our agent's server (same-pkg for this agent)
        agent8 = str(agent_id).replace("-", "")[:8]
        agent_rows = [r for r in rows if r.mcp_server_name and r.mcp_server_name.endswith(f"-a{agent8}")]
        assert len(agent_rows) == 1, f"Expected 1 Tool row, got {len(agent_rows)}"

        # AgentTool also not duplicated
        if agent_rows:
            at_rows = (await db.execute(select(AgentTool).where(
                AgentTool.agent_id == agent_id,
                AgentTool.tool_id == agent_rows[0].id
            ))).scalars().all()
            assert len(at_rows) == 1, f"Expected 1 AgentTool row, got {len(at_rows)}"


# ── Task 5: two agents get different servers, no Tool.name collision ──────────

async def test_two_agents_tool_names_no_collision():
    """Two agents installing same package get different server names → different Tool.name."""
    agent1_id, _, _ = await _make_agent()
    agent2_id, _, _ = await _make_agent()
    fake_tools = [{"name": "create_pipeline", "description": "cp", "inputSchema": {}}]

    async def _install_for(agent_id):
        with patch("app.services.resource_discovery.get_settings", return_value=_SettingsWithSandbox()), \
             patch("app.services.resource_discovery.SandboxMcpHost") as MockHost, \
             patch("app.services.resource_discovery.SandboxMcpHubClient") as MockHub, \
             patch("app.services.resource_discovery.ensure_workspace") as mock_ws:
            mock_host_inst = MagicMock()
            mock_host_inst.ensure_registered = AsyncMock(return_value=f"entry__{str(agent_id)[:6]}")
            mock_host_inst.deregister = AsyncMock()
            MockHost.return_value = mock_host_inst

            mock_hub_inst = MagicMock()
            mock_hub_inst.list_tools = AsyncMock(return_value=fake_tools)
            MockHub.return_value = mock_hub_inst

            from pathlib import Path
            mock_ws.return_value = Path("/tmp/fake-ws")

            from app.services.resource_discovery import import_mcp_stdio_direct
            return await import_mcp_stdio_direct(
                agent_id,
                {"transport": "stdio", "command": "npx", "args": ["-y", "collision-test-pkg"], "env": {}}
            )

    r1 = await _install_for(agent1_id)
    r2 = await _install_for(agent2_id)

    assert "✅" in r1
    assert "✅" in r2

    # Both agents should have their own Tool rows with different names
    async with async_session() as db:
        at1_rows = (await db.execute(select(AgentTool).where(AgentTool.agent_id == agent1_id))).scalars().all()
        at2_rows = (await db.execute(select(AgentTool).where(AgentTool.agent_id == agent2_id))).scalars().all()
        assert len(at1_rows) >= 1
        assert len(at2_rows) >= 1

        # Get tool names for each agent
        tool_ids_1 = {r.tool_id for r in at1_rows}
        tool_ids_2 = {r.tool_id for r in at2_rows}

        # Their tool IDs should be different (different servers)
        tools1 = (await db.execute(select(Tool).where(Tool.id.in_(tool_ids_1), Tool.type == "mcp"))).scalars().all()
        tools2 = (await db.execute(select(Tool).where(Tool.id.in_(tool_ids_2), Tool.type == "mcp"))).scalars().all()
        names1 = {t.name for t in tools1 if t.mcp_tool_name == "create_pipeline"}
        names2 = {t.name for t in tools2 if t.mcp_tool_name == "create_pipeline"}

        if names1 and names2:
            assert names1.isdisjoint(names2), f"Tool name collision: {names1 & names2}"
