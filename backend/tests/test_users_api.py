"""User listing filters and pagination against real tenant data."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api.users import list_users
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.org import OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture
async def directory():
    await engine.dispose()
    try:
        async with async_session() as db:
            suffix = uuid.uuid4().hex
            tenants = [Tenant(name=name, slug=f"{name}-{suffix}") for name in ("a", "b")]
            db.add_all(tenants)
            await db.flush()
            identity = Identity(
                username=f"search-{suffix}", email=f"{suffix}@example.invalid",
                phone=str(uuid.uuid4().int)[:15], password_hash="test-only",
            )
            db.add(identity)
            await db.flush()
            base = datetime(2026, 1, 1, tzinfo=timezone.utc)
            users = [User(
                tenant_id=tenants[0].id, display_name=f"Person {index}",
                identity_id=identity.id if index == 0 else None,
                created_at=base + timedelta(minutes=index),
            ) for index in range(4)]
            foreign = User(
                tenant_id=tenants[1].id, identity_id=identity.id,
                display_name=users[0].display_name,
            )
            db.add_all([*users, foreign])
            await db.flush()
            db.add_all([
                OrgMember(tenant_id=tenants[0].id, user_id=users[0].id,
                          name="Directory profile", nickname="Tea", status="active"),
                OrgMember(tenant_id=tenants[1].id, user_id=foreign.id,
                          name="Other tenant profile", nickname="Tea", status="active"),
                *[Agent(name=f"Agent {index}", tenant_id=tenants[0].id,
                        creator_id=users[0].id, is_expired=index == 2)
                  for index in range(3)],
            ])
            await db.flush()
            yield SimpleNamespace(db=db, tenants=tenants, users=users,
                                  foreign=foreign, identity=identity)
            await db.rollback()
    finally:
        await engine.dispose()


async def _list(directory, *, role="org_admin", **query):
    return await list_users(
        current_user=SimpleNamespace(role=role, tenant_id=directory.tenants[0].id),
        db=directory.db,
        **{"tenant_id": None, "page": 1, "page_size": 2,
           "search": "", "sort_order": "desc", **query},
    )


async def test_list_users_returns_paginated_page_with_total_and_agent_counts(directory):
    first = await _list(directory)
    second = await _list(directory, page=2)
    assert first["total"] == second["total"] == 4
    assert first["page"] == 1 and second["page"] == 2
    assert first["page_size"] == second["page_size"] == 2
    assert [item.id for item in first["items"] + second["items"]] == [
        user.id for user in reversed(directory.users)
    ]
    assert [item.agents_count for item in second["items"]] == [0, 2]
    assert second["items"][1].nickname == "Tea"
    ascending = await _list(directory, sort_order="asc")
    assert [item.id for item in ascending["items"]] == [user.id for user in directory.users[:2]]


async def test_list_users_searches_identity_fields_and_org_admin_stays_inside_tenant(directory):
    for term in (directory.users[0].display_name, directory.identity.username,
                 directory.identity.email, directory.identity.phone, "Tea"):
        result = await _list(directory, tenant_id=str(directory.tenants[1].id), search=f" {term} ")
        assert result["total"] == 1, term
        assert [item.id for item in result["items"]] == [directory.users[0].id], term
    absent = await _list(directory, search="no-matching-person")
    assert absent["total"] == 0 and absent["items"] == []


async def test_platform_admin_can_page_requested_tenant(directory):
    result = await _list(directory, role="platform_admin", tenant_id=str(directory.tenants[1].id))
    assert result["total"] == 1
    assert [item.id for item in result["items"]] == [directory.foreign.id]
