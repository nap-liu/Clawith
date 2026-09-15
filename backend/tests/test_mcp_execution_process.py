"""MCP revocation across the real execution process and HTTP boundaries."""

import asyncio
import json

import pytest

from app.database import async_session
from app.models.mcp_server import MCPServer
from app.models.tool import AgentTool, Tool
from app.services.agent_tools import execute_tool
from app.services.turn_tool_settings import scene_tool_settings_scope
from mcp_tool_refresh_support import _isolate, _make_agent  # noqa: F401 -- autouse fixture

pytestmark = pytest.mark.asyncio


async def test_scene_alias_and_platform_revocation_cross_real_execution_process(monkeypatch):
    monkeypatch.setenv("AGENT_EXECUTION_ISOLATION", "1")
    requests = []

    async def respond(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            headers = dict(line.split(":", 1) for line in header.decode().split("\r\n")[1:] if ":" in line)
            size = int(next(value for key, value in headers.items() if key.lower() == "content-length"))
            request = json.loads(await reader.readexactly(size))
            requests.append(request)
            if request["method"] == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "local-test-provider", "version": "1"}}
            elif request["method"] == "tools/call":
                result = {"content": [{"type": "text", "text": "real-provider-result"}]}
            else:
                result = {}
            payload = json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                         + f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    provider = await asyncio.start_server(respond, "127.0.0.1", 0)
    port = provider.sockets[0].getsockname()[1]
    try:
        user, agent, server = await _make_agent()
        async with async_session() as db:
            server_row = await db.get(MCPServer, server.id)
            server_row.base_url_template = f"http://127.0.0.1:{port}/mcp"
            tool = Tool(name=f"mcp_{server.name}_read", display_name="Read", type="mcp",
                        tenant_id=agent.tenant_id, source="agent", enabled=True,
                        mcp_server_id=server.id, mcp_tool_name="read")
            db.add(tool)
            await db.flush()
            db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=False,
                            source="user_installed", installed_by_agent_id=agent.id))
            await db.commit()
            tool_id, name = tool.id, tool.name
        async with scene_tool_settings_scope(agent.id, {
            "scene_tools": [{"tool_id": str(tool_id), "enabled": True}],
        }):
            result = await asyncio.wait_for(execute_tool("read", {}, agent.id, user.id, skip_autonomy=True), 30)
            assert [request["params"]["name"] for request in requests if request["method"] == "tools/call"] == ["read"]
            admitted_requests = len(requests)
            async with async_session() as db:
                tool = await db.get(Tool, tool_id)
                tool.enabled = False
                await db.commit()
            # Keep the original scene snapshot alive: a fresh child must still
            # apply the live platform veto to canonical and alias calls.
            for requested_name in (name, "read"):
                result = await asyncio.wait_for(execute_tool(requested_name, {}, agent.id, user.id,
                                                             skip_autonomy=True), 30)
                assert result != "real-provider-result"
                assert len(requests) == admitted_requests
    finally:
        provider.close()
        await provider.wait_closed()
