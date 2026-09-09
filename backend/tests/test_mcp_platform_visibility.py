"""Platform revocation, installation inventory and scene-aware MCP access."""

import json
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.database import async_session
from app.core.security import create_access_token
from app.models.tool import AgentTool, Tool
from app.services.agent_mcp_lifecycle import list_installed_mcp_servers, uninstall_mcp_server
from app.services.agent_tools import execute_tool, get_agent_tools_for_llm
from app.services.agent_context_extensions import _collect_extension_prompts
from app.services.agent_tools_mcp_runtime import _execute_mcp_tool
from app.services.mcp_access import resolve_mcp_execution
from app.services.turn_tool_settings import scene_tool_settings_scope
from mcp_tool_refresh_support import _isolate, _make_agent  # noqa: F401 -- shared autouse fixture
from test_agent_mcp_lifecycle import _make_agents, _make_server_tool

pytestmark = pytest.mark.asyncio


async def test_global_disable_hides_inventory_and_runtime_but_preserves_uninstall():
    tenant, (aid,) = await _make_agents(1)
    async with async_session() as db:
        server, tool = await _make_server_tool(db, tenant)
        tool.enabled = False
        db.add(AgentTool(agent_id=aid, tool_id=tool.id, enabled=True,
                         source="user_installed", installed_by_agent_id=aid))
        legacy = Tool(name=f"legacy_{uuid.uuid4().hex}", display_name="Legacy",
                      type="mcp", enabled=False, source="agent", tenant_id=tenant)
        db.add(legacy)
        await db.flush()
        db.add(AgentTool(agent_id=aid, tool_id=legacy.id, enabled=True))
        await db.commit()
        sid, name = server.id, tool.name
    assert json.loads(await list_installed_mcp_servers(aid)) == {"mcp_servers": []}
    definitions = await get_agent_tools_for_llm(aid)
    assert name not in {item["function"]["name"] for item in definitions}
    with patch("app.services.mcp_client.MCPClient") as client:
        await _execute_mcp_tool(name, {}, agent_id=aid)
        await _execute_mcp_tool("run", {}, agent_id=aid)
        client.assert_not_called()
    result = json.loads(await uninstall_mcp_server(aid, sid))
    assert result["ok"] is True
    assert result["removed_bindings"] == 1


async def test_inventory_counts_platform_visible_installs_and_current_scene():
    tenant, (aid,) = await _make_agents(1)
    async with async_session() as db:
        server, first = await _make_server_tool(db, tenant)
        _, hidden = await _make_server_tool(db, tenant, suffix="hidden")
        hidden.mcp_server_id = server.id
        hidden.enabled = False
        db.add_all([
            AgentTool(agent_id=aid, tool_id=first.id, enabled=False, source="system"),
            AgentTool(agent_id=aid, tool_id=hidden.id, enabled=True,
                      source="user_installed", installed_by_agent_id=aid),
        ])
        await db.commit()
        tid = first.id
    result = json.loads(await list_installed_mcp_servers(aid))["mcp_servers"]
    assert len(result) == 1
    assert (result[0]["tool_count"], result[0]["enabled_tool_count"], result[0]["removable"]) == (1, 0, True)
    async with scene_tool_settings_scope(aid, {"scene_tools": [{"tool_id": str(tid), "enabled": True}]}):
        result = json.loads(await list_installed_mcp_servers(aid))["mcp_servers"]
        assert result[0]["enabled_tool_count"] == 1
        async with async_session() as db:
            assert len(await resolve_mcp_execution(db, aid, "run")) == 1
    async with async_session() as db:
        assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == aid, AgentTool.tool_id == tid))
        assert assignment.enabled is False
    assert json.loads(await list_installed_mcp_servers(aid))["mcp_servers"][0]["enabled_tool_count"] == 0


async def test_foreign_server_is_hidden_even_with_a_global_tool_and_stale_assignment():
    tenant, (aid,) = await _make_agents(1)
    foreign, _ = await _make_agents(1)
    async with async_session() as db:
        server, tool = await _make_server_tool(db, foreign)
        tool.tenant_id = None
        server.system_prompt_block = "FOREIGN-MCP-PROMPT"
        db.add(AgentTool(agent_id=aid, tool_id=tool.id, enabled=True))
        await db.commit()
        name = tool.name
    assert json.loads(await list_installed_mcp_servers(aid))["mcp_servers"] == []
    async with async_session() as db:
        assert not await resolve_mcp_execution(db, aid, name)
        assert not await resolve_mcp_execution(db, aid, "run")
    assert name not in {t["function"]["name"] for t in await get_agent_tools_for_llm(aid)}
    assert "FOREIGN-MCP-PROMPT" not in "\n".join(await _collect_extension_prompts(aid))


