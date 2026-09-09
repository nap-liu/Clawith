"""Current-Agent MCP inventory, exact uninstall, and observable redaction."""

import json
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.services.agent_mcp_lifecycle import (
    list_installed_mcp_servers,
    uninstall_mcp_server,
)
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.tool_seeder import seed_builtin_tools

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


async def _make_agents(count=2):
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        tenant = Tenant(name=f"T_{suffix}", slug=f"t-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"u_{suffix}",
            email=f"u-{suffix}@x.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="U",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        agents = [
            Agent(name=f"A{i}_{suffix}", creator_id=user.id, tenant_id=tenant.id)
            for i in range(count)
        ]
        db.add_all(agents)
        await db.commit()
        return tenant.id, [agent.id for agent in agents]


async def _make_server_tool(db, tenant_id, suffix="inventory", transport="http"):
    server = MCPServer(
        tenant_id=tenant_id,
        name=f"{suffix}-{uuid.uuid4().hex[:8]}",
        display_name=suffix.title(),
        base_url_template="https://mcp.example/work?region=cn",
        headers_template={},
        transport=transport,
        command_template="npx" if transport == "stdio" else None,
        args_template=["-y", "pkg"] if transport == "stdio" else None,
        env_template={} if transport == "stdio" else None,
    )
    db.add(server)
    await db.flush()
    tool = Tool(
        name=f"mcp_{server.name}_run",
        display_name=f"{suffix}: run",
        type="mcp",
        category="mcp",
        mcp_server_id=server.id,
        mcp_server_name=server.name,
        mcp_tool_name="run",
        source="agent",
    )
    db.add(tool)
    await db.flush()
    return server, tool


async def test_inventory_uses_existing_assignment_provenance_and_exact_server_id():
    tenant_id, (owner_id, other_id) = await _make_agents()
    async with async_session() as db:
        server, tool = await _make_server_tool(db, tenant_id)
        db.add_all(
            [
                AgentTool(
                    agent_id=owner_id,
                    tool_id=tool.id,
                    enabled=True,
                    source="user_installed",
                    installed_by_agent_id=owner_id,
                    config={
                        "mcp_url": "https://mcp.example/work?token=secret&region=cn",
                        "api_key": "owner-secret",
                        "headers": {"X-Debug": "kept"},
                    },
                ),
                AgentTool(
                    agent_id=other_id,
                    tool_id=tool.id,
                    enabled=True,
                    source="system",
                    config={"api_key": "must-not-project"},
                ),
            ]
        )
        await db.commit()
        server_id = server.id

    owner_raw = await list_installed_mcp_servers(owner_id)
    other_raw = await list_installed_mcp_servers(other_id)
    owner = json.loads(owner_raw)["mcp_servers"][0]
    other = json.loads(other_raw)["mcp_servers"][0]

    assert owner == {
        "mcp_server_id": str(server_id),
        "display_name": "Inventory",
        "transport": "http",
        "tool_count": 1,
        "enabled_tool_count": 1,
        "removable": True,
    }
    assert other == {**owner, "removable": False}
    assert "owner-secret" not in owner_raw
    assert "must-not-project" not in other_raw
    assert "tools" not in owner and "config" not in owner and "configs" not in owner


async def test_refresh_dispatch_passes_current_turn_identity_and_session():
    _tenant_id, (agent_id,) = await _make_agents(1)
    async with async_session() as db:
        user_id = await db.scalar(select(Agent.creator_id).where(Agent.id == agent_id))
    server_id = uuid.uuid4()
    expected = json.dumps({"ok": True})

    with patch(
        "app.services.agent_mcp_lifecycle.refresh_mcp_server",
        AsyncMock(return_value=expected),
    ) as refresh, patch("app.services.activity_logger.log_activity", AsyncMock()):
        from app.services.agent_tools import execute_tool

        result = await execute_tool(
            "refresh_mcp_server",
            {"mcp_server_id": str(server_id)},
            agent_id=agent_id,
            user_id=user_id,
            session_id="current-session",
        )

    assert result == expected
    refresh.assert_awaited_once_with(
        agent_id,
        server_id,
        user_id=user_id,
        session_id="current-session",
    )


