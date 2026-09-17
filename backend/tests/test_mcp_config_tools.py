from __future__ import annotations
import asyncio
import json
import uuid
import pytest
from types import SimpleNamespace
from sqlalchemy import func, select
from app.database import async_session, engine
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.models.mcp_server import MCPServer  # noqa: F401  (resolve Tool.mcp_server_id FK metadata)


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


async def _seed_agent(creator, name="Agent", access_mode="company"):
    from app.models.agent import Agent, AgentPermission
    from app.models.participant import Participant
    async with async_session() as db:
        a = Agent(name=name, creator_id=creator.id, tenant_id=creator.tenant_id,
                  agent_type="native", access_mode=access_mode, status="idle")
        db.add(a)
        await db.flush()
        if access_mode == "company":
            db.add(AgentPermission(agent_id=a.id, scope_type="company", access_level="use"))
        db.add(Participant(type="agent", ref_id=a.id, display_name=a.name))
        await db.commit()
        await db.refresh(a)
        return a


async def _seed_builtin_tool(name=None):
    from app.models.tool import Tool
    name = name or f"tool_{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        t = Tool(name=name, display_name=name, description="", category="custom",
                 source="builtin", enabled=True, is_default=False)
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def test_set_agent_tools_enables():
    from app.mcp_server.tools_config import set_agent_tools_impl
    from app.models.tool import AgentTool
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    tool = await _seed_builtin_tool()
    token = await _pat(user)
    out = await set_agent_tools_impl(_ctx(token), agent=str(agent.id), enable=[tool.name])
    assert "✅" in out
    async with async_session() as db:
        row = (await db.execute(select(AgentTool).where(
            AgentTool.agent_id == agent.id, AgentTool.tool_id == tool.id))).scalar_one_or_none()
    assert row is not None and row.enabled is True


async def test_set_agent_tools_requires_write():
    from app.mcp_server.tools_config import set_agent_tools_impl
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="read")
    out = await set_agent_tools_impl(_ctx(token), agent=str(agent.id), enable=["x"])
    assert "需要 write" in out


async def test_set_agent_trigger_creates_cron():
    from app.mcp_server.tools_config import set_agent_trigger_impl
    from app.models.trigger import AgentTrigger
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user)
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
    from app.services.agent_tools import _handle_cancel_trigger
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user)
    await set_agent_trigger_impl(_ctx(token), agent=str(agent.id), name="todelete",
                                 type="cron", config={"expr": "0 9 * * *"}, reason="r")
    await _handle_cancel_trigger(agent.id, {"name": "todelete"}, user_id=user.id)
    out = await delete_agent_trigger_impl(_ctx(token), agent=str(agent.id), trigger="todelete")
    assert "✅" in out
    async with async_session() as db:
        row = (await db.execute(select(AgentTrigger).where(
            AgentTrigger.agent_id == agent.id, AgentTrigger.name == "todelete"))).scalar_one_or_none()
    assert row is None


async def test_set_agent_relationships_a2a_merge():
    from app.services.agent_manager import agent_manager
    from app.mcp_server.tools_config import set_agent_relationships_impl
    from app.models.org import AgentAgentRelationship
    from sqlalchemy import select
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    a = await _seed_agent(user, name=f"Lead_{uuid.uuid4().hex[:6]}")
    b = await _seed_agent(user, name=f"Helper_{uuid.uuid4().hex[:6]}")
    agent_manager._agent_dir(a.id).mkdir(parents=True, exist_ok=True)
    token = await _pat(user, scope="write")
    out = await set_agent_relationships_impl(_ctx(token), agent=str(a.id),
              agent_links=[{"agent_id": str(b.id), "relation": "collaborator"}])
    assert "✅" in out, out
    async with async_session() as db:
        row = (await db.execute(select(AgentAgentRelationship).where(
            AgentAgentRelationship.agent_id == a.id,
            AgentAgentRelationship.target_agent_id == b.id))).scalar_one_or_none()
    assert row is not None


