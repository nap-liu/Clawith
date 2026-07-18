"""Creator-safe inventory and exact self-uninstall for MCP installations."""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.services.agent_mcp_lifecycle import (
    build_installation_key,
    build_mcp_tool_name,
    ensure_owned_agent_tool,
    get_or_create_owned_server,
    list_installed_mcp_servers,
    uninstall_mcp_server,
)

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
        identity = Identity(username=f"u_{suffix}", email=f"u-{suffix}@x.local", password_hash="x")
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
        agents = [Agent(name=f"A{i}_{suffix}", creator_id=user.id, tenant_id=tenant.id) for i in range(count)]
        db.add_all(agents)
        await db.commit()
        return tenant.id, [agent.id for agent in agents]


async def test_installation_key_ignores_secret_values_but_not_shape():
    key_a = build_installation_key("http", url="https://mcp.example/x?token=old&region=cn")
    key_b = build_installation_key("http", url="https://mcp.example/x?region=us&token=new")
    key_c = build_installation_key("http", url="https://mcp.example/x?api_key=new")
    assert key_a == key_b
    assert key_a != key_c
    assert "old" not in key_a and "new" not in key_a
    long_name = build_mcp_tool_name("server", "x" * 200)
    assert len(long_name) == 100
    assert long_name == build_mcp_tool_name("server", "x" * 200)


async def test_inventory_projects_private_config_only_to_owner():
    tenant_id, (owner_id, other_id) = await _make_agents()
    async with async_session() as db:
        server = await get_or_create_owned_server(
            db,
            agent_id=owner_id,
            tenant_id=tenant_id,
            installation_key=build_installation_key("http", explicit_name="private"),
            display_name="Private",
            transport="http",
            url_template="https://secret.example/mcp",
            headers_template={"Authorization": "Bearer private"},
            credential_template="private-key",
        )
        tool = Tool(
            name=f"mcp_{server.name}_run",
            display_name="Private: run",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="run",
            source="agent",
        )
        db.add(tool)
        await db.flush()
        await ensure_owned_agent_tool(db, owner_id, tool.id, config={"route": "owner-only"})
        # Simulate an assignment created by an administrator. It is visible but
        # never grants creator config or self-uninstall rights to another agent.
        db.add(AgentTool(agent_id=other_id, tool_id=tool.id, enabled=True, source="system", config={}))
        await db.commit()
        server_id = server.id

    owner = json.loads(await list_installed_mcp_servers(owner_id))["mcp_servers"][0]
    other = json.loads(await list_installed_mcp_servers(other_id))["mcp_servers"][0]
    assert owner["mcp_server_id"] == str(server_id)
    assert owner["config"]["url"] == "https://secret.example/mcp"
    assert owner["config"]["credential"] == "private-key"
    assert owner["config"]["runtime"] == {"route": "owner-only"}
    assert owner["removable"] is True
    assert "config" not in other
    assert other["removable"] is False


async def test_concurrent_install_converges_to_one_private_server():
    tenant_id, (agent_id,) = await _make_agents(1)
    installation_key = build_installation_key("http", explicit_name="concurrent")

    async def install_once(index):
        async with async_session() as db:
            server = await get_or_create_owned_server(
                db,
                agent_id=agent_id,
                tenant_id=tenant_id,
                installation_key=installation_key,
                display_name="Concurrent",
                transport="http",
                url_template=f"https://example.invalid/mcp?token={index}",
                credential_template=f"secret-{index}",
            )
            server_id = server.id
            await db.commit()
            return server_id

    ids = await asyncio.gather(*(install_once(i) for i in range(12)))
    assert len(set(ids)) == 1
    async with async_session() as db:
        servers = (
            await db.execute(
                select(MCPServer).where(
                    MCPServer.owner_agent_id == agent_id,
                    MCPServer.installation_key == installation_key,
                )
            )
        ).scalars().all()
        assert len(servers) == 1
        overrides = (
            await db.execute(
                select(MCPServerOverride).where(MCPServerOverride.mcp_server_id == servers[0].id)
            )
        ).scalars().all()
        assert len(overrides) == 1