async def test_uninstall_only_detaches_current_agent_and_preserves_shared_server():
    tenant_id, (agent_a, agent_b) = await _make_agents()
    async with async_session() as db:
        server, tool = await _make_server_tool(db, tenant_id, "shared")
        db.add_all(
            [
                AgentTool(
                    agent_id=agent_a,
                    tool_id=tool.id,
                    enabled=True,
                    source="user_installed",
                    installed_by_agent_id=agent_a,
                ),
                AgentTool(
                    agent_id=agent_b,
                    tool_id=tool.id,
                    enabled=True,
                    source="system",
                ),
            ]
        )
        await db.commit()
        server_id, tool_id = server.id, tool.id

    result = json.loads(await uninstall_mcp_server(agent_a, server_id))
    assert result["ok"] is True
    assert result["shared_server_preserved"] is True
    assert result["other_agents_affected"] == 0
    assert result["deleted_orphan_tools"] == 0

    async with async_session() as db:
        assert (
            await db.execute(select(MCPServer).where(MCPServer.id == server_id))
        ).scalar_one_or_none() is not None
        assert (
            await db.execute(select(Tool).where(Tool.id == tool_id))
        ).scalar_one_or_none() is not None
        assignments = (
            await db.execute(select(AgentTool).where(AgentTool.tool_id == tool_id))
        ).scalars().all()
        assert [row.agent_id for row in assignments] == [agent_b]


async def test_uninstall_deletes_unreferenced_private_group():
    tenant_id, (agent_id,) = await _make_agents(1)
    async with async_session() as db:
        server, tool = await _make_server_tool(db, tenant_id, "orphan")
        db.add(
            AgentTool(
                agent_id=agent_id,
                tool_id=tool.id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=agent_id,
            )
        )
        await db.commit()
        server_id, tool_id = server.id, tool.id

    result = json.loads(await uninstall_mcp_server(agent_id, server_id))
    assert result["ok"] is True
    assert result["deleted_orphan_tools"] == 1
    assert result["server_deleted"] is True
    assert result["shared_server_preserved"] is False

    async with async_session() as db:
        assert (
            await db.execute(select(MCPServer).where(MCPServer.id == server_id))
        ).scalar_one_or_none() is None
        assert (
            await db.execute(select(Tool).where(Tool.id == tool_id))
        ).scalar_one_or_none() is None


async def test_uninstall_rejects_binding_not_installed_by_current_agent():
    tenant_id, (agent_id,) = await _make_agents(1)
    async with async_session() as db:
        server, tool = await _make_server_tool(db, tenant_id, "managed")
        db.add(AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=True, source="system"))
        await db.commit()
        server_id = server.id

    result = json.loads(await uninstall_mcp_server(agent_id, server_id))
    assert result == {
        "ok": False,
        "error": "not_installed_or_not_removable",
        "mcp_server_id": str(server_id),
    }


