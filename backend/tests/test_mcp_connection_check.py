"""Connection checks preserve installation routing and Project credentials."""

import json
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.models.tool import AgentTool, Tool
from app.services.agent_tools_mcp_runtime import _execute_mcp_tool
from app.services.mcp_connection_check import check_agent_connection
from mcp_tool_refresh_support import _isolate, _make_agent  # noqa: F401
from test_mcp_refresh_project_references import _add_project_reference

pytestmark = pytest.mark.asyncio


async def test_smithery_check_and_call_use_exact_private_installation_without_recovery():
    user, agent, _ = await _make_agent()
    installations = []
    async with async_session() as db:
        for variant in ("a", "b"):
            server = MCPServer(tenant_id=agent.tenant_id, name=f"connect-{agent.id}-{variant}",
                               display_name="Connect", base_url_template="https://same.run.tools/mcp")
            db.add(server)
            await db.flush()
            tool = Tool(name=f"connect_{server.id.hex}", display_name="Read", type="mcp", source="agent",
                        tenant_id=agent.tenant_id, mcp_server_id=server.id, mcp_tool_name="read")
            db.add(tool)
            await db.flush()
            config = {"smithery_namespace": f"ns-{variant}", "smithery_connection_id": f"conn-{variant}",
                      "smithery_api_key": f"key-{variant}"}
            assignment = AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True, config=config,
                                   source="user_installed", installed_by_agent_id=agent.id)
            db.add(assignment)
            await db.flush()
            installations.append((server.id, tool.name, assignment.id, config))
        await db.commit()
    requests = []
    rejected = False

    def provider(request):
        if rejected:
            return httpx.Response(401, json={"error": "unauthorized"})
        payload = json.loads(request.content)
        method = payload["method"]
        requests.append((request.url.path, request.headers.get("authorization"), method))
        if method.startswith("notifications/"):
            return httpx.Response(202)
        result = {"initialize": {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "serverInfo": {"name": "test", "version": "1"}},
                  "tools/list": {"tools": [{"name": "read", "inputSchema": {}}]},
                  "tools/call": {"content": [{"type": "text", "text": "called"}]}}[method]
        data = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        if "conn-b" in request.url.path:
            return httpx.Response(200, text=f"event: message\ndata: {json.dumps(data)}\n\n",
                                  headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json=data)

    client_type = httpx.AsyncClient
    with patch("httpx.AsyncClient", side_effect=lambda **kw: client_type(transport=httpx.MockTransport(provider), **kw)), \
            patch("app.services.agent_tools_mcp_runtime._smithery_auto_recover") as recover:
        for sid, name, aid, config in installations:
            async with async_session() as db:
                result = await check_agent_connection(db, await db.get(MCPServer, sid), agent.id, user.id, agent.tenant_id)
                assert result.success, result.error
                assert not db.in_transaction()
            assert await _execute_mcp_tool(name, {}, agent.id, user.id) == "called"
            path = f"/connect/{config['smithery_namespace']}/{config['smithery_connection_id']}/mcp"
            assert {method for url, key, method in requests if url == path and key == f"Bearer {config['smithery_api_key']}"} >= {"tools/list", "tools/call"}
        rejected = True
        async with async_session() as db:
            result = await check_agent_connection(db, await db.get(MCPServer, installations[0][0]),
                                                   agent.id, user.id, agent.tenant_id)
            assert not result.success
        recover.assert_not_called()
    async with async_session() as db:
        for sid, _, aid, config in installations:
            assert (await db.get(AgentTool, aid)).config == config
            assert (await db.get(MCPServer, sid)).instructions is None


async def test_project_check_uses_same_source_identity_as_execution_without_copying():
    user, source, server = await _make_agent()
    project, tool = await _add_project_reference(user, source, server, enabled=True)
    async with async_session() as db:
        assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == project.id))
        assignment.enabled = True
        db.add_all([
            MCPServerOverride(mcp_server_id=server.id, scope_type="agent", scope_id=source.id,
                              credential_template="source-key", headers_template={"X-Label": "中文"}),
            MCPServerOverride(mcp_server_id=server.id, scope_type="agent", scope_id=project.id,
                              credential_template="stale-copied-key"),
        ])
        await db.commit()
    observed = []

    class Provider:
        server_instructions = "read-only"

        def __init__(self, url, api_key=None, headers=None):
            observed.append((api_key, headers))

        async def list_tools(self):
            return [{"name": "seed", "inputSchema": {}}]

        async def call_tool(self, *_):
            return "called"

    with patch("app.services.mcp_refresh_service.MCPClient", Provider), patch("app.services.mcp_client.MCPClient", Provider):
        for execution_user in (user.id, None):
            async with async_session() as db:
                result = await check_agent_connection(db, await db.get(MCPServer, server.id), project.id,
                                                       execution_user, project.tenant_id)
                assert result.success
            assert await _execute_mcp_tool(tool.name, {}, project.id, execution_user) == "called"
            assert observed[-1] == observed[-2]
            assert observed[-1][0] == ("source-key" if execution_user else None)
        assert observed[0][1] == {"X-Label": "%E4%B8%AD%E6%96%87"}
        async with async_session() as db:
            assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == project.id))
            assignment.enabled = False
            await db.commit()
        async with async_session() as db:
            result = await check_agent_connection(db, await db.get(MCPServer, server.id), project.id,
                                                   None, project.tenant_id)
            assert result.success and observed[-1][0] is None
    async with async_session() as db:
        assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == project.id))
        assert assignment.config == {"project_option": "preserve"}
        assert (await db.get(MCPServer, server.id)).instructions is None
