"""Direct MCP imports are atomic and bridge discovered tools to mcp_servers."""
import uuid
import pytest
from sqlalchemy import select
from unittest.mock import AsyncMock, patch

from app.database import async_session, engine
from app.models.tool import AgentTool, Tool
from app.models.mcp_server import MCPServer

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
    assert "MCP Server ID" in result

    # Verify the Tool + MCPServer row were created and linked.
    async with async_session() as db:
        tool = (await db.execute(
            select(Tool).where(Tool.mcp_server_url == url)
        )).scalar_one_or_none()
        assert tool is not None
        assert tool.mcp_tool_name == "bridge_tool"
        assert tool.mcp_server_id is not None, "mcp_server_id should be set by auto-bridge"
        srv = (await db.execute(
            select(MCPServer).where(MCPServer.id == tool.mcp_server_id)
        )).scalar_one()
        assert srv.base_url_template == url
        assignment = (await db.execute(
            select(AgentTool).where(
                AgentTool.agent_id == agent_id,
                AgentTool.tool_id == tool.id,
            )
        )).scalar_one()
        assert assignment.installed_by_agent_id == agent_id
        assert assignment.config["mcp_url"] == url
        assert assignment.config["server_name"] == f"test_{suffix}"


async def test_smithery_import_links_existing_model_and_returns_exact_server_id():
    """Smithery keeps its install path but bridges tools to the existing server table."""
    from app.models.tool import AgentTool
    from app.services.resource_discovery import import_mcp_from_smithery

    suffix = uuid.uuid4().hex[:6]
    agent_id = await _make_agent(suffix)

    class _Response:
        def __init__(self, payload=None, *, text="", status_code=200):
            self._payload = payload or {}
            self.text = text
            self.status_code = status_code

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            if url.endswith("/servers"):
                return _Response(
                    {
                        "servers": [
                            {
                                "qualifiedName": "vendor/example",
                                "displayName": "Example",
                                "description": "Example MCP",
                                "remote": True,
                            }
                        ]
                    }
                )
            return _Response(
                {
                    "deploymentUrl": "https://vendor-example.run.tools",
                    "tools": [
                        {
                            "name": "lookup",
                            "description": "Lookup",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ],
                }
            )

        async def post(self, url, **kwargs):
            return _Response(
                text=(
                    'data: {"result":{"tools":[{"name":"lookup",'
                    '"description":"Lookup","inputSchema":{"type":"object",'
                    '"properties":{}}}]}}\n'
                )
            )

    connection = {
        "namespace": "creator-ns",
        "connection_id": "creator-conn",
        "auth_url": None,
    }
    with patch("app.services.resource_discovery.httpx.AsyncClient", _Client), patch(
        "app.services.resource_discovery._get_smithery_api_key",
        AsyncMock(return_value="smithery-secret"),
    ), patch(
        "app.services.resource_discovery._ensure_smithery_connection",
        AsyncMock(return_value=connection),
    ):
        first = await import_mcp_from_smithery("vendor/example", agent_id)
        second = await import_mcp_from_smithery("vendor/example", agent_id)

    assert "MCP Server ID" in first
    assert "MCP Server ID" in second
    async with async_session() as db:
        servers = (
            await db.execute(
                select(MCPServer).where(
                    MCPServer.base_url_template == "https://vendor-example.run.tools"
                )
            )
        ).scalars().all()
        assert len(servers) == 1
        tools = (
            await db.execute(
                select(Tool).where(
                    Tool.mcp_server_id == servers[0].id,
                    Tool.mcp_tool_name == "lookup",
                )
            )
        ).scalars().all()
        assert len(tools) == 1
        assignments = (
            await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.tool_id == tools[0].id,
                )
            )
        ).scalars().all()
        assert len(assignments) == 1
        assert assignments[0].installed_by_agent_id == agent_id
        assert assignments[0].config["smithery_api_key"] == "smithery-secret"
        assert assignments[0].config["mcp_url"] == "https://vendor-example.run.tools"


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
