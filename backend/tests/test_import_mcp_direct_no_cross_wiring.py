"""import_mcp_direct must NOT merge tools across distinct MCP servers.

Regression for the CJK-name collapse bug: two different DingTalk-style MCP
servers (distinct URLs, both with pure-CJK display names that slugify to the
same fallback) that expose the SAME raw tool name must produce TWO distinct
Tool rows, each bound to its own MCPServer. Before the fix, the second install
collided on the global Tool.name and got force-merged onto the first server's
row — so agent B's tool calls authenticated with agent A's key.
"""
import uuid
import pytest
from unittest.mock import patch
from sqlalchemy import select

from app.database import async_session, engine
from app.models.tool import Tool, AgentTool
from app.models.mcp_server import MCPServer
from app.models.agent import Agent
from app.models.user import User, Identity

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


class _FakeMCPClient:
    """Minimal stand-in for MCPClient as used by import_mcp_direct."""

    def __init__(self, url, headers=None, api_key=None):
        self.server_url = url
        self.server_instructions = None

    async def list_tools(self):
        return [
            {
                "name": "get_send_report_list",
                "description": "list reports",
                "inputSchema": {"type": "object", "properties": {}},
            }
        ]


async def _mk_agent(suffix):
    async with async_session() as db:
        ident = Identity(username=f"u_{suffix}", email=f"u_{suffix}@x.local", password_hash="x")
        db.add(ident)
        await db.flush()
        user = User(identity_id=ident.id, display_name="U", role="member", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name=f"a_{suffix}", creator_id=user.id, tenant_id=user.tenant_id)
        db.add(agent)
        await db.commit()
        await db.refresh(agent)
        return agent.id


async def _routed_server_id(agent_id, raw_tool="get_send_report_list"):
    async with async_session() as db:
        rows = (
            await db.execute(
                select(Tool)
                .join(AgentTool, AgentTool.tool_id == Tool.id)
                .where(AgentTool.agent_id == agent_id, Tool.mcp_tool_name == raw_tool)
            )
        ).scalars().all()
        assert len(rows) == 1, f"expected exactly 1 enabled '{raw_tool}' for agent, got {len(rows)}"
        return rows[0].mcp_server_id


async def test_cjk_named_servers_do_not_cross_wire():
    from app.services.resource_discovery import import_mcp_direct

    s = uuid.uuid4().hex[:6]
    agent_a = await _mk_agent(f"a{s}")
    agent_b = await _mk_agent(f"b{s}")
    url_a = f"https://mcp-gw.example/server/aaa{s}?key=KEYA{s}"
    url_b = f"https://mcp-gw.example/server/bbb{s}?key=KEYB{s}"

    with patch("app.services.mcp_client.MCPClient", _FakeMCPClient):
        # Both installs use the same pure-CJK display name.
        await import_mcp_direct(mcp_url=url_a, agent_id=agent_a, server_name="钉钉日志")
        await import_mcp_direct(mcp_url=url_b, agent_id=agent_b, server_name="钉钉日志")

    async with async_session() as db:
        srv_a = (await db.execute(select(MCPServer).where(MCPServer.base_url_template == url_a))).scalar_one_or_none()
        srv_b = (await db.execute(select(MCPServer).where(MCPServer.base_url_template == url_b))).scalar_one_or_none()

    assert srv_a is not None, "server A row missing"
    assert srv_b is not None, "server B row was never created (bridge skipped on collision)"
    assert srv_a.id != srv_b.id

    rs_a = await _routed_server_id(agent_a)
    rs_b = await _routed_server_id(agent_b)

    assert rs_a == srv_a.id, f"agent A routes to {rs_a}, expected its own server {srv_a.id}"
    assert rs_b == srv_b.id, f"agent B routes to {rs_b}, expected its own server {srv_b.id}"
    assert rs_a != rs_b, "CROSS-WIRING: both agents route to the SAME MCP server (= same key)"