async def test_admin_endpoint_uninstalls_complete_mcp_group():
    tenant_id, (agent_id,) = await _make_agents(1)
    async with async_session() as db:
        agent = (
            await db.execute(select(Agent).where(Agent.id == agent_id))
        ).scalar_one()
        user = (
            await db.execute(select(User).where(User.id == agent.creator_id))
        ).scalar_one()
        server, first_tool = await _make_server_tool(db, tenant_id, "group")
        second_tool = Tool(
            name=f"mcp_{server.name}_second",
            display_name="Group: second",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="second",
            source="agent",
        )
        db.add(second_tool)
        await db.flush()
        db.add_all(
            [
                AgentTool(
                    agent_id=agent_id,
                    tool_id=tool.id,
                    enabled=True,
                    source="user_installed",
                    installed_by_agent_id=agent_id,
                )
                for tool in (first_tool, second_tool)
            ]
        )
        await db.commit()
        server_id = server.id
        token = create_access_token(str(user.id), user.role)

    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.delete(
            f"/api/tools/agents/{agent_id}/mcp-servers/{server_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json()["removed_bindings"] == 2
    assert response.json()["server_deleted"] is True
    async with async_session() as db:
        assert (
            await db.execute(select(MCPServer).where(MCPServer.id == server_id))
        ).scalar_one_or_none() is None


async def test_observable_result_masks_only_credentials_and_keeps_diagnostics():
    from app.services.llm.caller import _observable_tool_result

    raw = json.dumps(
        {
            "mcp_servers": [
                {
                    "mcp_server_id": "server-id",
                    "transport": "stdio",
                    "config": {
                        "command": "npx",
                        "args": ["-y", "pkg"],
                        "env": {"API_KEY": "secret", "NODE_ENV": "production"},
                        "mcp_url": "https://mcp.example/path?token=secret&region=cn",
                    },
                    "last_error": "initialize timed out",
                }
            ]
        }
    )
    observable = json.loads(_observable_tool_result("list_installed_mcp_servers", raw))
    server = observable["mcp_servers"][0]
    assert server["mcp_server_id"] == "server-id"
    assert server["config"]["command"] == "npx"
    assert server["config"]["args"] == ["-y", "pkg"]
    assert server["config"]["env"] == {"API_KEY": "******", "NODE_ENV": "production"}
    assert server["config"]["mcp_url"] == (
        "https://mcp.example/path?token=%2A%2A%2A%2A%2A%2A&region=cn"
    )
    assert server["last_error"] == "initialize timed out"


async def test_activity_log_keeps_sanitized_inventory_result():
    _tenant_id, (agent_id,) = await _make_agents(1)
    raw = json.dumps(
        {
            "mcp_servers": [
                {
                    "mcp_server_id": "server-id",
                    "config": {
                        "command": "npx",
                        "api_key": "secret",
                    },
                }
            ]
        }
    )
    with patch(
        "app.services.agent_mcp_lifecycle.list_installed_mcp_servers",
        AsyncMock(return_value=raw),
    ), patch("app.services.activity_logger.log_activity", AsyncMock()) as activity:
        from app.services.agent_tools import execute_tool

        result = await execute_tool(
            "list_installed_mcp_servers",
            {},
            agent_id=agent_id,
            user_id=agent_id,
        )

    assert result == raw
    activity.assert_awaited_once()
    detail = activity.await_args.kwargs["detail"]
    assert "server-id" in detail["result"]
    assert "npx" in detail["result"]
    assert "secret" not in detail["result"]
    assert "******" in detail["result"]


async def test_llm_debug_log_preserves_shape_and_masks_known_credentials():
    from app.services.llm.client import LLMMessage, OpenAICompatibleClient

    raw = json.dumps(
        {
            "mcp_servers": [
                {
                    "mcp_server_id": "server-id",
                    "config": {
                        "command": "npx",
                        "api_key": "secret",
                        "env": {"TOKEN": "env-secret", "MODE": "debug"},
                    },
                }
            ]
        }
    )
    client = OpenAICompatibleClient(
        api_key="unused",
        base_url="https://example.invalid/v1",
        model="test-model",
    )
    with patch("app.services.llm.client.logger.debug") as debug:
        payload = client._build_payload(
            [LLMMessage(role="tool", content=raw, tool_call_id="call-1")],
            tools=None,
            temperature=0,
            max_tokens=10,
        )

    assert payload["messages"][0]["content"] == raw
    serialized = json.dumps(
        {"args": debug.call_args.args, "kwargs": debug.call_args.kwargs},
        default=str,
    )
    assert "server-id" in serialized
    assert "npx" in serialized
    assert "debug" in serialized
    assert "secret" not in serialized
    assert "env-secret" not in serialized
    assert "******" in serialized


async def test_runtime_rechecks_assignment_after_schema_was_built():
    tenant_id, (agent_id,) = await _make_agents(1)
    async with async_session() as db:
        server, tool = await _make_server_tool(db, tenant_id, "frozen")
        exact_name = tool.name
        await db.commit()

    from app.services.agent_tools import _execute_mcp_tool

    with patch("app.services.mcp_client.MCPClient.call_tool", AsyncMock()) as call:
        result = await _execute_mcp_tool(
            exact_name,
            {},
            agent_id=agent_id,
            user_id=agent_id,
        )
    from app.services.llm.failure_outcome import render_message
    assert result == render_message("mcpAccess.unavailable")
    call.assert_not_awaited()


async def test_seeded_inventory_contract_is_visible_to_the_llm():
    expected_names = {
        "list_installed_mcp_servers", "refresh_mcp_server", "uninstall_mcp_server",
    }
    await seed_builtin_tools()
    _tenant_id, (agent_id,) = await _make_agents(1)
    async with async_session() as db:
        rows = (await db.scalars(select(Tool).where(Tool.name.in_(expected_names)))).all()
        assert {row.name for row in rows} == expected_names
        for row in rows:
            assert row.is_default is True
            db.add(AgentTool(agent_id=agent_id, tool_id=row.id, enabled=True))
        await db.commit()

    runtime = {
        item["function"]["name"]: item["function"]
        for item in await get_agent_tools_for_llm(agent_id)
    }
    for row in rows:
        schema = runtime[row.name]["parameters"]
        assert schema == row.parameters_schema
        assert runtime[row.name]["description"] == row.description
        if row.name == "list_installed_mcp_servers":
            assert schema == {"type": "object", "properties": {}, "required": []}
        else:
            assert schema["required"] == ["mcp_server_id"]
            assert set(schema["properties"]) == {"mcp_server_id"}
            assert schema["properties"]["mcp_server_id"]["type"] == "string"
