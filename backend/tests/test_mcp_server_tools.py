"""Tests for MCP install/uninstall_agent_mcp_server tools."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.database import async_session, engine
from app.models.mcp_server import MCPServer  # noqa: F401  (resolve Tool.mcp_server_id FK metadata)
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose()
    yield
    await engine.dispose()


def _ctx(token):
    h = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=h)))


async def _seed_tenant():
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def _seed_user(tenant_id=None):
    async with async_session() as db:
        s = uuid.uuid4().hex[:12]
        ident = Identity(username=f"u_{s}", email=f"{s}@t.local", password_hash="x")
        db.add(ident)
        await db.flush()
        u = User(identity_id=ident.id, display_name="U", role="member", is_active=True, tenant_id=tenant_id)
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u


async def _pat(user, scope="write"):
    from app.services.pat_service import issue_pat

    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="t", scope=scope)
    return token


async def _seed_agent(creator, name="Agent"):
    from app.models.agent import Agent
    from app.models.participant import Participant

    async with async_session() as db:
        a = Agent(
            name=name,
            creator_id=creator.id,
            tenant_id=creator.tenant_id,
            agent_type="native",
            access_mode="company",
            status="idle",
        )
        db.add(a)
        await db.flush()
        db.add(Participant(type="agent", ref_id=a.id, display_name=a.name))
        await db.commit()
        await db.refresh(a)
        return a


async def _seed_mcp_server(tenant_id=None, name: str | None = None) -> MCPServer:
    """Seed a minimal MCPServer row with all NOT-NULL fields satisfied."""
    async with async_session() as db:
        srv_name = name or f"srv_{uuid.uuid4().hex[:8]}"
        srv = MCPServer(
            tenant_id=tenant_id,
            name=srv_name,
            display_name=srv_name,
            base_url_template="https://example.smithery.test",
            transport="http",
        )
        db.add(srv)
        await db.commit()
        await db.refresh(srv)
        return srv


# ── Test 1: install requires confirm ────────────────────────────────────────────


async def test_install_requires_confirm():
    """install_agent_mcp_server_impl without confirm=True returns guidance containing 'confirm=True'."""
    from app.mcp_server.tools_mcp import install_agent_mcp_server_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    out = await install_agent_mcp_server_impl(_ctx(token), agent=str(agent.id), server_id="github")
    assert "confirm=True" in out or "confirm=true" in out.lower(), f"Expected confirm guidance, got: {out!r}"


# ── Test 2: install confirmed without key reports key needed ────────────────────


async def test_install_confirmed_without_key_reports_key_needed():
    """With confirm=True but no Smithery API key, the function returns the key-required message."""
    from app.mcp_server.tools_mcp import install_agent_mcp_server_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    out = await install_agent_mcp_server_impl(_ctx(token), agent=str(agent.id), server_id="github", confirm=True)
    assert "Smithery API key" in out, f"Expected key-required message, got: {out!r}"


# ── Test 3: uninstall removes installed tools ───────────────────────────────────


async def test_uninstall_removes_installed_tools():
    """Seeded Tool + AgentTool rows for a user-installed MCP server are deleted on uninstall."""
    from sqlalchemy import select

    from app.mcp_server.tools_mcp import uninstall_agent_mcp_server_impl
    from app.models.tool import AgentTool, Tool

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    # Seed the MCPServer + a user-installed Tool + AgentTool assignment
    srv = await _seed_mcp_server(tenant_id=tenant.id)

    async with async_session() as db:
        tool_name = f"srv_x_tool_{uuid.uuid4().hex[:6]}"
        # Tool.source = "agent" matches what resource_discovery stores for Smithery tools.
        # installed_by_agent_id tracking lives on AgentTool, not Tool.
        t = Tool(
            name=tool_name,
            display_name="Srv X Tool",
            description="A test tool",
            type="mcp",
            category="custom",
            enabled=True,
            source="agent",
            mcp_server_id=srv.id,
            mcp_server_name=srv.name,
        )
        db.add(t)
        await db.flush()
        at = AgentTool(
            agent_id=agent.id,
            tool_id=t.id,
            enabled=True,
            source="user_installed",
            installed_by_agent_id=agent.id,
        )
        db.add(at)
        await db.commit()
        tool_id = t.id
        at_id = at.id

    out = await uninstall_agent_mcp_server_impl(_ctx(token), agent=str(agent.id), server=srv.name, confirm=True)
    assert "✅" in out, f"Expected ✅ in: {out!r}"

    # Verify rows are gone
    async with async_session() as db:
        remaining_tool = (await db.execute(select(Tool).where(Tool.id == tool_id))).scalar_one_or_none()
        remaining_at = (await db.execute(select(AgentTool).where(AgentTool.id == at_id))).scalar_one_or_none()

    assert remaining_tool is None, "Tool row should have been deleted"
    assert remaining_at is None, "AgentTool row should have been deleted"


# ── Test 4: uninstall requires write scope ──────────────────────────────────────


async def test_uninstall_requires_write():
    """A read-only PAT should be rejected for uninstall (needs write scope)."""
    from app.mcp_server.tools_mcp import uninstall_agent_mcp_server_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="read")

    out = await uninstall_agent_mcp_server_impl(_ctx(token), agent=str(agent.id), server="any-server", confirm=True)
    assert "需要 write" in out, f"Expected write-scope error, got: {out!r}"
