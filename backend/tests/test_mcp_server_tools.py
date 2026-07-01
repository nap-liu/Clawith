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


# ── Helpers for local-server install tests ──────────────────────────────────────


async def _seed_mcp_tool(srv, name: str | None = None):
    """Seed a Tool row linked to a registered MCPServer (as test-connection would)."""
    from app.models.tool import Tool

    async with async_session() as db:
        tname = name or f"mcp_{srv.name}_tool_{uuid.uuid4().hex[:6]}"
        t = Tool(
            name=tname,
            display_name=tname,
            description="d",
            type="mcp",
            category="custom",
            enabled=True,
            source="agent",
            mcp_server_id=srv.id,
            mcp_server_name=srv.name,
        )
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def _agent_tools(agent_id):
    from sqlalchemy import select

    from app.models.tool import AgentTool

    async with async_session() as db:
        return (await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))).scalars().all()


# ── Test 5: install a locally-registered server by id → associates existing tools ─


async def test_install_local_server_by_id_associates_existing_tools():
    from app.mcp_server.tools_mcp import install_agent_mcp_server_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    srv = await _seed_mcp_server(tenant_id=tenant.id)
    await _seed_mcp_tool(srv)
    await _seed_mcp_tool(srv)

    out = await install_agent_mcp_server_impl(_ctx(token), agent=str(agent.id), server_id=str(srv.id), confirm=True)
    assert "✅" in out, f"Expected success, got: {out!r}"

    ats = await _agent_tools(agent.id)
    assert len(ats) == 2, f"Expected 2 AgentTool rows, got {len(ats)}"
    assert all(at.source == "user_installed" for at in ats)
    assert all(at.installed_by_agent_id == agent.id for at in ats)


# ── Test 6: install a locally-registered server by name → associates ─────────────


async def test_install_local_server_by_name_associates():
    from app.mcp_server.tools_mcp import install_agent_mcp_server_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    srv = await _seed_mcp_server(tenant_id=tenant.id)
    await _seed_mcp_tool(srv)

    out = await install_agent_mcp_server_impl(_ctx(token), agent=str(agent.id), server_id=srv.name, confirm=True)
    assert "✅" in out, f"Expected success, got: {out!r}"
    assert len(await _agent_tools(agent.id)) == 1


# ── Test 7: local-server install confirm guidance mentions the registered server ─


async def test_install_local_server_confirm_guidance():
    from app.mcp_server.tools_mcp import install_agent_mcp_server_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    srv = await _seed_mcp_server(tenant_id=tenant.id)
    await _seed_mcp_tool(srv)

    out = await install_agent_mcp_server_impl(_ctx(token), agent=str(agent.id), server_id=str(srv.id))
    assert ("confirm=True" in out or "confirm=true" in out.lower()), f"Expected confirm guidance: {out!r}"
    assert srv.name in out, f"Expected registered server name in guidance: {out!r}"
    assert "已注册" in out or "本平台" in out, f"Expected registered-server wording: {out!r}"
    # No association happened on the guidance call
    assert len(await _agent_tools(agent.id)) == 0


# ── Test 8: local server with no discovered tools → falls back to direct import ──


async def test_install_local_server_no_tools_direct_import(monkeypatch):
    import app.services.resource_discovery as rd
    from app.mcp_server.tools_mcp import install_agent_mcp_server_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    srv = await _seed_mcp_server(tenant_id=tenant.id)  # http, no Tool rows

    calls = {}

    async def _fake_direct(mcp_url, agent_id, server_name=None, api_key=None, headers=None):
        calls["mcp_url"] = mcp_url
        calls["agent_id"] = agent_id
        return "✅ imported 3 tools (direct)"

    monkeypatch.setattr(rd, "import_mcp_direct", _fake_direct)

    out = await install_agent_mcp_server_impl(_ctx(token), agent=str(agent.id), server_id=str(srv.id), confirm=True)
    assert calls.get("mcp_url") == srv.base_url_template, f"direct import not called with server url: {calls!r}"
    assert str(calls.get("agent_id")) == str(agent.id)
    assert "✅" in out


