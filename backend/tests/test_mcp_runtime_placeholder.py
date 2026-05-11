"""End-to-end placeholder rendering at MCP tool call site.

Mocks MCPClient.call_tool — asserts the *constructor* receives the
correctly-rendered URL / headers / api_key. This proves \${user.email},
\${tenant.id}, etc. are interpolated when the LLM triggers a tool call.
"""
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from app.database import async_session, engine
from app.models.user import User, Identity
from app.models.tenant import Tenant
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tool import Tool, AgentTool
from app.services.agent_tools import _execute_mcp_tool

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_render_user_email_into_url_and_headers():
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        tenant = Tenant(name=f"T_{suffix}", slug=f"t-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(username=f"u_{suffix}", email=f"alice-{suffix}@example.com",
                            password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="Alice", role="member",
                    is_active=True, tenant_id=tenant.id)
        db.add(user)
        await db.flush()
        agent = Agent(name=f"A_{suffix}", creator_id=user.id, tenant_id=tenant.id)
        db.add(agent)
        await db.flush()

        srv = MCPServer(
            name=f"render_{suffix}", display_name="r",
            base_url_template="https://srv.example/${tenant.id}/items",
            headers_template={"X-User-Email": "${user.email}", "X-Static": "y"},
            credential_template="${user.email}-token",
        )
        db.add(srv)
        await db.flush()

        tool = Tool(
            name=f"mcp_render_{suffix}", display_name="r", type="mcp",
            mcp_server_url="https://srv.example/${tenant.id}/items",  # legacy field, ignored when mcp_server_id set
            mcp_server_id=srv.id, mcp_tool_name="search",
        )
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        agent_id = agent.id
        user_id = user.id
        tenant_id = tenant.id

    fake_client = AsyncMock()
    fake_client.call_tool = AsyncMock(return_value="OK")

    with patch("app.services.mcp_client.MCPClient", return_value=fake_client) as mock_cls:
        result = await _execute_mcp_tool(
            f"mcp_render_{suffix}", {}, agent_id=agent_id, user_id=user_id, session_id="sess-1",
        )

    assert result == "OK"
    # Inspect MCPClient constructor args
    args, kwargs = mock_cls.call_args
    assert args[0] == f"https://srv.example/{tenant_id}/items"
    assert kwargs["headers"]["X-User-Email"] == f"alice-{suffix}@example.com"
    assert kwargs["headers"]["X-Static"] == "y"
    assert kwargs["api_key"] == f"alice-{suffix}@example.com-token"


async def test_unknown_placeholder_returns_error_to_llm():
    """When ${user.unknown_field} is in template, return structured error string."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(username=f"u_{suffix}", email=f"u-{suffix}@x.local", password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name=f"A_{suffix}", creator_id=user.id)
        db.add(agent)
        await db.flush()

        srv = MCPServer(
            name=f"bad_{suffix}", display_name="b",
            base_url_template="https://srv.example/${user.unknown_field}/x",
            headers_template={},
        )
        db.add(srv)
        await db.flush()

        tool = Tool(
            name=f"mcp_bad_{suffix}", display_name="b", type="mcp",
            mcp_server_url="https://srv.example", mcp_server_id=srv.id,
            mcp_tool_name="search",
        )
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        agent_id = agent.id
        user_id = user.id

    with patch("app.services.mcp_client.MCPClient") as mock_cls:
        result = await _execute_mcp_tool(
            f"mcp_bad_{suffix}", {}, agent_id=agent_id, user_id=user_id,
        )

    # Should NOT have constructed the client
    mock_cls.assert_not_called()
    # Result should be structured error
    assert "❌ MCP tool" in result
    assert "URL placeholder error" in result or "unresolved" in result.lower()


async def test_legacy_path_unchanged_when_mcp_server_id_null():
    """Tool with mcp_server_id IS NULL falls back to old behavior."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(username=f"u_{suffix}", email=f"u-{suffix}@x.local", password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name=f"A_{suffix}", creator_id=user.id)
        db.add(agent)
        await db.flush()

        tool = Tool(
            name=f"mcp_legacy_{suffix}", display_name="l", type="mcp",
            mcp_server_url=f"https://legacy-{suffix}.example",
            mcp_server_id=None,  # NOT migrated
            mcp_tool_name="search",
        )
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        agent_id = agent.id
        user_id = user.id

    fake_client = AsyncMock()
    fake_client.call_tool = AsyncMock(return_value="LEGACY-OK")

    with patch("app.services.mcp_client.MCPClient", return_value=fake_client) as mock_cls:
        result = await _execute_mcp_tool(
            f"mcp_legacy_{suffix}", {}, agent_id=agent_id, user_id=user_id,
        )

    assert result == "LEGACY-OK"
    args, _ = mock_cls.call_args
    # Legacy path uses tool.mcp_server_url verbatim — NO placeholder rendering
    assert args[0] == f"https://legacy-{suffix}.example"