async def test_set_agent_relationships_human_platform_user():
    from app.services.agent_manager import agent_manager
    from app.mcp_server.tools_config import set_agent_relationships_impl
    from app.models.org import AgentRelationship
    from sqlalchemy import select
    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id)
    colleague = await _seed_user(tenant_id=tenant.id)   # same tenant → has company access to a company agent
    a = await _seed_agent(owner, name=f"Boss_{uuid.uuid4().hex[:6]}")  # company access_mode by default
    agent_manager._agent_dir(a.id).mkdir(parents=True, exist_ok=True)
    token = await _pat(owner, scope="write")
    out = await set_agent_relationships_impl(_ctx(token), agent=str(a.id),
              human_links=[{"user_id": str(colleague.id), "relation": "manager"}])
    assert "✅" in out, out
    async with async_session() as db:
        rows = (await db.execute(select(AgentRelationship).where(
            AgentRelationship.agent_id == a.id))).scalars().all()
    assert len(rows) >= 1


async def test_mcp_relationship_replace_suppresses_and_merge_explicitly_restores():
    from app.mcp_server.tools_config import set_agent_relationships_impl
    from app.models.org import RelationshipSuppression

    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id)
    colleague_a = await _seed_user(tenant_id=tenant.id)
    colleague_b = await _seed_user(tenant_id=tenant.id)
    source = await _seed_agent(owner, name=f"Source_{uuid.uuid4().hex[:6]}")
    target_a = await _seed_agent(owner, name=f"TargetA_{uuid.uuid4().hex[:6]}")
    target_b = await _seed_agent(owner, name=f"TargetB_{uuid.uuid4().hex[:6]}")
    token = await _pat(owner, scope="write")

    await set_agent_relationships_impl(
        _ctx(token),
        agent=str(source.id),
        human_links=[
            {"user_id": str(colleague_a.id)},
            {"user_id": str(colleague_b.id)},
        ],
        agent_links=[
            {"agent_id": str(target_a.id)},
            {"agent_id": str(target_b.id)},
        ],
    )
    await set_agent_relationships_impl(
        _ctx(token),
        agent=str(source.id),
        human_links=[{"user_id": str(colleague_a.id)}],
        agent_links=[{"agent_id": str(target_a.id)}],
        mode="replace",
    )

    async with async_session() as db:
        suppressions = (
            await db.execute(
                select(RelationshipSuppression).where(
                    RelationshipSuppression.agent_id == source.id
                )
            )
        ).scalars().all()
        assert {(row.target_type, row.target_id) for row in suppressions} == {
            ("user", colleague_b.id),
            ("agent", target_b.id),
        }

    await set_agent_relationships_impl(
        _ctx(token),
        agent=str(source.id),
        human_links=[{"user_id": str(colleague_b.id)}],
        agent_links=[{"agent_id": str(target_b.id)}],
        mode="merge",
    )
    async with async_session() as db:
        remaining = await db.scalar(
            select(func.count(RelationshipSuppression.id)).where(
                RelationshipSuppression.agent_id == source.id
            )
        )
    assert remaining == 0


async def test_set_agent_relationships_requires_write():
    from app.mcp_server.tools_config import set_agent_relationships_impl
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    a = await _seed_agent(user)
    token = await _pat(user, scope="read")
    out = await set_agent_relationships_impl(_ctx(token), agent=str(a.id),
              agent_links=[{"agent_id": str(uuid.uuid4())}])
    assert "需要 write" in out


# ── Access tools, list_agent_triggers, update_agent_trigger ──

async def test_set_agent_access_mode_requires_confirm():
    from app.mcp_server.tools_config import set_agent_access_mode_impl
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user, access_mode="company")
    token = await _pat(user, scope="write")
    out = await set_agent_access_mode_impl(
        _ctx(token), agent=str(agent.id), access_mode="private"
    )
    assert "confirm=true" in out.lower() or "confirm=True" in out
    # Agent access_mode must NOT have changed
    async with async_session() as db:
        from app.models.agent import Agent
        a = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
    assert a.access_mode == "company"


