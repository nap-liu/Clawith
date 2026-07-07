import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.agent import Agent, AgentPermission
from app.models.identity import IdentityProvider
from app.models.org import AgentAgentRelationship, AgentRelationship, OrgDepartment, OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services import agent_tools
from app.services.contact_relationships import (
    add_contact_for_agent,
    remove_contact_for_agent,
    search_contacts_for_agent,
)
from app.services.tool_seeder import BUILTIN_TOOLS


def _llm_tool_description(tool_name: str) -> str:
    for tool in agent_tools.AGENT_TOOLS:
        function = tool.get("function") or {}
        if function.get("name") == tool_name:
            return function.get("description") or ""
    raise AssertionError(f"{tool_name} not found in AGENT_TOOLS")


def _seed_tool_description(tool_name: str) -> str:
    for tool in BUILTIN_TOOLS:
        if tool.get("name") == tool_name:
            return tool.get("description") or ""
    raise AssertionError(f"{tool_name} not found in BUILTIN_TOOLS")


def test_contact_tool_descriptions_require_user_intent_and_creator_confirmation():
    for description in (
        _llm_tool_description("search_contacts"),
        _seed_tool_description("search_contacts"),
    ):
        normalized = description.lower()
        assert "user explicitly asks" in normalized
        assert "relationship network" in normalized
        assert "do not use" in normalized
        assert "proactively" in normalized
        assert "when you need to contact" not in normalized

    for description in (
        _llm_tool_description("add_contact"),
        _seed_tool_description("add_contact"),
    ):
        normalized = description.lower()
        assert "user explicitly asked" in normalized
        assert "creator" in normalized
        assert "confirmed" in normalized
        assert "do not add contacts proactively" in normalized

    for description in (
        _llm_tool_description("remove_contact"),
        _seed_tool_description("remove_contact"),
    ):
        normalized = description.lower()
        assert "remove" in normalized
        assert "relationship network" in normalized
        assert "user explicitly asked" in normalized
        assert "creator" in normalized
        assert "confirmed" in normalized
        assert "do not remove contacts proactively" in normalized


@pytest.fixture
async def contact_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        Identity.__table__,
        Tenant.__table__,
        User.__table__,
        Agent.__table__,
        AgentPermission.__table__,
        IdentityProvider.__table__,
        OrgDepartment.__table__,
        OrgMember.__table__,
        AgentRelationship.__table__,
        AgentAgentRelationship.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


async def _seed_contact_graph(session):
    tenant_id = uuid.uuid4()
    other_tenant_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    member_user_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    web_provider_id = uuid.uuid4()

    session.add_all(
        [
            Tenant(id=tenant_id, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}"),
            Tenant(id=other_tenant_id, name="Other", slug=f"other-{uuid.uuid4().hex[:8]}"),
            Identity(id=uuid.uuid4(), username=f"creator-{uuid.uuid4().hex[:8]}"),
            User(
                id=creator_id,
                identity_id=None,
                tenant_id=tenant_id,
                display_name="Creator",
                role="member",
                is_active=True,
            ),
            User(
                id=member_user_id,
                identity_id=None,
                tenant_id=tenant_id,
                display_name="刘喜",
                role="member",
                is_active=True,
            ),
            IdentityProvider(id=provider_id, tenant_id=tenant_id, name="DingTalk", provider_type="dingtalk"),
            IdentityProvider(id=web_provider_id, tenant_id=tenant_id, name="Web", provider_type="web"),
        ]
    )
    await session.flush()

    source = Agent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=creator_id,
        name="Source Agent",
        access_mode="company",
        company_access_level="use",
        status="running",
    )
    target = Agent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        name="Research Agent",
        role_description="Research helper",
        access_mode="company",
        company_access_level="use",
        status="running",
    )
    other_tenant_agent = Agent(
        id=uuid.uuid4(),
        tenant_id=other_tenant_id,
        creator_id=uuid.uuid4(),
        name="Other Tenant Agent",
        access_mode="company",
        status="running",
    )
    dingtalk_member = OrgMember(
        id=uuid.uuid4(),
        provider_id=provider_id,
        user_id=member_user_id,
        tenant_id=tenant_id,
        name="刘喜",
        title="前端开发",
        department_path="Root/信息技术部/技术部/前端开发",
        external_id="632277911",
        phone="13800000000",
        status="active",
    )
    platform_member = OrgMember(
        id=uuid.uuid4(),
        provider_id=web_provider_id,
        user_id=member_user_id,
        tenant_id=tenant_id,
        name="刘喜",
        title="Platform User",
        department_path="",
        external_id=f"platform:{member_user_id}",
        status="active",
    )
    other_tenant_member = OrgMember(
        id=uuid.uuid4(),
        tenant_id=other_tenant_id,
        name="跨租户用户",
        external_id="other-user",
        status="active",
    )
    session.add_all([source, target, other_tenant_agent, dingtalk_member, platform_member, other_tenant_member])
    await session.flush()
    return {
        "tenant_id": tenant_id,
        "creator_id": creator_id,
        "source": source,
        "target": target,
        "other_tenant_agent": other_tenant_agent,
        "dingtalk_member": dingtalk_member,
        "platform_member": platform_member,
        "other_tenant_member": other_tenant_member,
    }