async def test_scene_only_tool_can_resolve_without_inventing_installation():
    tenant, (aid,) = await _make_agents(1)
    async with async_session() as db:
        server, tool = await _make_server_tool(db, tenant)
        tool.source = "admin"
        await db.commit()
        tid, name = tool.id, tool.name
    async with scene_tool_settings_scope(aid, {"scene_tools": [{"tool_id": str(tid), "enabled": True}]}):
        assert json.loads(await list_installed_mcp_servers(aid))["mcp_servers"] == []
        with patch("app.services.mcp_client.MCPClient") as provider:
            provider.return_value.call_tool = AsyncMock(return_value="provider-result")
            assert await execute_tool("run", {}, aid, aid, skip_autonomy=True) == "provider-result"
            provider.return_value.call_tool.assert_awaited_once_with("run", {})
        async with async_session() as db:
            assert len(await resolve_mcp_execution(db, aid, name)) == 1
            assert len(await resolve_mcp_execution(db, aid, "run")) == 1
        async with async_session() as db:
            tool = await db.get(Tool, tid)
            tool.enabled = False
            await db.commit()
        async with async_session() as db:
            assert not await resolve_mcp_execution(db, aid, name)
            assert not await resolve_mcp_execution(db, aid, "run")
        with patch("app.services.mcp_client.MCPClient") as provider:
            await execute_tool("run", {}, aid, aid, skip_autonomy=True)
            provider.assert_not_called()


async def test_ambiguous_alias_and_disabled_canonical_name_cannot_fall_through():
    tenant, (aid,) = await _make_agents(1)
    async with async_session() as db:
        _, first = await _make_server_tool(db, tenant)
        _, second = await _make_server_tool(db, tenant, suffix="second")
        db.add_all([AgentTool(agent_id=aid, tool_id=tool.id, enabled=True) for tool in (first, second)])
        await db.commit()
        tid, other_tid, canonical = first.id, second.id, first.name
    with patch("app.services.mcp_client.MCPClient") as provider:
        await _execute_mcp_tool("run", {}, agent_id=aid)
        provider.assert_not_called()
    async with async_session() as db:
        assert len(await resolve_mcp_execution(db, aid, canonical)) == 1
        first = await db.get(Tool, tid)
        second = await db.get(Tool, other_tid)
        first.enabled = False
        second.mcp_tool_name = canonical
        await db.commit()
    with patch("app.services.mcp_client.MCPClient") as provider:
        await _execute_mcp_tool(canonical, {}, agent_id=aid)
        provider.assert_not_called()


@pytest.mark.parametrize("blocker_type", ["mcp", "builtin"])
async def test_foreign_canonical_name_does_not_shadow_current_agent_alias(blocker_type):
    tenant, (aid,) = await _make_agents(1)
    foreign, _ = await _make_agents(1)
    alias = f"alias_{uuid.uuid4().hex}"
    async with async_session() as db:
        _, tool = await _make_server_tool(db, tenant)
        tool.mcp_tool_name = alias
        db.add(AgentTool(agent_id=aid, tool_id=tool.id, enabled=True))
        db.add(Tool(name=alias, display_name="Foreign", type=blocker_type,
                    source="admin", tenant_id=foreign, enabled=True))
        await db.commit()
    with patch("app.services.mcp_client.MCPClient") as provider:
        provider.return_value.call_tool = AsyncMock(return_value="current-agent-result")
        assert await _execute_mcp_tool(alias, {}, agent_id=aid) == "current-agent-result"
        provider.return_value.call_tool.assert_awaited_once_with(alias, {})


async def test_platform_catalog_retains_disabled_mcp_while_agent_catalog_keeps_independent_cli():
    from app.main import app

    user, agent, server = await _make_agent()
    async with async_session() as db:
        mcp = Tool(name=f"mcp_{server.name}_read", display_name="Read", type="mcp", category="mcp",
                   tenant_id=agent.tenant_id, source="admin", enabled=False, mcp_server_id=server.id)
        cli = Tool(name=f"cli_{uuid.uuid4().hex[:10]}", display_name="Executable", type="cli", category="cli",
                   tenant_id=agent.tenant_id, source="admin", enabled=True,
                   config={"binary": {"sha256": "a" * 64}})
        db.add_all([mcp, cli])
        await db.flush()
        db.add_all([AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True) for tool in (mcp, cli)])
        await db.commit()
        mcp_id, cli_id = str(mcp.id), str(cli.id)
        cli_name = cli.name
    headers = {"Authorization": f"Bearer {create_access_token(str(user.id), user.role)}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/tools", headers=headers)
        assert response.status_code == 200, response.text
        catalog = {tool["id"]: tool for tool in response.json()}
        assert catalog[mcp_id]["enabled"] is False
        assert catalog[cli_id]["category"] == "cli"
        response = await client.get(f"/api/tools/agents/{agent.id}", headers=headers)
        assert response.status_code == 200, response.text
        catalog = {tool["id"]: tool for tool in response.json()}
        assert mcp_id not in catalog
        assert catalog[cli_id]["enabled"] is True
    definitions = await get_agent_tools_for_llm(agent.id)
    assert cli_name in {definition["function"]["name"] for definition in definitions}