async def test_set_agent_access_mode_preserves_existing_grants():
    from app.mcp_server.tools_config import set_agent_access_mode_impl
    from app.models.agent import Agent, AgentPermission
    from app.models.org import OrgDepartment
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    colleague = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user, access_mode="company")
    async with async_session() as db:
        stored_agent = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
        stored_agent.company_access_level = "manage"
        department = OrgDepartment(
            tenant_id=tenant.id,
            name="Engineering",
            path="Root/Engineering",
            status="active",
        )
        db.add(department)
        await db.flush()
        db.add_all([
            AgentPermission(
                agent_id=agent.id,
                scope_type="user",
                scope_id=colleague.id,
                access_level="use",
            ),
            AgentPermission(
                agent_id=agent.id,
                scope_type="department",
                scope_id=department.id,
                access_level="manage",
            ),
        ])
        await db.commit()
        department_id = department.id
    token = await _pat(user, scope="write")
    out = await set_agent_access_mode_impl(
        _ctx(token), agent=str(agent.id), access_mode="custom", confirm=True
    )
    assert "✅" in out
    async with async_session() as db:
        a = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
        grants = (
            await db.execute(
                select(AgentPermission).where(AgentPermission.agent_id == agent.id)
            )
        ).scalars().all()
    assert a.access_mode == "custom"
    assert a.company_grant_level is None
    assert any(p.scope_type == "user" and p.scope_id == colleague.id for p in grants)
    assert any(p.scope_type == "department" and p.scope_id == department_id for p in grants)


async def test_set_agent_access_mode_company_level_change_is_explicit():
    from app.mcp_server.tools_config import set_agent_access_mode_impl
    from app.models.agent import Agent

    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(owner, access_mode="company")
    token = await _pat(owner, scope="write")

    preview = await set_agent_access_mode_impl(
        _ctx(token),
        agent=str(agent.id),
        access_mode="company",
        company_access_level="manage",
    )
    assert "manage" in preview
    async with async_session() as db:
        assert (await db.get(Agent, agent.id)).company_grant_level == "use"
    out = await set_agent_access_mode_impl(
        _ctx(token),
        agent=str(agent.id),
        access_mode="company",
        company_access_level="manage",
        confirm=True,
    )
    assert "✅" in out
    async with async_session() as db:
        stored = (await db.execute(select(Agent).where(Agent.id == agent.id))).scalar_one()
    assert stored.company_access_level == "manage"


async def test_grant_agent_access_is_incremental_and_atomic():
    from app.mcp_server.tools_config import grant_agent_access_impl
    from app.models.agent import AgentPermission
    from app.models.org import OrgDepartment

    tenant = await _seed_tenant()
    other_tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id)
    existing_user = await _seed_user(tenant_id=tenant.id)
    new_user = await _seed_user(tenant_id=tenant.id)
    foreign_user = await _seed_user(tenant_id=other_tenant.id)
    agent = await _seed_agent(owner, access_mode="custom")
    async with async_session() as db:
        department = OrgDepartment(
            tenant_id=tenant.id,
            name="Product",
            path="Root/Product",
            status="active",
        )
        db.add(department)
        await db.flush()
        db.add(
            AgentPermission(
                agent_id=agent.id,
                scope_type="user",
                scope_id=existing_user.id,
                access_level="use",
            )
        )
        await db.commit()
        department_id = department.id

    token = await _pat(owner, scope="write")
    rejected = await grant_agent_access_impl(
        _ctx(token),
        agent=str(agent.id),
        user_ids=[str(new_user.id), str(foreign_user.id)],
        department_ids=[str(department_id)],
        confirm=True,
    )
    assert "❌" in rejected
    async with async_session() as db:
        rejected_rows = (
            await db.execute(
                select(AgentPermission).where(AgentPermission.agent_id == agent.id)
            )
        ).scalars().all()
    assert not any(p.scope_id == new_user.id for p in rejected_rows)
    assert not any(p.scope_id == department_id for p in rejected_rows)

    out = await grant_agent_access_impl(
        _ctx(token),
        agent=str(agent.id),
        user_ids=[str(new_user.id)],
        department_ids=[str(department_id)],
        access_level="manage",
        confirm=True,
    )
    assert "✅" in out
    async with async_session() as db:
        grants = (
            await db.execute(
                select(AgentPermission).where(AgentPermission.agent_id == agent.id)
            )
        ).scalars().all()
    assert any(p.scope_id == existing_user.id for p in grants)
    assert any(p.scope_id == new_user.id and p.access_level == "manage" for p in grants)
    assert any(p.scope_id == department_id and p.access_level == "manage" for p in grants)


