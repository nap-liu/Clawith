"""When user_id == agent_id (trigger-daemon sentinel), placeholder
context falls back to Agent.creator_id for ${user.*} resolution.
"""
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from app.database import async_session, engine
from app.models.user import User, Identity
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


async def test_user_id_equals_agent_id_falls_back_to_creator():
    """Trigger daemon path: caller.py passes user_id=agent_id sentinel.
    PlaceholderContext should resolve ${user.email} to Agent.creator's email.
    """
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        # Creator user
        creator_identity = Identity(
            username=f"creator_{suffix}", email=f"creator-{suffix}@org.test", password_hash="x",
        )
        db.add(creator_identity)
        await db.flush()
        creator = User(identity_id=creator_identity.id, display_name="Creator",
                       role="member", is_active=True)
        db.add(creator)
        await db.flush()

        agent = Agent(name=f"Trig_{suffix}", creator_id=creator.id)
        db.add(agent)
        await db.flush()

        srv = MCPServer(
            name=f"trig_{suffix}", display_name="t",
            base_url_template="https://srv/${user.email}",
            headers_template={"X-Owner": "${user.email}"},
        )
        db.add(srv)
        await db.flush()

        tool = Tool(
            name=f"mcp_trig_{suffix}", display_name="t", type="mcp",
            mcp_server_url="https://srv", mcp_server_id=srv.id, mcp_tool_name="x",
        )
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        agent_id = agent.id

    fake_client = AsyncMock()
    fake_client.call_tool = AsyncMock(return_value="OK")

    # Simulate trigger-daemon path: user_id == agent_id sentinel
    with patch("app.services.mcp_client.MCPClient", return_value=fake_client) as mock_cls:
        result = await _execute_mcp_tool(
            f"mcp_trig_{suffix}", {}, agent_id=agent_id, user_id=agent_id,  # sentinel
        )

    assert result == "OK"
    args, kwargs = mock_cls.call_args
    expected_email = f"creator-{suffix}@org.test"
    assert args[0] == f"https://srv/{expected_email}"
    assert kwargs["headers"]["X-Owner"] == expected_email


async def test_user_id_none_also_falls_back_to_creator():
    """Direct trigger path with user_id=None — same fallback."""
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        creator_identity = Identity(
            username=f"creator_{suffix}", email=f"owner-{suffix}@org.test", password_hash="x",
        )
        db.add(creator_identity)
        await db.flush()
        creator = User(identity_id=creator_identity.id, display_name="O", role="member", is_active=True)
        db.add(creator)
        await db.flush()

        agent = Agent(name=f"None_{suffix}", creator_id=creator.id)
        db.add(agent)
        await db.flush()

        srv = MCPServer(
            name=f"none_{suffix}", display_name="n",
            base_url_template="https://srv/${user.email}",
            headers_template={},
        )
        db.add(srv)
        await db.flush()

        tool = Tool(
            name=f"mcp_none_{suffix}", display_name="n", type="mcp",
            mcp_server_url="https://srv", mcp_server_id=srv.id, mcp_tool_name="x",
        )
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        agent_id = agent.id

    fake_client = AsyncMock()
    fake_client.call_tool = AsyncMock(return_value="OK")

    with patch("app.services.mcp_client.MCPClient", return_value=fake_client) as mock_cls:
        result = await _execute_mcp_tool(
            f"mcp_none_{suffix}", {}, agent_id=agent_id, user_id=None,  # absent user
        )

    assert result == "OK"
    args, _ = mock_cls.call_args
    assert args[0] == f"https://srv/owner-{suffix}@org.test"
