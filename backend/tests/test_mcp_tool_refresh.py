"""Manual MCP tool refresh for global and Agent-scoped configurations."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from mcp_tool_refresh_support import _isolate, _make_agent

pytestmark = pytest.mark.asyncio


async def test_agent_refresh_uses_override_and_preserves_existing_assignment(monkeypatch):
    user, agent, server = await _make_agent()
    async with async_session() as db:
        expected_phone = await db.scalar(
            select(Identity.phone)
            .join(User, User.identity_id == Identity.id)
            .where(User.id == user.id)
        )
        db.add(
            MCPServerOverride(
                mcp_server_id=server.id,
                scope_type="agent",
                scope_id=agent.id,
                url_template="https://agent.example/mcp",
                headers_template={
                    "X-Agent": "yes",
                    "X-Phone": "${user.phone}",
                    "X-Session": "${session.id}",
                },
            )
        )
        existing = Tool(
            name=f"mcp_{server.name}_run",
            display_name="Run",
            description="old",
            type="mcp",
            category="mcp",
            parameters_schema={},
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="run",
            tenant_id=server.tenant_id,
            source="agent",
        )
        db.add(existing)
        await db.flush()
        db.add(
            AgentTool(
                agent_id=agent.id,
                tool_id=existing.id,
                enabled=False,
                source="user_installed",
                installed_by_agent_id=agent.id,
                config={"command": "existing-config"},
            )
        )
        await db.commit()

    captured: dict = {}

    class FakeClient:
        server_instructions = "Use refreshed tools."
        server_info = {"name": "fake"}

        def __init__(self, server_url, api_key=None, headers=None):
            captured.update(url=server_url, api_key=api_key, headers=headers)

        async def list_tools(self):
            return [
                {
                    "name": "run",
                    "description": "new description",
                    "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}},
                },
                {
                    "name": "new_tool",
                    "description": "new tool",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ]

    import app.services.mcp_refresh_service as refresh_service

    monkeypatch.setattr(refresh_service, "MCPClient", FakeClient)
    async with async_session() as db:
        result = await refresh_service.refresh_mcp_server_tools(
            db,
            server.id,
            agent_id=agent.id,
            user_id=user.id,
            session_id="session-123",
            assign_to_agent=True,
        )
        await db.commit()

    assert captured == {
        "url": "https://agent.example/mcp",
        "api_key": None,
        "headers": {
            "X-Agent": "yes",
            "X-Phone": expected_phone,
            "X-Session": "session-123",
        },
    }
    assert result.discovered == 2
    assert result.created == 1
    assert result.updated == 1
    assert result.assigned == 1

    async with async_session() as db:
        tools = (
            await db.execute(
                select(Tool)
                .where(Tool.mcp_server_id == server.id)
                .order_by(Tool.mcp_tool_name)
            )
        ).scalars().all()
        assert [tool.mcp_tool_name for tool in tools] == ["new_tool", "run"]
        run = next(tool for tool in tools if tool.mcp_tool_name == "run")
        new_tool = next(tool for tool in tools if tool.mcp_tool_name == "new_tool")
        assert run.description == "new description"
        assignments = (
            await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == agent.id,
                    AgentTool.tool_id.in_([run.id, new_tool.id]),
                )
            )
        ).scalars().all()
        by_tool = {assignment.tool_id: assignment for assignment in assignments}
        assert by_tool[run.id].enabled is False
        assert by_tool[run.id].config == {"command": "existing-config"}
        assert by_tool[new_tool.id].enabled is True
        assert by_tool[new_tool.id].source == "user_installed"
        assert by_tool[new_tool.id].installed_by_agent_id == agent.id
        assert by_tool[new_tool.id].config == {"command": "existing-config"}


async def test_global_refresh_creates_tools_without_agent_assignments(monkeypatch):
    user, _agent, server = await _make_agent()
    async with async_session() as db:
        server_row = (
            await db.execute(select(MCPServer).where(MCPServer.id == server.id))
        ).scalar_one()
        server_row.created_by_user_id = user.id
        await db.commit()

    class FakeClient:
        server_instructions = None
        server_info = {"name": "fake"}

        def __init__(self, server_url, api_key=None, headers=None):
            assert server_url == "https://global.example/mcp"

        async def list_tools(self):
            return [
                {
                    "name": "global_tool",
                    "description": "global",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

    import app.services.mcp_refresh_service as refresh_service

    monkeypatch.setattr(refresh_service, "MCPClient", FakeClient)
    async with async_session() as db:
        result = await refresh_service.refresh_mcp_server_tools(db, server.id)
        await db.commit()

    assert result.created == 1
    assert result.assigned == 0
    async with async_session() as db:
        tool = (
            await db.execute(
                select(Tool).where(
                    Tool.mcp_server_id == server.id,
                    Tool.mcp_tool_name == "global_tool",
                )
            )
        ).scalar_one()
        assert (
            await db.execute(select(AgentTool).where(AgentTool.tool_id == tool.id))
        ).scalar_one_or_none() is None


async def test_stdio_refresh_uses_sandbox_hub_and_cleans_up(monkeypatch):
    user, agent, server = await _make_agent()
    async with async_session() as db:
        row = (
            await db.execute(select(MCPServer).where(MCPServer.id == server.id))
        ).scalar_one()
        row.transport = "stdio"
        row.base_url_template = ""
        row.command_template = "npx"
        row.args_template = ["-y", "pkg"]
        row.env_template = {"TOKEN": "value"}
        seed_tool = Tool(
            name=f"mcp_{row.name}_seed",
            display_name="Seed",
            type="mcp",
            category="mcp",
            mcp_server_id=row.id,
            mcp_server_name=row.name,
            mcp_tool_name="seed",
            source="agent",
            tenant_id=row.tenant_id,
        )
        db.add(seed_tool)
        await db.flush()
        db.add(
            AgentTool(
                agent_id=agent.id,
                tool_id=seed_tool.id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=agent.id,
            )
        )
        await db.commit()

    class Settings:
        SANDBOX_API_URL = "http://sandbox:8080"
        SANDBOX_API_KEY = "key"

    import app.services.mcp_refresh_service as refresh_service

    host = MagicMock()
    host.ensure_registered = AsyncMock(return_value="entry__refresh")
    host.deregister = AsyncMock()
    hub = MagicMock()
    hub.list_tools = AsyncMock(
        return_value=[
            {"name": "seed", "description": "updated", "inputSchema": {}},
            {"name": "next", "description": "next", "inputSchema": {}},
        ]
    )
    monkeypatch.setattr(refresh_service, "get_settings", lambda: Settings())
    monkeypatch.setattr(refresh_service, "SandboxMcpHost", lambda *_: host)
    monkeypatch.setattr(refresh_service, "SandboxMcpHubClient", lambda *_: hub)
    monkeypatch.setattr(refresh_service, "_agent_workspace_root", lambda _agent_id: MagicMock(
        resolve=lambda: "/data/agents/test",
        mkdir=lambda **_kwargs: None,
    ))

    async with async_session() as db:
        result = await refresh_service.refresh_mcp_server_tools(
            db,
            server.id,
            agent_id=agent.id,
            user_id=user.id,
            assign_to_agent=True,
        )
        await db.commit()

    assert result.discovered == 2
    host.ensure_registered.assert_awaited_once()
    hub.list_tools.assert_awaited_once_with("entry__refresh")
    host.deregister.assert_awaited_once_with("entry__refresh")


@pytest.fixture
async def client():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_refresh_endpoint_returns_counts(client):
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        identity = Identity(
            username=f"admin_{suffix}",
            email=f"admin-{suffix}@x.local",
            password_hash="x",
            is_platform_admin=True,
        )
        db.add(identity)
        await db.flush()
        admin = User(
            identity_id=identity.id,
            display_name="Admin",
            role="platform_admin",
            is_active=True,
        )
        db.add(admin)
        server = MCPServer(
            name=f"api-refresh-{suffix}",
            display_name="API Refresh",
            base_url_template="https://api.example/mcp",
            headers_template={},
        )
        db.add(server)
        await db.commit()
        token = create_access_token(str(admin.id), "platform_admin")

    class FakeClient:
        server_instructions = None
        server_info = {}

        def __init__(self, *_args, **_kwargs):
            pass

        async def list_tools(self):
            return [{"name": "one", "description": "one", "inputSchema": {}}]

    with patch("app.services.mcp_refresh_service.MCPClient", FakeClient):
        response = await client.post(
            f"/api/admin/mcp-servers/{server.id}/refresh-tools",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "discovered": 1,
        "created": 1,
        "updated": 0,
        "assigned": 0,
        "effective": "next_turn",
    }


async def test_agent_refresh_endpoint_uses_agent_scope_and_assigns_new_tools(client):
    user, agent, server = await _make_agent()
    async with async_session() as db:
        seed = Tool(
            name=f"mcp_{server.name}_seed",
            display_name="Seed",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="seed",
            source="agent",
            tenant_id=server.tenant_id,
        )
        db.add(seed)
        await db.flush()
        db.add(
            AgentTool(
                agent_id=agent.id,
                tool_id=seed.id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=agent.id,
            )
        )
        db.add(
            MCPServerOverride(
                mcp_server_id=server.id,
                scope_type="agent",
                scope_id=agent.id,
                url_template="https://agent-api.example/mcp",
            )
        )
        await db.commit()
    token = create_access_token(str(user.id), user.role)
    requested_urls: list[str] = []

    class FakeClient:
        server_instructions = None
        server_info = {}

        def __init__(self, server_url, **_kwargs):
            requested_urls.append(server_url)

        async def list_tools(self):
            return [{"name": "next", "description": "next", "inputSchema": {}}]

    with patch("app.services.mcp_refresh_service.MCPClient", FakeClient):
        response = await client.post(
            f"/api/admin/mcp-servers/{server.id}/refresh-tools?agent_id={agent.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert requested_urls == ["https://agent-api.example/mcp"]
    assert response.json() == {
        "success": True,
        "discovered": 1,
        "created": 1,
        "updated": 0,
        "assigned": 1,
        "effective": "next_turn",
    }


async def test_agent_refresh_endpoint_rejects_enterprise_server_before_discovery(client):
    user, agent, server = await _make_agent()
    async with async_session() as db:
        server_row = (
            await db.execute(select(MCPServer).where(MCPServer.id == server.id))
        ).scalar_one()
        server_row.created_by_user_id = user.id
        tool = Tool(
            name=f"mcp_{server.name}_enterprise",
            display_name="Enterprise",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="enterprise",
            source="admin",
            tenant_id=server.tenant_id,
        )
        db.add(tool)
        await db.flush()
        db.add(
            AgentTool(
                agent_id=agent.id,
                tool_id=tool.id,
                enabled=True,
                source="system",
            )
        )
        await db.commit()

    token = create_access_token(str(user.id), user.role)
    with patch("app.services.mcp_refresh_service.MCPClient") as mcp_client:
        response = await client.post(
            f"/api/admin/mcp-servers/{server.id}/refresh-tools?agent_id={agent.id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 403
    assert "administrator global refresh" in response.json()["detail"]
    mcp_client.assert_not_called()


async def test_agent_tool_refresh_splits_historical_self_installed_server():
    user, agent, server = await _make_agent()
    async with async_session() as db:
        other_agent = Agent(
            name=f"Other_{uuid.uuid4().hex[:8]}",
            creator_id=user.id,
            tenant_id=agent.tenant_id,
        )
        db.add(other_agent)
        tool = Tool(
            name=f"mcp_{server.name}_shared",
            display_name="Shared",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="shared",
            source="agent",
            tenant_id=server.tenant_id,
        )
        db.add(tool)
        await db.flush()
        db.add_all(
            [
                AgentTool(
                    agent_id=agent.id,
                    tool_id=tool.id,
                    enabled=True,
                    source="user_installed",
                    installed_by_agent_id=agent.id,
                ),
                AgentTool(
                    agent_id=other_agent.id,
                    tool_id=tool.id,
                    enabled=True,
                    source="user_installed",
                    installed_by_agent_id=other_agent.id,
                ),
            ]
        )
        await db.commit()

    from app.services.agent_mcp_lifecycle import refresh_mcp_server

    class FakeClient:
        server_instructions = None

        def __init__(self, *_args, **_kwargs):
            pass

        async def list_tools(self):
            return [
                {
                    "name": "shared",
                    "description": "refreshed",
                    "inputSchema": {"type": "object", "properties": {}},
                }
            ]

    with patch("app.services.mcp_refresh_service.MCPClient", FakeClient):
        result = json.loads(
            await refresh_mcp_server(
                agent.id,
                server.id,
                user_id=user.id,
                session_id="session-123",
            )
        )

    assert result["ok"] is True
    assert result["mcp_server_id"] != str(server.id)

    async with async_session() as db:
        current_server_ids = (
            await db.execute(
                select(Tool.mcp_server_id)
                .join(AgentTool, AgentTool.tool_id == Tool.id)
                .where(AgentTool.agent_id == agent.id)
            )
        ).scalars().all()
        other_server_ids = (
            await db.execute(
                select(Tool.mcp_server_id)
                .join(AgentTool, AgentTool.tool_id == Tool.id)
                .where(AgentTool.agent_id == other_agent.id)
            )
        ).scalars().all()

    assert set(current_server_ids) == {uuid.UUID(result["mcp_server_id"])}
    assert set(other_server_ids) == {server.id}


async def test_agent_tool_refresh_reuses_agent_scope_service():
    user, agent, server = await _make_agent()
    async with async_session() as db:
        tool = Tool(
            name=f"mcp_{server.name}_seed",
            display_name="Seed",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="seed",
            source="agent",
            tenant_id=server.tenant_id,
        )
        db.add(tool)
        await db.flush()
        db.add(
            AgentTool(
                agent_id=agent.id,
                tool_id=tool.id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=agent.id,
            )
        )
        await db.commit()

    from app.services.agent_mcp_lifecycle import refresh_mcp_server
    from app.services.mcp_refresh_service import MCPToolRefreshResult

    refreshed = MCPToolRefreshResult(discovered=2, created=1, updated=1, assigned=1)
    with patch(
        "app.services.agent_mcp_lifecycle.refresh_mcp_server_tools",
        AsyncMock(return_value=refreshed),
    ) as refresh:
        result = json.loads(
            await refresh_mcp_server(
                agent.id,
                server.id,
                user_id=user.id,
                session_id="session-123",
            )
        )

    assert result == {
        "ok": True,
        "mcp_server_id": str(server.id),
        "discovered": 2,
        "created": 1,
        "updated": 1,
        "assigned": 1,
        "effective": "next_turn",
    }
    refresh.assert_awaited_once()
    assert refresh.await_args.kwargs == {
        "agent_id": agent.id,
        "user_id": user.id,
        "session_id": "session-123",
        "assign_to_agent": True,
    }


async def test_shared_agent_refresh_failure_rolls_back_clone_and_returns_original_id():
    user, agent, server = await _make_agent()
    async with async_session() as db:
        other_agent = Agent(
            name=f"Other_{uuid.uuid4().hex[:8]}",
            creator_id=user.id,
            tenant_id=agent.tenant_id,
        )
        db.add(other_agent)
        tool = Tool(
            name=f"mcp_{server.name}_shared_failure",
            display_name="Shared failure",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="shared_failure",
            source="agent",
            tenant_id=server.tenant_id,
        )
        db.add(tool)
        await db.flush()
        db.add_all(
            [
                AgentTool(
                    agent_id=agent.id,
                    tool_id=tool.id,
                    enabled=True,
                    source="user_installed",
                    installed_by_agent_id=agent.id,
                ),
                AgentTool(
                    agent_id=other_agent.id,
                    tool_id=tool.id,
                    enabled=True,
                    source="user_installed",
                    installed_by_agent_id=other_agent.id,
                ),
            ]
        )
        await db.commit()

    from app.services.agent_mcp_lifecycle import (
        list_installed_mcp_servers,
        refresh_mcp_server,
    )

    with patch(
        "app.services.agent_mcp_lifecycle.refresh_mcp_server_tools",
        AsyncMock(side_effect=RuntimeError("shared refresh failure")),
    ):
        result = json.loads(
            await refresh_mcp_server(
                agent.id,
                server.id,
                user_id=user.id,
                session_id="session-123",
            )
        )

    assert result == {
        "ok": False,
        "error": "refresh_failed",
        "mcp_server_id": str(server.id),
        "detail": "RuntimeError: shared refresh failure",
    }

    async with async_session() as db:
        server_ids = (
            await db.execute(
                select(MCPServer.id).where(MCPServer.name.like(f"{server.name}%"))
            )
        ).scalars().all()
        agent_server_ids = (
            await db.execute(
                select(Tool.mcp_server_id)
                .join(AgentTool, AgentTool.tool_id == Tool.id)
                .where(AgentTool.agent_id == agent.id)
            )
        ).scalars().all()
        other_server_ids = (
            await db.execute(
                select(Tool.mcp_server_id)
                .join(AgentTool, AgentTool.tool_id == Tool.id)
                .where(AgentTool.agent_id == other_agent.id)
            )
        ).scalars().all()

    assert server_ids == [server.id]
    assert set(agent_server_ids) == {server.id}
    assert set(other_server_ids) == {server.id}
    inventory = json.loads(await list_installed_mcp_servers(agent.id))
    assert inventory["mcp_servers"][0]["mcp_server_id"] == str(server.id)


async def test_anonymous_refresh_does_not_fall_back_to_agent_creator():
    user, agent, server = await _make_agent()
    async with async_session() as db:
        db.add(
            MCPServerOverride(
                mcp_server_id=server.id,
                scope_type="agent",
                scope_id=agent.id,
                headers_template={"X-Phone": "${user.phone}"},
            )
        )
        tool = Tool(
            name=f"mcp_{server.name}_anonymous",
            display_name="Anonymous",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="anonymous",
            source="agent",
            tenant_id=server.tenant_id,
        )
        db.add(tool)
        await db.flush()
        db.add(
            AgentTool(
                agent_id=agent.id,
                tool_id=tool.id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=agent.id,
            )
        )
        await db.commit()

    from app.services.agent_mcp_lifecycle import refresh_mcp_server

    with patch("app.services.mcp_refresh_service.MCPClient") as mcp_client:
        result = json.loads(
            await refresh_mcp_server(
                agent.id,
                server.id,
                user_id=None,
                session_id="anonymous-session",
            )
        )

    assert result["ok"] is False
    assert result["error"] == "refresh_failed"
    assert result["mcp_server_id"] == str(server.id)
    assert "UnknownPlaceholderError" in result["detail"]
    assert "${user.phone}" in result["detail"]
    mcp_client.assert_not_called()


async def test_agent_tool_refresh_preserves_original_error_after_rollback():
    user, agent, server = await _make_agent()
    async with async_session() as db:
        tool = Tool(
            name=f"mcp_{server.name}_seed",
            display_name="Seed",
            type="mcp",
            category="mcp",
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="seed",
            source="agent",
            tenant_id=server.tenant_id,
        )
        db.add(tool)
        await db.flush()
        db.add(
            AgentTool(
                agent_id=agent.id,
                tool_id=tool.id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=agent.id,
            )
        )
        await db.commit()

    from app.services.agent_mcp_lifecycle import refresh_mcp_server

    with patch(
        "app.services.agent_mcp_lifecycle.refresh_mcp_server_tools",
        AsyncMock(side_effect=RuntimeError("primary refresh failure")),
    ):
        result = json.loads(
            await refresh_mcp_server(
                agent.id,
                server.id,
                user_id=user.id,
                session_id="session-123",
            )
        )

    assert result == {
        "ok": False,
        "error": "refresh_failed",
        "mcp_server_id": str(server.id),
        "detail": "RuntimeError: primary refresh failure",
    }