async def test_grant_agent_access_is_concurrency_safe_for_same_department():
    from app.mcp_server.tools_config import grant_agent_access_impl
    from app.models.agent import AgentPermission
    from app.models.org import OrgDepartment

    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(owner, access_mode="custom")
    async with async_session() as db:
        baseline_department = OrgDepartment(
            tenant_id=tenant.id,
            name="Baseline",
            path="Root/Baseline",
            status="active",
        )
        concurrent_department = OrgDepartment(
            tenant_id=tenant.id,
            name="Concurrent",
            path="Root/Concurrent",
            status="active",
        )
        db.add_all([baseline_department, concurrent_department])
        await db.commit()
        baseline_department_id = baseline_department.id
        concurrent_department_id = concurrent_department.id

    token = await _pat(owner, scope="write")
    baseline = await grant_agent_access_impl(
        _ctx(token),
        agent=str(agent.id),
        department_ids=[str(baseline_department_id)],
        confirm=True,
    )
    assert "✅" in baseline

    results = await asyncio.gather(
        *(
            grant_agent_access_impl(
                _ctx(token),
                agent=str(agent.id),
                department_ids=[str(concurrent_department_id)],
                access_level="use",
                confirm=True,
            )
            for _ in range(8)
        )
    )
    assert all("✅" in result for result in results), results

    async with async_session() as db:
        grant_count = (
            await db.execute(
                select(func.count(AgentPermission.id)).where(
                    AgentPermission.agent_id == agent.id,
                    AgentPermission.scope_type == "department",
                    AgentPermission.scope_id == concurrent_department_id,
                )
            )
        ).scalar_one()
        owner_grant_count = (
            await db.execute(
                select(func.count(AgentPermission.id)).where(
                    AgentPermission.agent_id == agent.id,
                    AgentPermission.scope_type == "user",
                    AgentPermission.scope_id == owner.id,
                )
            )
        ).scalar_one()
    assert grant_count == 1
    assert owner_grant_count == 0
    from app.core.permissions import get_agent_access_level_for_user_id
    async with async_session() as db:
        assert await get_agent_access_level_for_user_id(db, owner.id, agent) == "manage"


async def test_company_access_upsert_is_concurrency_safe_with_null_scope_id():
    from app.mcp_server.tools_config import set_agent_access_mode_impl
    from app.models.agent import AgentPermission

    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(owner, access_mode="company")

    token = await _pat(owner, scope="write")

    async def upsert_company_grant():
        result = await set_agent_access_mode_impl(
            _ctx(token), agent=str(agent.id), access_mode="company",
            company_access_level="use", confirm=True,
        )
        assert "✅" in result

    await asyncio.gather(*(upsert_company_grant() for _ in range(8)))

    async with async_session() as db:
        grants = (
            await db.execute(
                select(AgentPermission).where(
                    AgentPermission.agent_id == agent.id,
                    AgentPermission.scope_type == "company",
                    AgentPermission.scope_id.is_(None),
                )
            )
        ).scalars().all()
    assert len(grants) == 1
    assert grants[0].access_level == "use"


