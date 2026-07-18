"""Direct MCP imports are atomic and bridge discovered tools to mcp_servers."""
import uuid
import pytest
from sqlalchemy import select
from unittest.mock import AsyncMock, patch

from app.database import async_session, engine
from app.models.tool import Tool
from app.models.mcp_server import MCPServer, MCPServerOverride

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_agent(suffix: str):
    from app.main import app  # noqa: F401 — ensure all models are registered
    from app.models.user import User, Identity
    from app.models.agent import Agent

    async with async_session() as db:
        identity = Identity(
            username=f"t_{suffix}",
            email=f"t_{suffix}@x.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="T", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"agent_{suffix}",
            creator_id=user.id,
            tenant_id=user.tenant_id,
        )
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id


async def test_import_mcp_direct_creates_mcp_server_row():
    """A successful discovery creates real Tool rows linked to the server."""
    from app.services.resource_discovery import import_mcp_direct

    suffix = uuid.uuid4().hex[:6]
    url = f"https://import-{suffix}.test.invalid/mcp/"
    agent_id = await _make_agent(suffix)

    fake_client = AsyncMock()
    fake_client.server_instructions = "Use the bridge tool."
    fake_client.list_tools.return_value = [
        {
            "name": "bridge_tool",
            "description": "A discovered tool",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]

    with patch("app.services.mcp_client.MCPClient", return_value=fake_client):
        result = await import_mcp_direct(
            mcp_url=url,
            agent_id=agent_id,
            server_name=f"test_{suffix}",
        )

    assert "(1 tools)" in result

    # Verify the Tool + MCPServer row were created and linked.
    async with async_session() as db:
        tool = (
            await db.execute(
                select(Tool)
                .join(MCPServer, MCPServer.id == Tool.mcp_server_id)
                .join(MCPServerOverride, MCPServerOverride.mcp_server_id == MCPServer.id)
                .where(MCPServerOverride.scope_id == agent_id, MCPServerOverride.url_template == url)
            )
        ).scalar_one_or_none()
        assert tool is not None
        assert tool.mcp_tool_name == "bridge_tool"
        assert tool.mcp_server_id is not None, "mcp_server_id should be set by auto-bridge"
        srv = (await db.execute(
            select(MCPServer).where(MCPServer.id == tool.mcp_server_id)
        )).scalar_one()
        assert srv.base_url_template == ""
        assert srv.owner_agent_id == agent_id


@pytest.mark.parametrize("failure_mode", ["error", "empty"])
async def test_failed_or_empty_discovery_creates_no_placeholder_rows(failure_mode):
    """A direct import never reports success or persists a fake generic tool."""
    from app.services.resource_discovery import import_mcp_direct

    suffix = uuid.uuid4().hex[:6]
    url = f"https://failed-{failure_mode}-{suffix}.test.invalid/mcp/"
    agent_id = await _make_agent(suffix)

    fake_client = AsyncMock()
    fake_client.server_instructions = None
    if failure_mode == "error":
        fake_client.list_tools.side_effect = TimeoutError()
    else:
        fake_client.list_tools.return_value = []

    with patch("app.services.mcp_client.MCPClient", return_value=fake_client):
        result = await import_mcp_direct(
            mcp_url=url,
            agent_id=agent_id,
            server_name=f"failed_{suffix}",
        )

    assert result.startswith("❌ MCP server import failed")
    assert "No MCP server or tool records were created" in result

    async with async_session() as db:
        server = (await db.execute(
            select(MCPServer).where(MCPServer.base_url_template == url)
        )).scalar_one_or_none()
        tool = (await db.execute(
            select(Tool).where(Tool.mcp_server_url == url)
        )).scalar_one_or_none()

    assert server is None
    assert tool is None
