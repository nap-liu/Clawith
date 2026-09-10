"""Observable REST/database coverage for the normalized Agent ACL."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.permissions import (
    build_agent_accessible_user_ids_query, build_visible_agents_query,
    get_agent_access_level_for_user_id,
)
from app.core.security import create_access_token
from app.database import async_session, engine
from app.models.agent import Agent, AgentPermission
from app.models.audit import AuditLog
from app.models.org import OrgDepartment, OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services.agent_permissions import update_agent_grants


@pytest.fixture
async def acl():
    await engine.dispose()
    async with async_session() as db:
        tenant = Tenant(name="Permission tenant", slug=f"acl-{uuid.uuid4().hex}")
        other = Tenant(name="Other tenant", slug=f"acl-{uuid.uuid4().hex}")
        db.add_all([tenant, other])
        await db.flush()
        users = {}
        for name in ("owner", "ordinary", "manager", "department", "admin", "foreign"):
            identity = Identity(username=f"acl-{name}-{uuid.uuid4().hex}", password_hash="test")
            db.add(identity)
            await db.flush()
            user = User(
                identity_id=identity.id, display_name=name,
                tenant_id=other.id if name == "foreign" else tenant.id,
                role="org_admin" if name == "admin" else "member", is_active=True,
            )
            db.add(user)
            await db.flush()
            users[name] = user
        parent = OrgDepartment(tenant_id=tenant.id, name="Operations", path="Operations", status="active")
        db.add(parent)
        await db.flush()
        child = OrgDepartment(
            tenant_id=tenant.id, name="Support", path="Operations/Support",
            parent_id=parent.id, status="active",
        )
        db.add(child)
        await db.flush()
        member = OrgMember(
            tenant_id=tenant.id, name="department", user_id=users["department"].id,
            department_id=child.id, department_path=child.path, status="active",
        )
        db.add(member)
        agent = Agent(name="Shared assistant", creator_id=users["owner"].id, tenant_id=tenant.id)
        db.add(agent)
        await db.flush()
        await update_agent_grants(db, agent, actor_id=users["owner"].id, data={"grants": []})
        await db.commit()
        context = {"agent": agent, "parent": parent, "child": child, "member": member, **users}
    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        context["client"] = client
        yield context
    await engine.dispose()


def headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id), user.role)}"}


def grant(kind, subject=None, level="use"):
    return {"scope_type": kind, "scope_id": str(subject) if subject else None, "access_level": level}


async def save(acl, payload, actor="owner"):
    return await acl["client"].put(
        f'/api/agents/{acl["agent"].id}/permissions', json=payload, headers=headers(acl[actor]),
    )


async def level(acl, who):
    async with async_session() as db:
        agent = await db.get(Agent, acl["agent"].id)
        return await get_agent_access_level_for_user_id(db, acl[who].id, agent)


async def test_company_and_scoped_grants_combine_and_revoke_independently(acl):
    grants = [
        grant("company"), grant("department", acl["parent"].id, "manage"),
        grant("user", acl["manager"].id, "manage"), grant("user", acl["department"].id),
    ]
    response = await save(acl, {"grants": grants})
    assert response.status_code == 200, response.text
    for who, expected in (("ordinary", "use"), ("manager", "manage"), ("department", "manage"), ("foreign", None)):
        assert await level(acl, who) == expected
    response = await acl["client"].get(
        f'/api/agents/{acl["agent"].id}/permissions', headers=headers(acl["manager"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["effective_access_level"] == "manage"
    assert len(response.json()["grants"]) == 4
    # A legacy company update must preserve the supplemental roster.
    response = await save(acl, {"scope_type": "company", "access_level": "use"})
    assert response.status_code == 200
    assert await level(acl, "manager") == "manage"
    response = await save(acl, {"grants": [g for g in grants if g["scope_id"] != str(acl["manager"].id)]})
    assert response.status_code == 200
    assert await level(acl, "manager") == "use"
    # Company access off leaves scoped grants effective and directory queries agree.
    response = await save(acl, {"scope_type": "custom"})
    assert response.status_code == 200
    assert await level(acl, "ordinary") is None
    assert await level(acl, "department") == "manage"
    async with async_session() as db:
        agent = await db.get(Agent, acl["agent"].id)
        eligible = set(await db.scalars(build_agent_accessible_user_ids_query(agent)))
        assert acl["department"].id in eligible and acl["ordinary"].id not in eligible
        visible = set(await db.scalars(build_visible_agents_query(acl["department"]).with_only_columns(Agent.id)))
        assert agent.id in visible
        member = await db.get(OrgMember, acl["member"].id)
        member.status = "inactive"
        await db.commit()
    # Department membership expires, leaving only the explicit use grant.
    assert await level(acl, "department") == "use"


async def test_invalid_or_unauthorized_changes_are_atomic_and_columns_cannot_grant_access(acl):
    assert (await save(acl, {"grants": [grant("company")]})).status_code == 200
    assert (await save(acl, {"grants": []}, "ordinary")).status_code == 403
    assert (await save(acl, {"grants": [grant("user", acl["foreign"].id, "manage")]})).status_code == 400
    invalid = grant("user", acl["manager"].id)
    invalid["access_level"] = "owner"
    assert (await save(acl, {"grants": [invalid]})).status_code == 422
    assert await level(acl, "ordinary") == "use"
    assert (await save(acl, {"grants": []})).status_code == 200
    async with async_session() as db:
        stored = await db.get(Agent, acl["agent"].id)
        stored.access_mode = "company"
        stored.company_access_level = "manage"
        await db.commit()
    assert await level(acl, "ordinary") is None
    assert await level(acl, "owner") == "manage"
    assert await level(acl, "admin") == "manage"
    async with async_session() as db:
        logs = list(await db.scalars(select(AuditLog).where(
            AuditLog.agent_id == acl["agent"].id, AuditLog.action == "agent_permissions_updated",
        )))
        assert len(logs) == 2
        assert logs[-1].details.keys() == {"before", "after"}


async def test_create_and_mcp_share_the_same_additive_grants(acl):
    from types import SimpleNamespace

    from app.mcp_server.tools_config_access import grant_agent_access_impl, revoke_agent_access_impl
    from app.services.pat_service import issue_pat

    response = await acl["client"].post('/api/agents/', headers=headers(acl["owner"]), json={
        "name": "Company assistant",
        "permission_grants": [grant("company"), grant("user", acl["manager"].id, "manage")],
    })
    assert response.status_code == 201, response.text
    async with async_session() as db:
        agent = await db.get(Agent, uuid.UUID(response.json()["id"]))
        assert await get_agent_access_level_for_user_id(db, acl["manager"].id, agent) == "manage"
        token, _ = await issue_pat(db, user=acl["owner"], name="ACL test", scope="write")
    context = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        headers={"authorization": f"Bearer {token}"},
    )))
    output = await grant_agent_access_impl(
        context, agent=str(agent.id), department_ids=[str(acl["parent"].id)], access_level="manage", confirm=True,
    )
    assert "✅" in output, output
    async with async_session() as db:
        assert await get_agent_access_level_for_user_id(db, acl["department"].id, agent) == "manage"
    output = await revoke_agent_access_impl(
        context, agent=str(agent.id), department_ids=[str(acl["parent"].id)], confirm=True,
    )
    assert "✅" in output, output
    async with async_session() as db:
        assert await get_agent_access_level_for_user_id(db, acl["department"].id, agent) == "use"
        grants = list(await db.scalars(select(AgentPermission).where(AgentPermission.agent_id == agent.id)))
        assert {(g.scope_type, g.access_level) for g in grants} == {("company", "use"), ("user", "manage")}
