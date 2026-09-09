"""Private credential variants and shared catalog immutability."""

from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.database import async_session
from app.main import app
from app.models.mcp_server import MCPServer
from app.models.tool import AgentTool, Tool
from app.services.agent_tools_mcp_runtime import _execute_mcp_tool
from app.services.resource_discovery import import_mcp_direct
from mcp_tool_refresh_support import _isolate, _make_agent  # noqa: F401 -- autouse fixture

pytestmark = pytest.mark.asyncio


class Provider:
    server_instructions = "provider instructions"
    calls = []

    def __init__(self, server_url=None, api_key=None, headers=None):
        self.key, self.headers = api_key, headers

    async def list_tools(self):
        return [{"name": "read", "description": "Read", "inputSchema": {"type": "object", "properties": {}}}]

    async def call_tool(self, name, arguments):
        type(self).calls.append((name, self.key, self.headers))
        return "called"


async def test_one_agent_same_name_url_different_credentials_has_separate_stable_installations():
    user, agent, _ = await _make_agent()
    config = {"mcp_url": "https://same.example/mcp", "agent_id": agent.id, "server_name": "Same service"}
    with patch("app.services.mcp_client.MCPClient", Provider):
        for key, headers in [("key-a", {"X-Team": "A"}), ("key-b", {"X-Team": "B"}), ("key-a", {"X-Team": "A"})]:
            await import_mcp_direct(**config, api_key=key, headers=headers)
        async with async_session() as db:
            tools = (await db.scalars(select(Tool).join(AgentTool).where(AgentTool.agent_id == agent.id))).all()
            servers = (await db.scalars(select(MCPServer).where(MCPServer.id.in_([tool.mcp_server_id for tool in tools])))).all()
        assert len(tools) == len(servers) == 2
        assert len({server.name for server in servers}) == 2
        assert {server.display_name for server in servers} == {"Same service", "Same service-2"}
        assert len({tool.name for tool in tools}) == 2
        assert all(secret not in server.name for server in servers for secret in ("key-a", "key-b"))
        Provider.calls = []
        for tool in tools:
            assert await _execute_mcp_tool(tool.name, {}, agent_id=agent.id, user_id=user.id) == "called"
        assert {(key, headers["X-Team"]) for _, key, headers in Provider.calls} == {("key-a", "A"), ("key-b", "B")}


async def test_stdio_environment_variants_preserve_catalog_origin_and_retries():
    from app.services.mcp_server_service import get_or_create_agent_stdio_server, persist_stdio_discovered_tools

    user, agent, _ = await _make_agent()
    ids = []
    async with async_session() as db:
        for secret in ("env-a", "env-b", "env-a"):
            cfg = {"command": "npx", "args": ["-y", "sample-mcp"], "env": {"TOKEN": secret}}
            server = await get_or_create_agent_stdio_server(db, agent.id, agent.tenant_id, cfg)
            await persist_stdio_discovered_tools(db, server, await Provider().list_tools(), source="agent")
            tool = await db.scalar(select(Tool).where(Tool.mcp_server_id == server.id))
            if not await db.scalar(select(AgentTool.id).where(AgentTool.agent_id == agent.id, AgentTool.tool_id == tool.id)):
                db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True,
                                 source="user_installed", installed_by_agent_id=agent.id))
            await db.commit()
            assert tool.source == "agent"
            ids.append(server.id)
    assert ids[0] == ids[2] != ids[1]


@pytest.mark.parametrize("platform", [False, True])
async def test_shared_agent_can_override_and_filter_but_never_mutate_catalog(platform):
    user, agent, initial_server = await _make_agent()
    async with async_session() as db:
        server = await db.get(MCPServer, initial_server.id)
        server.tenant_id = None if platform else agent.tenant_id
        server.created_by_user_id = user.id
        server.credential_template = "shared-key"
        tool = Tool(name=f"mcp_{server.name}_shared", display_name="Shared", type="mcp", source="admin",
                    tenant_id=server.tenant_id, mcp_server_id=server.id, mcp_tool_name="read",
                    enabled=True, description="immutable", parameters_schema={"type": "object", "properties": {}})
        db.add(tool)
        await db.flush()
        binding = AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True,
                            source="user_installed", installed_by_agent_id=agent.id)
        db.add(binding)
        await db.commit()
        sid, tid, bid, name = server.id, tool.id, binding.id, tool.name
    token = create_access_token(str(user.id), user.role)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        with patch("app.services.mcp_refresh_service.MCPClient") as provider:
            for suffix in ("/refresh-tools", f"/refresh-tools?agent_id={agent.id}", "/test-connection"):
                response = await client.post(f"/api/admin/mcp-servers/{sid}{suffix}")
                assert response.status_code == 403, response.text
            provider.assert_not_called()
        response = await client.patch(f"/api/admin/mcp-servers/{sid}", json={"display_name": "changed"})
        assert response.status_code == 403
        response = await client.put(f"/api/admin/mcp-servers/{sid}/overrides/agent/{agent.id}",
                                    json={"url_template": "https://different.example/mcp"})
        assert response.status_code == 403
        response = await client.put(f"/api/admin/mcp-servers/{sid}/overrides/agent/{agent.id}",
                                    json={"credential_template": "private-key", "headers_template": {"X-Team": "private"}})
        assert response.status_code == 200, response.text
        with patch("app.services.mcp_refresh_service.MCPClient", Provider):
            response = await client.post(f"/api/admin/mcp-servers/{sid}/test-connection?agent_id={agent.id}")
        assert response.status_code == 200 and response.json()["success"], response.text
        with patch("app.services.mcp_client.MCPClient", Provider):
            Provider.calls = []
            assert await _execute_mcp_tool(name, {}, agent_id=agent.id, user_id=user.id) == "called"
            assert Provider.calls == [("read", "private-key", {"X-Team": "private"})]
        response = await client.put(f"/api/tools/agents/{agent.id}", json=[{"tool_id": str(tid), "enabled": False}])
        assert response.status_code == 200, response.text
        with patch("app.services.mcp_client.MCPClient") as provider:
            await _execute_mcp_tool(name, {}, agent_id=agent.id)
            provider.assert_not_called()
        response = await client.delete(f"/api/tools/agent-tool/{bid}")
        assert response.status_code == 200, response.text
    async with async_session() as db:
        server, tool = await db.get(MCPServer, sid), await db.get(Tool, tid)
        assert server.credential_template == "shared-key" and server.instructions is None
        assert tool.enabled and tool.description == "immutable"
        assert not await db.get(AgentTool, bid)