@pytest.mark.asyncio
async def test_search_contacts_returns_dingtalk_human_and_visible_agent(contact_session):
    ctx = await _seed_contact_graph(contact_session)

    human_results = await search_contacts_for_agent(
        contact_session,
        ctx["source"].id,
        query="刘",
        contact_type="human",
        current_user_id=ctx["creator_id"],
    )
    assert human_results == [
        {
            "id": str(ctx["dingtalk_member"].id),
            "type": "human",
            "name": "刘喜",
            "title": "前端开发",
            "channel": "dingtalk",
            "department_path": "Root/信息技术部/技术部/前端开发",
            "relationship_status": "not_added",
            "send_hint": '添加后使用 send_channel_message(member_name="刘喜", channel="dingtalk", ...)',
        }
    ]
    assert "phone" not in human_results[0]
    assert "external_id" not in human_results[0]

    agent_results = await search_contacts_for_agent(
        contact_session,
        ctx["source"].id,
        query="Research",
        contact_type="agent",
        current_user_id=ctx["creator_id"],
    )
    assert [row["id"] for row in agent_results] == [str(ctx["target"].id)]
    assert agent_results[0]["type"] == "agent"
    assert agent_results[0]["send_hint"] == '添加后使用 send_message_to_agent(agent_name="Research Agent", ...)'


@pytest.mark.asyncio
async def test_add_contact_creates_human_relationship_idempotently(contact_session):
    ctx = await _seed_contact_graph(contact_session)

    first = await add_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="human",
        target_id=str(ctx["dingtalk_member"].id),
        relation="collaborator",
        description="前端事项协作",
        current_user_id=ctx["creator_id"],
    )
    second = await add_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="human",
        target_id=str(ctx["dingtalk_member"].id),
        relation="stakeholder",
        description="更新描述",
        current_user_id=ctx["creator_id"],
    )

    assert first["status"] == "added"
    assert second["status"] == "already_added"
    assert second["relationship_status"] == "active"

    rows = (
        await contact_session.execute(
            select(AgentRelationship).where(AgentRelationship.agent_id == ctx["source"].id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].member_id == ctx["dingtalk_member"].id
    assert rows[0].relation == "stakeholder"
    assert rows[0].description == "更新描述"


@pytest.mark.asyncio
async def test_add_contact_creates_agent_relationship_idempotently(contact_session):
    ctx = await _seed_contact_graph(contact_session)

    first = await add_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="agent",
        target_id=str(ctx["target"].id),
        relation="peer",
        description="调研协作",
        current_user_id=ctx["creator_id"],
    )
    second = await add_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="agent",
        target_id=str(ctx["target"].id),
        relation="collaborator",
        description="更新协作关系",
        current_user_id=ctx["creator_id"],
    )

    assert first["status"] == "added"
    assert second["status"] == "already_added"
    assert second["relationship_status"] == "active"

    rows = (
        await contact_session.execute(
            select(AgentAgentRelationship).where(AgentAgentRelationship.agent_id == ctx["source"].id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].target_agent_id == ctx["target"].id
    assert rows[0].relation == "collaborator"
    assert rows[0].description == "更新协作关系"


@pytest.mark.asyncio
async def test_add_contact_rejects_cross_tenant_targets(contact_session):
    ctx = await _seed_contact_graph(contact_session)

    human = await add_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="human",
        target_id=str(ctx["other_tenant_member"].id),
        current_user_id=ctx["creator_id"],
    )
    agent = await add_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="agent",
        target_id=str(ctx["other_tenant_agent"].id),
        current_user_id=ctx["creator_id"],
    )

    assert human["status"] == "error"
    assert human["reason"] == "contact_not_available"
    assert agent["status"] == "error"
    assert agent["reason"] == "contact_not_available"