async def test_revoke_agent_access_removes_only_named_grants_and_protects_owner():
    from app.mcp_server.tools_config import revoke_agent_access_impl
    from app.models.agent import AgentPermission

    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id)
    user_a = await _seed_user(tenant_id=tenant.id)
    user_b = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(owner, access_mode="custom")
    async with async_session() as db:
        db.add_all([
            AgentPermission(agent_id=agent.id, scope_type="user", scope_id=owner.id, access_level="manage"),
            AgentPermission(agent_id=agent.id, scope_type="user", scope_id=user_a.id, access_level="use"),
            AgentPermission(agent_id=agent.id, scope_type="user", scope_id=user_b.id, access_level="use"),
        ])
        await db.commit()
    token = await _pat(owner, scope="write")

    protected = await revoke_agent_access_impl(
        _ctx(token), agent=str(agent.id), user_ids=[str(owner.id)], confirm=True
    )
    assert "✅" in protected
    from app.core.permissions import get_agent_access_level_for_user_id
    async with async_session() as db:
        assert await get_agent_access_level_for_user_id(db, owner.id, agent) == "manage"
    out = await revoke_agent_access_impl(
        _ctx(token), agent=str(agent.id), user_ids=[str(user_a.id)], confirm=True
    )
    assert "✅" in out
    async with async_session() as db:
        remaining_ids = set(
            (
                await db.execute(
                    select(AgentPermission.scope_id).where(
                        AgentPermission.agent_id == agent.id,
                        AgentPermission.scope_type == "user",
                    )
                )
            ).scalars().all()
        )
    assert owner.id not in remaining_ids
    assert user_a.id not in remaining_ids
    assert user_b.id in remaining_ids


async def test_get_agent_access_lists_department_grants():
    from app.mcp_server.tools_config import get_agent_access_impl
    from app.models.agent import AgentPermission
    from app.models.org import OrgDepartment

    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(owner, access_mode="custom")
    async with async_session() as db:
        department = OrgDepartment(
            tenant_id=tenant.id,
            name="Operations",
            path="Root/Operations",
            status="active",
        )
        db.add(department)
        await db.flush()
        db.add(
            AgentPermission(
                agent_id=agent.id,
                scope_type="department",
                scope_id=department.id,
                access_level="use",
            )
        )
        await db.commit()
        department_id = department.id
    token = await _pat(owner, scope="read")
    out = await get_agent_access_impl(_ctx(token), agent=str(agent.id))
    assert str(department_id) in out
    assert "Operations" in out


async def test_search_agent_access_subjects_discovers_ids_without_cross_tenant_leak():
    from app.mcp_server.tools_config import search_agent_access_subjects_impl
    from app.models.identity import IdentityProvider
    from app.models.org import OrgDepartment

    tenant = await _seed_tenant()
    other_tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id)
    local_user = await _seed_user(tenant_id=tenant.id)
    foreign_user = await _seed_user(tenant_id=other_tenant.id)
    agent = await _seed_agent(owner, access_mode="custom")
    async with async_session() as db:
        local = (await db.execute(select(User).where(User.id == local_user.id))).scalar_one()
        local.display_name = "Needle Local"
        foreign = (await db.execute(select(User).where(User.id == foreign_user.id))).scalar_one()
        foreign.display_name = "Needle Foreign"
        dingtalk_provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="dingtalk",
            name="华东钉钉",
            is_active=True,
        )
        wecom_provider = IdentityProvider(
            tenant_id=tenant.id,
            provider_type="wecom",
            name="华南企微",
            is_active=True,
        )
        foreign_provider = IdentityProvider(
            tenant_id=other_tenant.id,
            provider_type="dingtalk",
            name="其他租户钉钉",
            is_active=True,
        )
        db.add_all([dingtalk_provider, wecom_provider, foreign_provider])
        await db.flush()
        local_department = OrgDepartment(
            tenant_id=tenant.id,
            provider_id=dingtalk_provider.id,
            name="Needle Department",
            path="Root/Needle Department",
            status="active",
        )
        same_name_department = OrgDepartment(
            tenant_id=tenant.id,
            provider_id=wecom_provider.id,
            name="Needle Department",
            path="Root/Needle Department",
            status="active",
        )
        foreign_department = OrgDepartment(
            tenant_id=other_tenant.id,
            provider_id=foreign_provider.id,
            name="Needle Foreign Department",
            path="Root/Needle Foreign Department",
            status="active",
        )
        db.add_all([local_department, same_name_department, foreign_department])
        await db.commit()
        local_department_id = local_department.id
        same_name_department_id = same_name_department.id
        foreign_department_id = foreign_department.id
        dingtalk_provider_id = dingtalk_provider.id
        wecom_provider_id = wecom_provider.id

    token = await _pat(owner, scope="read")
    out = await search_agent_access_subjects_impl(
        _ctx(token), agent=str(agent.id), query="Needle", subject_type="all"
    )
    payload = json.loads(out)
    departments = {
        item["department_id"]: item
        for item in payload["items"]
        if item["subject_type"] == "department"
    }
    assert str(local_user.id) in out
    assert departments[str(local_department_id)] == {
        "subject_type": "department",
        "department_id": str(local_department_id),
        "name": "Needle Department",
        "path": "Root/Needle Department",
        "provider_id": str(dingtalk_provider_id),
        "provider_name": "华东钉钉",
        "provider_type": "dingtalk",
        "include_descendants": True,
    }
    assert departments[str(same_name_department_id)]["provider_id"] == str(wecom_provider_id)
    assert departments[str(same_name_department_id)]["provider_name"] == "华南企微"
    assert departments[str(same_name_department_id)]["provider_type"] == "wecom"
    assert str(foreign_user.id) not in out
    assert str(foreign_department_id) not in out
    assert "其他租户钉钉" not in out


