from __future__ import annotations
import uuid
import pytest
from types import SimpleNamespace
from sqlalchemy import select
from app.database import async_session, engine
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.models.mcp_server import MCPServer  # noqa: F401  (resolve Tool.mcp_server_id FK metadata)


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose(); yield; await engine.dispose()


def _ctx(token):
    h = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=h)))


async def _seed_tenant():
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t); await db.commit(); await db.refresh(t); return t


async def _seed_user(tenant_id=None):
    async with async_session() as db:
        s = uuid.uuid4().hex[:12]
        ident = Identity(username=f"u_{s}", email=f"{s}@t.local", password_hash="x")
        db.add(ident); await db.flush()
        u = User(identity_id=ident.id, display_name="U", role="member", is_active=True, tenant_id=tenant_id)
        db.add(u); await db.commit(); await db.refresh(u); return u


async def _pat(user, scope="write"):
    from app.services.pat_service import issue_pat
    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="t", scope=scope)
    return token


async def _seed_agent(creator, name="Agent", access_mode="company"):
    from app.models.agent import Agent
    from app.models.participant import Participant
    async with async_session() as db:
        a = Agent(name=name, creator_id=creator.id, tenant_id=creator.tenant_id,
                  agent_type="native", access_mode=access_mode, status="idle")
        db.add(a); await db.flush()
        db.add(Participant(type="agent", ref_id=a.id, display_name=a.name))
        await db.commit(); await db.refresh(a); return a


async def _seed_builtin_tool(name=None):
    from app.models.tool import Tool
    name = name or f"tool_{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        t = Tool(name=name, display_name=name, description="", category="custom",
                 source="builtin", enabled=True, is_default=False)
        db.add(t); await db.commit(); await db.refresh(t); return t


async def test_set_agent_tools_enables():
    from app.mcp_server.tools_config import set_agent_tools_impl
    from app.models.tool import AgentTool
    tenant = await _seed_tenant(); user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user); tool = await _seed_builtin_tool()
    token = await _pat(user)
    out = await set_agent_tools_impl(_ctx(token), agent=str(agent.id), enable=[tool.name])
    assert "✅" in out
    async with async_session() as db:
        row = (await db.execute(select(AgentTool).where(
            AgentTool.agent_id == agent.id, AgentTool.tool_id == tool.id))).scalar_one_or_none()
    assert row is not None and row.enabled is True


async def test_set_agent_tools_requires_write():
    from app.mcp_server.tools_config import set_agent_tools_impl
    tenant = await _seed_tenant(); user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="read")
    out = await set_agent_tools_impl(_ctx(token), agent=str(agent.id), enable=["x"])
    assert "需要 write" in out


async def test_set_agent_trigger_creates_cron():
    from app.mcp_server.tools_config import set_agent_trigger_impl
    from app.models.trigger import AgentTrigger
    tenant = await _seed_tenant(); user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user); token = await _pat(user)
    out = await set_agent_trigger_impl(_ctx(token), agent=str(agent.id), name="daily",
                                       type="cron", config={"expr": "0 9 * * *"}, reason="morning brief")
    assert "❌" not in out, out
    async with async_session() as db:
        row = (await db.execute(select(AgentTrigger).where(
            AgentTrigger.agent_id == agent.id, AgentTrigger.name == "daily"))).scalar_one_or_none()
    assert row is not None


async def test_delete_agent_trigger_removes():
    from app.mcp_server.tools_config import set_agent_trigger_impl, delete_agent_trigger_impl
    from app.models.trigger import AgentTrigger
    tenant = await _seed_tenant(); user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user); token = await _pat(user)
    await set_agent_trigger_impl(_ctx(token), agent=str(agent.id), name="todelete",
                                 type="cron", config={"expr": "0 9 * * *"}, reason="r")
    out = await delete_agent_trigger_impl(_ctx(token), agent=str(agent.id), trigger="todelete")
    assert "✅" in out
    async with async_session() as db:
        row = (await db.execute(select(AgentTrigger).where(
            AgentTrigger.agent_id == agent.id, AgentTrigger.name == "todelete"))).scalar_one_or_none()
    assert row is None


async def test_edit_agent_soul_updates_personality():
    from app.services.agent_manager import agent_manager
    from app.mcp_server.tools_config import edit_agent_soul_impl
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    # create a soul.md for the agent (the bare DB seed doesn't initialize files)
    soul_path = agent_manager._agent_dir(agent.id) / "soul.md"
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text("# Soul\n## Personality\nold\n", encoding="utf-8")
    token = await _pat(user, scope="write")
    out = await edit_agent_soul_impl(_ctx(token), agent=str(agent.id), personality="curious and concise")
    assert "✅" in out
    content = soul_path.read_text(encoding="utf-8")
    assert "curious and concise" in content and "old" not in content


async def test_edit_agent_soul_no_fields_noop():
    from app.services.agent_manager import agent_manager
    from app.mcp_server.tools_config import edit_agent_soul_impl
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    soul_path = agent_manager._agent_dir(agent.id) / "soul.md"
    soul_path.parent.mkdir(parents=True, exist_ok=True)
    soul_path.write_text("# Soul\n", encoding="utf-8")
    token = await _pat(user, scope="write")
    out = await edit_agent_soul_impl(_ctx(token), agent=str(agent.id))
    assert "未提供" in out