async def test_exact_uninstall_deletes_private_tree_and_is_idempotent():
    tenant_id, (agent_id,) = await _make_agents(1)
    async with async_session() as db:
        server = await get_or_create_owned_server(
            db,
            agent_id=agent_id,
            tenant_id=tenant_id,
            installation_key=build_installation_key("stdio", command="npx", args=["-y", "pkg"]),
            display_name="pkg",
            transport="stdio",
            command_template="npx",
            args_template=["-y", "pkg"],
            env_template={"TOKEN": "secret"},
        )
        tool = Tool(
            name=f"mcp_{server.name}_run",
            display_name="pkg: run",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="run",
            source="agent",
        )
        db.add(tool)
        await db.flush()
        await ensure_owned_agent_tool(db, agent_id, tool.id)
        await db.commit()
        server_id = server.id

    class _Settings:
        SANDBOX_API_URL = "http://sandbox"
        SANDBOX_API_KEY = "key"

    cleanup = AsyncMock()
    with patch("app.services.agent_mcp_lifecycle.get_settings", return_value=_Settings()), patch(
        "app.services.sandbox_mcp_host.SandboxMcpHost.deregister_prefix", cleanup
    ):
        first = json.loads(await uninstall_mcp_server(agent_id, server_id))
    assert first["ok"] is True and first["private_server_deleted"] is True
    assert first["effective"] == "immediate"
    cleanup.assert_awaited_once()

    async with async_session() as db:
        assert (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none() is None
        assert (await db.execute(select(MCPServerOverride).where(MCPServerOverride.mcp_server_id == server_id))).scalars().all() == []
        assert (await db.execute(select(Tool).where(Tool.mcp_server_id == server_id))).scalars().all() == []

    second = json.loads(await uninstall_mcp_server(agent_id, server_id))
    assert second == {"ok": True, "state": "already_uninstalled", "mcp_server_id": str(server_id)}


async def test_shared_server_uninstall_only_detaches_creator_binding():
    tenant_id, (agent_a, agent_b) = await _make_agents()
    async with async_session() as db:
        server = MCPServer(
            tenant_id=tenant_id,
            name=f"shared-{uuid.uuid4().hex[:8]}",
            display_name="Shared",
            base_url_template="https://shared.example/mcp",
            headers_template={},
            transport="http",
        )
        db.add(server)
        await db.flush()
        tool = Tool(
            name=f"mcp_{server.name}_run",
            display_name="Shared: run",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="run",
        )
        db.add(tool)
        await db.flush()
        db.add_all([
            AgentTool(
                agent_id=agent_a,
                tool_id=tool.id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=agent_a,
            ),
            AgentTool(agent_id=agent_b, tool_id=tool.id, enabled=True, source="system"),
        ])
        await db.commit()
        server_id, tool_id = server.id, tool.id

    result = json.loads(await uninstall_mcp_server(agent_a, server_id))
    assert result["ok"] is True and result["private_server_deleted"] is False
    async with async_session() as db:
        assert (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
        assert (await db.execute(select(Tool).where(Tool.id == tool_id))).scalar_one_or_none()
        remaining = (await db.execute(select(AgentTool).where(AgentTool.tool_id == tool_id))).scalars().all()
        assert [row.agent_id for row in remaining] == [agent_b]


async def test_private_result_is_redacted_before_persistence():
    from app.services.llm.caller import _public_tool_result

    raw = json.dumps({"mcp_servers": [{"mcp_server_id": "x", "config": {"credential": "secret"}}]})
    public = json.loads(_public_tool_result("list_installed_mcp_servers", raw))
    assert public == {"mcp_servers": [{"mcp_server_id": "x"}]}


async def test_private_inventory_result_is_never_written_to_activity_log():
    _tenant_id, (agent_id,) = await _make_agents(1)
    secret_result = json.dumps({
        "mcp_servers": [{
            "mcp_server_id": str(uuid.uuid4()),
            "config": {
                "url": "https://secret.example/mcp?token=leak-me-not",
                "credential": "creator-secret",
            },
        }]
    })

    with patch(
        "app.services.agent_mcp_lifecycle.list_installed_mcp_servers",
        AsyncMock(return_value=secret_result),
    ), patch("app.services.activity_logger.log_activity", AsyncMock()) as activity:
        from app.services.agent_tools import execute_tool

        result = await execute_tool(
            "list_installed_mcp_servers",
            {},
            agent_id=agent_id,
            user_id=agent_id,
        )

    assert result == secret_result
    activity.assert_awaited_once()
    args, kwargs = activity.await_args
    serialized_log = json.dumps({"args": args, "kwargs": kwargs}, default=str)
    assert "secret.example" not in serialized_log
    assert "creator-secret" not in serialized_log
    assert "leak-me-not" not in serialized_log
    assert "result" not in kwargs["detail"]


async def test_llm_debug_log_never_serializes_private_tool_result():
    from app.services.llm.client import LLMMessage, OpenAICompatibleClient

    secret = (
        '{"mcp_servers":[{"config":{"url":"https://secret.example/mcp?token=leak-me-not",'
        '"credential":"creator-secret","env":{"TOKEN":"env-secret"}}}]}'
    )
    client = OpenAICompatibleClient(
        api_key="unused",
        base_url="https://example.invalid/v1",
        model="test-model",
    )
    with patch("app.services.llm.client.logger.debug") as debug:
        payload = client._build_payload(
            [LLMMessage(role="tool", content=secret, tool_call_id="call-1")],
            tools=None,
            temperature=0,
            max_tokens=10,
        )

    assert payload["messages"][0]["content"] == secret
    serialized_log = json.dumps(
        {"args": debug.call_args.args, "kwargs": debug.call_args.kwargs},
        default=str,
    )
    for forbidden in ("secret.example", "leak-me-not", "creator-secret", "env-secret"):
        assert forbidden not in serialized_log
    assert "message_count" in serialized_log


async def test_runtime_rechecks_assignment_after_schema_was_built():
    """An exact tool name from a frozen turn schema cannot run after uninstall."""
    tenant_id, (agent_id,) = await _make_agents(1)
    async with async_session() as db:
        server = MCPServer(
            tenant_id=tenant_id,
            name=f"frozen-{uuid.uuid4().hex[:8]}",
            display_name="Frozen",
            base_url_template="https://example.invalid/mcp",
            headers_template={},
            transport="http",
        )
        db.add(server)
        await db.flush()
        tool = Tool(
            name=f"mcp_{server.name}_run",
            display_name="Frozen: run",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="run",
        )
        db.add(tool)
        await db.flush()
        exact_name = tool.name
        # No live AgentTool row: this mirrors a tool already removed earlier in
        # the same LLM turn while its initial schema still contains exact_name.
        await db.commit()

    from app.services.agent_tools import _execute_mcp_tool

    with patch("app.services.mcp_client.MCPClient.call_tool", AsyncMock()) as call:
        result = await _execute_mcp_tool(exact_name, {}, agent_id=agent_id, user_id=agent_id)
    assert "no longer installed" in result
    call.assert_not_awaited()


async def test_smithery_import_is_private_idempotent_and_creator_visible():
    _tenant_id, (agent_id,) = await _make_agents(1)

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
                return _Response({
                    "servers": [{
                        "qualifiedName": "vendor/example",
                        "displayName": "Example",
                        "description": "Example MCP",
                        "remote": True,
                    }]
                })
            return _Response({
                "deploymentUrl": "https://vendor-example.run.tools",
                "tools": [{
                    "name": "lookup",
                    "description": "Lookup",
                    "inputSchema": {"type": "object", "properties": {}},
                }],
            })

        async def post(self, url, **kwargs):
            return _Response(text='data: {"result":{"tools":[{"name":"lookup","description":"Lookup","inputSchema":{"type":"object","properties":{}}}]}}\n')

    connection = {
        "namespace": "creator-ns",
        "connection_id": "creator-conn",
        "auth_url": None,
    }
    with patch("app.services.resource_discovery.httpx.AsyncClient", _Client), patch(
        "app.services.resource_discovery._get_smithery_api_key", AsyncMock(return_value="smithery-secret")
    ), patch(
        "app.services.resource_discovery._ensure_smithery_connection", AsyncMock(return_value=connection)
    ):
        from app.services.resource_discovery import import_mcp_from_smithery

        first = await import_mcp_from_smithery("vendor/example", agent_id)
        second = await import_mcp_from_smithery("vendor/example", agent_id)

    assert "MCP Server ID" in first and "MCP Server ID" in second
    async with async_session() as db:
        servers = (
            await db.execute(select(MCPServer).where(MCPServer.owner_agent_id == agent_id))
        ).scalars().all()
        assert len(servers) == 1
        server = servers[0]
        assert server.base_url_template == ""
        override = (
            await db.execute(
                select(MCPServerOverride).where(
                    MCPServerOverride.mcp_server_id == server.id,
                    MCPServerOverride.scope_id == agent_id,
                )
            )
        ).scalar_one()
        assert override.url_template == "https://vendor-example.run.tools"
        assert override.credential_template == "smithery-secret"
        assignments = (
            await db.execute(
                select(AgentTool)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(AgentTool.agent_id == agent_id, Tool.mcp_server_id == server.id)
            )
        ).scalars().all()
        assert len(assignments) == 1
        assert assignments[0].config == {
            "smithery_namespace": "creator-ns",
            "smithery_connection_id": "creator-conn",
        }

    inventory = json.loads(await list_installed_mcp_servers(agent_id))["mcp_servers"]
    assert inventory[0]["config"]["credential"] == "smithery-secret"