async def test_list_agent_triggers():
    from app.mcp_server.tools_config import set_agent_trigger_impl, list_agent_triggers_impl
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")
    await set_agent_trigger_impl(_ctx(token), agent=str(agent.id), name="morning_cron",
                                 type="cron", config={"expr": "0 9 * * *"}, reason="daily brief")
    out = await list_agent_triggers_impl(_ctx(token), agent=str(agent.id))
    assert "morning_cron" in out


async def test_update_agent_trigger_disables():
    from app.mcp_server.tools_config import set_agent_trigger_impl, update_agent_trigger_impl
    from app.models.trigger import AgentTrigger
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    token = await _pat(user, scope="write")
    await set_agent_trigger_impl(_ctx(token), agent=str(agent.id), name="to_disable",
                                 type="cron", config={"expr": "0 8 * * *"}, reason="wake up")
    out = await update_agent_trigger_impl(_ctx(token), agent=str(agent.id),
                                          trigger="to_disable", is_enabled=False)
    assert "✅" in out
    # before→after should be visible
    assert "True" in out or "true" in out.lower()
    assert "False" in out or "false" in out.lower()
    async with async_session() as db:
        row = (await db.execute(select(AgentTrigger).where(
            AgentTrigger.agent_id == agent.id, AgentTrigger.name == "to_disable"))).scalar_one_or_none()
    assert row is not None and row.is_enabled is False
    assert row.execution_user_id == user.id


async def test_set_agent_tool_config_sets_config():
    from app.mcp_server.tools_config import set_agent_tool_config_impl
    from app.models.tool import AgentTool
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    agent = await _seed_agent(user)
    tool = await _seed_builtin_tool()
    token = await _pat(user)
    out = await set_agent_tool_config_impl(_ctx(token), agent=str(agent.id), tool=tool.name, config={"foo": "bar"})
    assert "✅" in out
    async with async_session() as db:
        row = (await db.execute(select(AgentTool).where(
            AgentTool.agent_id == agent.id, AgentTool.tool_id == tool.id))).scalar_one_or_none()
    assert row is not None and row.config and "foo" in row.config


async def test_set_agent_tool_config_allow_network_admin_only():
    from app.mcp_server.tools_config import set_agent_tool_config_impl
    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)  # role member
    agent = await _seed_agent(user)
    tool = await _seed_builtin_tool()
    token = await _pat(user)
    out = await set_agent_tool_config_impl(_ctx(token), agent=str(agent.id), tool=tool.name, config={"allow_network": True})
    assert "管理员" in out  # admin-only denial