@pytest.mark.asyncio
async def test_remove_contact_deletes_human_relationship_idempotently(contact_session):
    ctx = await _seed_contact_graph(contact_session)
    await add_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="human",
        target_id=str(ctx["dingtalk_member"].id),
        current_user_id=ctx["creator_id"],
    )

    first = await remove_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="human",
        target_id=str(ctx["dingtalk_member"].id),
        current_user_id=ctx["creator_id"],
    )
    second = await remove_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="human",
        target_id=str(ctx["dingtalk_member"].id),
        current_user_id=ctx["creator_id"],
    )

    assert first == {
        "status": "removed",
        "id": str(ctx["dingtalk_member"].id),
        "type": "human",
        "name": "刘喜",
    }
    assert second == {
        "status": "not_found",
        "id": str(ctx["dingtalk_member"].id),
        "type": "human",
        "name": "刘喜",
    }
    rows = (
        await contact_session.execute(
            select(AgentRelationship).where(AgentRelationship.agent_id == ctx["source"].id)
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_remove_contact_deletes_agent_relationship_idempotently(contact_session):
    ctx = await _seed_contact_graph(contact_session)
    await add_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="agent",
        target_id=str(ctx["target"].id),
        current_user_id=ctx["creator_id"],
    )

    first = await remove_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="agent",
        target_id=str(ctx["target"].id),
        current_user_id=ctx["creator_id"],
    )
    second = await remove_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="agent",
        target_id=str(ctx["target"].id),
        current_user_id=ctx["creator_id"],
    )

    assert first == {
        "status": "removed",
        "id": str(ctx["target"].id),
        "type": "agent",
        "name": "Research Agent",
    }
    assert second == {
        "status": "not_found",
        "id": str(ctx["target"].id),
        "type": "agent",
        "name": "Research Agent",
    }
    rows = (
        await contact_session.execute(
            select(AgentAgentRelationship).where(AgentAgentRelationship.agent_id == ctx["source"].id)
        )
    ).scalars().all()
    assert rows == []


class _SameSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


def _same_session_factory(session):
    return lambda: _SameSessionContext(session)


async def _noop_log_activity(*_args, **_kwargs):
    return None


async def _no_cli_tool(*_args, **_kwargs):
    return None


async def _unknown_mcp_tool(tool_name, *_args, **_kwargs):
    return f"Tool {tool_name} not found"


def _patch_tool_session(monkeypatch, session):
    monkeypatch.setattr(agent_tools, "async_session", _same_session_factory(session))
    monkeypatch.setattr(agent_tools, "_execute_cli_tool", _no_cli_tool)
    monkeypatch.setattr(agent_tools, "_execute_mcp_tool", _unknown_mcp_tool)

    import app.services.activity_logger as activity_logger

    monkeypatch.setattr(activity_logger, "log_activity", _noop_log_activity)


