"""Observable company MCP import and model-facing naming behavior."""

import importlib.util
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from app.core.security import create_access_token
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.services.mcp_import_service import MCPDiscovery

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.fixture
async def client():
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
        yield value


async def _make_tenant_and_user(role: str = "org_admin") -> tuple[Tenant, User, str]:
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        tenant = Tenant(name=f"Tenant {suffix}", slug=f"tenant-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"user_{suffix}",
            email=f"user_{suffix}@example.test",
            password_hash="x",
            is_platform_admin=role == "platform_admin",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="Import admin",
            role=role,
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.commit()
        return tenant, user, create_access_token(str(user.id), role)


async def _make_user_in_tenant(tenant_id: uuid.UUID, role: str) -> tuple[User, str]:
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        identity = Identity(
            username=f"user_{suffix}",
            email=f"user_{suffix}@example.test",
            password_hash="x",
            is_platform_admin=role == "platform_admin",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="MCP user",
            role=role,
            is_active=True,
            tenant_id=tenant_id,
        )
        db.add(user)
        await db.commit()
        return user, create_access_token(str(user.id), role)


async def test_import_discovers_once_and_atomically_persists_large_catalog(client, monkeypatch):
    tenant, _user, token = await _make_tenant_and_user()
    calls = 0

    async def discover(_draft):
        nonlocal calls
        calls += 1
        return MCPDiscovery(
            tools=[
                {
                    "name": f"remote_tool_{index}",
                    "description": f"Remote tool {index}",
                    "inputSchema": {"type": "object", "properties": {}},
                }
                for index in range(266)
            ],
            instructions="Use the remote catalog.",
        )

    monkeypatch.setattr("app.api.mcp_server_import.discover_mcp_import", discover)
    response = await client.post(
        "/api/admin/mcp-servers/import",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "display_name": "Company Knowledge Search",
            "tenant_id": str(tenant.id),
            "transport": "http",
            "base_url_template": "https://mcp.example.test/api",
        },
    )

    assert response.status_code == 201, response.text
    assert calls == 1
    assert response.json()["created"] == 266
    server_id = uuid.UUID(response.json()["server"]["id"])
    async with async_session() as db:
        server = await db.get(MCPServer, server_id)
        tools = (await db.execute(select(Tool).where(Tool.mcp_server_id == server_id))).scalars().all()
    assert server is not None
    assert server.display_name == "Company Knowledge Search"
    assert len(tools) == 266
    assert len({tool.name for tool in tools}) == 266
    assert all(tool.mcp_server_id == server_id for tool in tools)


async def test_cross_tenant_import_is_rejected_before_discovery(client, monkeypatch):
    _tenant, _user, token = await _make_tenant_and_user()
    foreign_tenant, _foreign_user, _foreign_token = await _make_tenant_and_user()
    calls = 0

    async def discover(_draft):
        nonlocal calls
        calls += 1
        return MCPDiscovery(tools=[], instructions=None)

    monkeypatch.setattr("app.api.mcp_server_import.discover_mcp_import", discover)
    response = await client.post(
        "/api/admin/mcp-servers/import",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "display_name": "Forbidden",
            "tenant_id": str(foreign_tenant.id),
            "transport": "http",
            "base_url_template": "https://mcp.example.test/forbidden",
        },
    )
    assert response.status_code == 403
    assert calls == 0


async def test_failed_discovery_leaves_no_partial_group(client, monkeypatch):
    tenant, _user, token = await _make_tenant_and_user()

    async def discover(_draft):
        raise RuntimeError("remote unavailable")

    monkeypatch.setattr("app.api.mcp_server_import.discover_mcp_import", discover)
    response = await client.post(
        "/api/admin/mcp-servers/import",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "display_name": "Unavailable group",
            "tenant_id": str(tenant.id),
            "transport": "http",
            "base_url_template": "https://mcp.example.test/unavailable",
        },
    )
    assert response.status_code == 422
    async with async_session() as db:
        servers = (
            (await db.execute(select(MCPServer).where(MCPServer.display_name == "Unavailable group"))).scalars().all()
        )
    assert servers == []


async def test_empty_discovery_does_not_create_a_group(client, monkeypatch):
    tenant, _user, token = await _make_tenant_and_user()

    async def discover(_draft):
        return MCPDiscovery(tools=[], instructions=None)

    monkeypatch.setattr("app.api.mcp_server_import.discover_mcp_import", discover)
    response = await client.post(
        "/api/admin/mcp-servers/import",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "display_name": "Empty group",
            "tenant_id": str(tenant.id),
            "transport": "http",
            "base_url_template": "https://mcp.example.test/empty",
        },
    )
    assert response.status_code == 422
    async with async_session() as db:
        assert await db.scalar(select(MCPServer.id).where(MCPServer.display_name == "Empty group")) is None


