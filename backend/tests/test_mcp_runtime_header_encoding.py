"""Non-ASCII header values must be percent-encoded before reaching httpx.

HTTP headers are ASCII-only on the wire (RFC 7230). When a placeholder
renders to a Chinese name like ${agent.name} = '小智', the raw value
would crash httpx's ASCII encoder. The fix percent-encodes non-ASCII
values; ASCII-only values pass through unchanged.
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


async def test_non_ascii_header_value_is_percent_encoded():
    suffix = uuid.uuid4().hex[:6]
    async with async_session() as db:
        identity = Identity(username=f"u_{suffix}", email=f"u_{suffix}@x.local", password_hash="x")
        db.add(identity)
        await db.flush()
        user = User(identity_id=identity.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        # Agent name contains non-ASCII characters
        agent = Agent(name=f"小智_{suffix}", creator_id=user.id)
        db.add(agent)
        await db.flush()

        srv = MCPServer(
            name=f"hdr_{suffix}", display_name="hdr",
            base_url_template="https://srv.example",
            headers_template={
                "X-Agent-Name": "${agent.name}",     # non-ASCII rendered value
                "X-User-Email": "${user.email}",     # ASCII rendered value
            },
        )
        db.add(srv)
        await db.flush()

        tool = Tool(
            name=f"mcp_hdr_{suffix}", display_name="t", type="mcp",
            mcp_server_url="https://srv.example", mcp_server_id=srv.id, mcp_tool_name="x",
        )
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        agent_id = agent.id
        user_id = user.id

    fake = AsyncMock()
    fake.call_tool = AsyncMock(return_value="OK")
    with patch("app.services.mcp_client.MCPClient", return_value=fake) as mock_cls:
        result = await _execute_mcp_tool(
            f"mcp_hdr_{suffix}", {}, agent_id=agent_id, user_id=user_id,
        )

    assert result == "OK", f"call should succeed: got {result!r}"
    args, kwargs = mock_cls.call_args
    headers = kwargs["headers"]
    # Non-ASCII rendered to percent-encoded
    assert headers["X-Agent-Name"].startswith("%"), \
        f"Chinese agent name should be percent-encoded, got {headers['X-Agent-Name']!r}"
    # Spot check it's actually decodable to the original
    from urllib.parse import unquote
    assert unquote(headers["X-Agent-Name"]) == f"小智_{suffix}"
    # ASCII passes through unchanged
    assert "%" not in headers["X-User-Email"]
    assert headers["X-User-Email"] == f"u_{suffix}@x.local"