@pytest.mark.asyncio
async def test_execute_tool_search_contacts_returns_human_contacts(contact_session, monkeypatch):
    ctx = await _seed_contact_graph(contact_session)
    _patch_tool_session(monkeypatch, contact_session)

    result = await agent_tools.execute_tool(
        "search_contacts",
        {"query": "刘", "type": "human"},
        ctx["source"].id,
        ctx["creator_id"],
    )

    assert f"id={ctx['dingtalk_member'].id}" in result
    assert "type=human" in result
    assert "human:" not in result
    assert "刘喜" in result
    assert "dingtalk" in result
    assert "13800000000" not in result
    assert "632277911" not in result


@pytest.mark.asyncio
async def test_execute_tool_add_contact_creates_human_and_agent_relationships(contact_session, monkeypatch):
    ctx = await _seed_contact_graph(contact_session)
    _patch_tool_session(monkeypatch, contact_session)

    human_result = await agent_tools.execute_tool(
        "add_contact",
        {
            "target_type": "human",
            "target_id": str(ctx["dingtalk_member"].id),
            "relation": "collaborator",
            "description": "前端协作",
        },
        ctx["source"].id,
        ctx["creator_id"],
        skip_autonomy=True,
    )
    agent_result = await agent_tools.execute_tool(
        "add_contact",
        {
            "target_type": "agent",
            "target_id": str(ctx["target"].id),
            "relation": "peer",
            "description": "调研协作",
        },
        ctx["source"].id,
        ctx["creator_id"],
        skip_autonomy=True,
    )

    assert "✅ Added 刘喜" in human_result
    assert 'send_channel_message(member_name="刘喜", channel="dingtalk", ...)' in human_result
    assert "human:" not in human_result
    assert "✅ Added Research Agent" in agent_result
    assert "agent:" not in agent_result

    human_rows = (
        await contact_session.execute(
            select(AgentRelationship).where(AgentRelationship.agent_id == ctx["source"].id)
        )
    ).scalars().all()
    agent_rows = (
        await contact_session.execute(
            select(AgentAgentRelationship).where(AgentAgentRelationship.agent_id == ctx["source"].id)
        )
    ).scalars().all()
    assert [row.member_id for row in human_rows] == [ctx["dingtalk_member"].id]
    assert [row.target_agent_id for row in agent_rows] == [ctx["target"].id]


@pytest.mark.asyncio
async def test_execute_tool_remove_contact_removes_human_and_agent_relationships(contact_session, monkeypatch):
    ctx = await _seed_contact_graph(contact_session)
    _patch_tool_session(monkeypatch, contact_session)
    await add_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="human",
        target_id=str(ctx["dingtalk_member"].id),
        current_user_id=ctx["creator_id"],
    )
    await add_contact_for_agent(
        contact_session,
        ctx["source"].id,
        target_type="agent",
        target_id=str(ctx["target"].id),
        current_user_id=ctx["creator_id"],
    )

    human_result = await agent_tools.execute_tool(
        "remove_contact",
        {"target_type": "human", "target_id": str(ctx["dingtalk_member"].id)},
        ctx["source"].id,
        ctx["creator_id"],
        skip_autonomy=True,
    )
    agent_result = await agent_tools.execute_tool(
        "remove_contact",
        {"target_type": "agent", "target_id": str(ctx["target"].id)},
        ctx["source"].id,
        ctx["creator_id"],
        skip_autonomy=True,
    )

    assert "✅ Removed 刘喜" in human_result
    assert "human id=" in human_result
    assert "human:" not in human_result
    assert "✅ Removed Research Agent" in agent_result
    assert "agent id=" in agent_result
    assert "agent:" not in agent_result

    human_rows = (
        await contact_session.execute(
            select(AgentRelationship).where(AgentRelationship.agent_id == ctx["source"].id)
        )
    ).scalars().all()
    agent_rows = (
        await contact_session.execute(
            select(AgentAgentRelationship).where(AgentAgentRelationship.agent_id == ctx["source"].id)
        )
    ).scalars().all()
    assert human_rows == []
    assert agent_rows == []