async def test_company_server_masks_headers_and_requires_company_admin(client):
    tenant, org_admin, org_token = await _make_tenant_and_user()
    _agent_admin, agent_admin_token = await _make_user_in_tenant(tenant.id, "agent_admin")
    member, member_token = await _make_user_in_tenant(tenant.id, "member")
    _platform_user, platform_token = await _make_user_in_tenant(tenant.id, "platform_admin")
    async with async_session() as db:
        server = MCPServer(
            tenant_id=tenant.id,
            name=f"company-{uuid.uuid4().hex[:8]}",
            display_name="Company MCP",
            base_url_template="https://mcp.example.test/secure",
            headers_template={
                "Authorization": "Bearer runtime-value",
                "X-API-Key": "runtime-value",
                "X-Trace": "visible",
                "X-Auth-Template": "Bearer ${user.email}",
            },
            created_by_user_id=org_admin.id,
        )
        agent = Agent(name="Header viewer", creator_id=member.id, tenant_id=tenant.id)
        db.add_all([server, agent])
        await db.flush()
        tool = Tool(
            name=f"mcp_secure_{uuid.uuid4().hex[:8]}",
            display_name="Secure tool",
            type="mcp",
            category="mcp",
            parameters_schema={},
            source="admin",
            tenant_id=tenant.id,
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_tool_name="secure",
        )
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        server_id = server.id
        agent_id = agent.id

    detail = await client.get(
        f"/api/admin/mcp-servers/{server_id}?agent_id={agent_id}",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert detail.status_code == 200, detail.text
    headers = detail.json()["headers_template"]
    assert headers["Authorization"] == "***"
    assert headers["X-API-Key"] == "***"
    assert headers["X-Trace"] == "visible"
    assert headers["X-Auth-Template"] == "Bearer ${user.email}"

    denied = await client.patch(
        f"/api/admin/mcp-servers/{server_id}",
        headers={"Authorization": f"Bearer {agent_admin_token}"},
        json={"display_name": "Forbidden rename"},
    )
    assert denied.status_code == 403

    update = await client.patch(
        f"/api/admin/mcp-servers/{server_id}",
        headers={"Authorization": f"Bearer {org_token}"},
        json={
            "display_name": "Company MCP renamed",
            "headers_template": {**headers, "X-Trace": "updated"},
        },
    )
    assert update.status_code == 200, update.text
    assert update.json()["headers_template"]["Authorization"] == "***"

    platform_update = await client.patch(
        f"/api/admin/mcp-servers/{server_id}",
        headers={"Authorization": f"Bearer {platform_token}"},
        json={"display_name": "Platform rename"},
    )
    assert platform_update.status_code == 200, platform_update.text

    async with async_session() as db:
        stored = await db.get(MCPServer, server_id)
        assert stored.headers_template["Authorization"] == "Bearer runtime-value"
        assert stored.headers_template["X-API-Key"] == "runtime-value"
        assert stored.headers_template["X-Trace"] == "updated"


async def test_backfill_keeps_same_url_groups_isolated_and_is_idempotent():
    tenant, user, _token = await _make_tenant_and_user()
    suffix = uuid.uuid4().hex[:8]
    shared_url = f"https://mcp.example.test/backfill/{suffix}"
    async with async_session() as db:
        existing = MCPServer(
            tenant_id=tenant.id,
            name=f"existing-{suffix}",
            display_name="Existing configured server",
            base_url_template=shared_url,
            headers_template={"Authorization": "${tenant.existing_token}"},
            created_by_user_id=user.id,
        )
        group_a = Tool(
            name=f"legacy_a_{suffix}",
            display_name="A",
            type="mcp",
            category="mcp",
            parameters_schema={},
            config={},
            source="admin",
            tenant_id=tenant.id,
            mcp_server_url=shared_url,
            mcp_server_name="Legacy group A",
            mcp_tool_name="a",
        )
        group_b = Tool(
            name=f"legacy_b_{suffix}",
            display_name="B",
            type="mcp",
            category="mcp",
            parameters_schema={},
            config={},
            source="admin",
            tenant_id=tenant.id,
            mcp_server_url=shared_url,
            mcp_server_name="Legacy group B",
            mcp_tool_name="b",
        )
        configured = Tool(
            name=f"legacy_configured_{suffix}",
            display_name="Configured",
            type="mcp",
            category="mcp",
            parameters_schema={},
            config={"configured": True},
            source="admin",
            tenant_id=tenant.id,
            mcp_server_url=shared_url,
            mcp_server_name="Configured legacy group",
            mcp_tool_name="c",
        )
        db.add_all([existing, group_a, group_b, configured])
        await db.commit()
        existing_id = existing.id
        group_a_id = group_a.id
        group_b_id = group_b.id
        configured_id = configured.id

    migration_path = Path(__file__).parents[1] / "alembic/versions/202609041200_backfill_company_mcp_groups.py"
    spec = importlib.util.spec_from_file_location("company_mcp_group_backfill_test", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    async with engine.begin() as connection:
        linked, _skipped = await connection.run_sync(migration.backfill_company_mcp_groups)
    assert linked >= 2

    async with async_session() as db:
        group_a = await db.get(Tool, group_a_id)
        group_b = await db.get(Tool, group_b_id)
        configured = await db.get(Tool, configured_id)
        assert group_a.mcp_server_id not in {None, existing_id}
        assert group_b.mcp_server_id not in {None, existing_id, group_a.mcp_server_id}
        server_a = await db.get(MCPServer, group_a.mcp_server_id)
        server_b = await db.get(MCPServer, group_b.mcp_server_id)
        assert group_a.mcp_server_name == server_a.name
        assert group_b.mcp_server_name == server_b.name
        assert configured.mcp_server_id is None
        before = await db.scalar(
            select(func.count()).select_from(MCPServer).where(MCPServer.base_url_template == shared_url)
        )

    async with engine.begin() as connection:
        rerun_linked, _rerun_skipped = await connection.run_sync(migration.backfill_company_mcp_groups)
    assert rerun_linked == 0
    async with async_session() as db:
        after = await db.scalar(
            select(func.count()).select_from(MCPServer).where(MCPServer.base_url_template == shared_url)
        )
    assert after == before


async def test_agent_tool_schema_tracks_group_and_local_display_names(client):
    tenant, user, token = await _make_tenant_and_user()
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        agent = Agent(name=f"Agent {suffix}", creator_id=user.id, tenant_id=tenant.id)
        server = MCPServer(
            tenant_id=tenant.id,
            name=f"stable-{suffix}",
            display_name="Knowledge Group",
            base_url_template="https://mcp.example.test/runtime",
            headers_template={},
        )
        db.add_all([agent, server])
        await db.flush()
        tool = Tool(
            name=f"mcp_stable_{suffix}_retrieve",
            display_name="Local Retrieval",
            description="Retrieve documents.",
            type="mcp",
            category="mcp",
            parameters_schema={"type": "object", "properties": {}},
            mcp_server_id=server.id,
            mcp_server_name=server.name,
            mcp_server_url=server.base_url_template,
            mcp_tool_name="remote_retrieve",
            tenant_id=tenant.id,
            source="admin",
        )
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        agent_id = agent.id
        server_id = server.id
        tool_id = tool.id
        stable_tool_name = tool.name

    from app.services.agent_tools import get_agent_tools_for_llm

    first = await get_agent_tools_for_llm(agent_id)
    first_tool = next(item for item in first if item["function"]["name"] == stable_tool_name)
    assert '[MCP tool group: "Knowledge Group"; local tool: "Local Retrieval"]' in first_tool["function"]["description"]

    rename_group = await client.patch(
        f"/api/admin/mcp-servers/{server_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"display_name": "Renamed Knowledge Group"},
    )
    assert rename_group.status_code == 200, rename_group.text
    rename_tool = await client.put(
        f"/api/tools/{tool_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"display_name": "Renamed Local Retrieval"},
    )
    assert rename_tool.status_code == 200, rename_tool.text

    listed = await client.get(
        f"/api/tools?tenant_id={tenant.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert listed.status_code == 200, listed.text
    listed_tool = next(item for item in listed.json() if item["id"] == str(tool_id))
    assert listed_tool["mcp_server_display_name"] == "Renamed Knowledge Group"
    assert listed_tool["display_name"] == "Renamed Local Retrieval"

    second = await get_agent_tools_for_llm(agent_id)
    second_tool = next(item for item in second if item["function"]["name"] == stable_tool_name)
    assert "Renamed Knowledge Group" in second_tool["function"]["description"]
    assert "Renamed Local Retrieval" in second_tool["function"]["description"]
    assert second_tool["function"]["name"] == stable_tool_name