# ── Test 9: unknown server id → falls back to Smithery (backward compatible) ─────


async def test_install_unknown_server_falls_back_to_smithery():
    from app.mcp_server.tools_mcp import install_agent_mcp_server_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    out = await install_agent_mcp_server_impl(_ctx(token), agent=str(agent.id), server_id="github", confirm=True)
    assert "Smithery API key" in out, f"Expected Smithery fallback, got: {out!r}"


# ── Test 10: cross-tenant server id is NOT associated (treated as unknown) ───────


async def test_install_cross_tenant_server_not_associated():
    from app.mcp_server.tools_mcp import install_agent_mcp_server_impl

    tenant_a = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant_a.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    tenant_b = await _seed_tenant()
    other_srv = await _seed_mcp_server(tenant_id=tenant_b.id)
    await _seed_mcp_tool(other_srv)

    out = await install_agent_mcp_server_impl(_ctx(token), agent=str(agent.id), server_id=str(other_srv.id), confirm=True)
    # Not associated: must not create AgentTool rows for the cross-tenant server's tools
    assert len(await _agent_tools(agent.id)) == 0, "cross-tenant server must not be associated"
    assert "Smithery API key" in out or "❌" in out, f"Expected non-association path, got: {out!r}"


# ── Test 11: install (associate) then uninstall is symmetric ─────────────────────


async def test_install_local_then_uninstall_symmetric():
    from app.mcp_server.tools_mcp import install_agent_mcp_server_impl, uninstall_agent_mcp_server_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")

    srv = await _seed_mcp_server(tenant_id=tenant.id)
    await _seed_mcp_tool(srv)
    await _seed_mcp_tool(srv)

    await install_agent_mcp_server_impl(_ctx(token), agent=str(agent.id), server_id=str(srv.id), confirm=True)
    assert len(await _agent_tools(agent.id)) == 2

    out = await uninstall_agent_mcp_server_impl(_ctx(token), agent=str(agent.id), server=srv.name, confirm=True)
    assert "✅" in out, f"Expected uninstall success: {out!r}"
    assert len(await _agent_tools(agent.id)) == 0, "uninstall should remove all associated AgentTool rows"


# ── Test 12: list_mcp_servers lists tenant + global, hides other tenants ─────────


async def test_list_mcp_servers_scope():
    from app.mcp_server.tools_mcp import list_mcp_servers_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    token = await _pat(user, scope="read")  # read scope suffices for discovery

    mine = await _seed_mcp_server(tenant_id=tenant.id, name=f"mine_{uuid.uuid4().hex[:6]}")
    glob = await _seed_mcp_server(tenant_id=None, name=f"glob_{uuid.uuid4().hex[:6]}")
    other_t = await _seed_tenant()
    other = await _seed_mcp_server(tenant_id=other_t.id, name=f"other_{uuid.uuid4().hex[:6]}")

    out = await list_mcp_servers_impl(_ctx(token))
    assert mine.name in out, f"own-tenant server missing: {out!r}"
    assert glob.name in out, f"global server missing: {out!r}"
    assert other.name not in out, f"other-tenant server leaked: {out!r}"


# ── Test 13: list_mcp_servers shows discovered tool count ────────────────────────


async def test_list_mcp_servers_tool_count():
    from app.mcp_server.tools_mcp import list_mcp_servers_impl

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    token = await _pat(user, scope="read")

    srv = await _seed_mcp_server(tenant_id=tenant.id, name=f"cnt_{uuid.uuid4().hex[:6]}")
    await _seed_mcp_tool(srv)
    await _seed_mcp_tool(srv)

    out = await list_mcp_servers_impl(_ctx(token))
    assert srv.name in out
    assert "2" in out, f"Expected tool count 2 in output: {out!r}"
