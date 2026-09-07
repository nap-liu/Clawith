import uuid
from types import SimpleNamespace

import pytest

from app.core import permissions


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _AccessLevelDb:
    """Stub session returning queued results for the access-level helper."""

    def __init__(self, queued):
        self._queued = list(queued)

    async def execute(self, _stmt):
        return self._queued.pop(0)


def _make_agent(creator_id, tenant_id, access_mode="company", company_access_level=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=creator_id,
        tenant_id=tenant_id,
        access_mode=access_mode,
        company_access_level=company_access_level,
    )


@pytest.mark.asyncio
async def test_access_level_platform_admin_can_manage_others_private():
    """platform_admin retains manage access even on someone else's
    private agent — required for cross-tenant operations / audit."""
    tenant = uuid.uuid4()
    admin = SimpleNamespace(id=uuid.uuid4(), role="platform_admin", tenant_id=tenant, is_active=True)
    creator = uuid.uuid4()
    agent = _make_agent(creator_id=creator, tenant_id=tenant, access_mode="private")

    db = _AccessLevelDb([_ScalarResult(admin)])
    level = await permissions.get_agent_access_level_for_user_id(db, admin.id, agent)

    assert level == "manage"


@pytest.mark.asyncio
async def test_global_agent_access_still_allows_cross_tenant_platform_admin():
    admin = SimpleNamespace(
        id=uuid.uuid4(),
        role="platform_admin",
        tenant_id=uuid.uuid4(),
        is_active=True,
    )
    agent = _make_agent(
        creator_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        access_mode="private",
    )

    resolved, level = await permissions.check_agent_access(_AccessLevelDb([_ScalarResult(agent)]), admin, agent.id)

    assert resolved is agent
    assert level == "manage"


@pytest.mark.asyncio
async def test_access_level_org_admin_manages_others_private_standard_agent():
    """org_admin governs every standard Agent in their tenant."""
    tenant = uuid.uuid4()
    admin = SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=tenant, is_active=True)
    creator = uuid.uuid4()
    agent = _make_agent(creator_id=creator, tenant_id=tenant, access_mode="private")

    db = _AccessLevelDb([_ScalarResult(admin)])
    level = await permissions.get_agent_access_level_for_user_id(db, admin.id, agent)

    assert level == "manage"


@pytest.mark.asyncio
async def test_access_level_org_admin_manages_non_private():
    """org_admin manages company / custom agents in tenant even if not creator."""
    tenant = uuid.uuid4()
    admin = SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=tenant, is_active=True)
    creator = uuid.uuid4()
    agent = _make_agent(creator_id=creator, tenant_id=tenant, access_mode="company")

    db = _AccessLevelDb([_ScalarResult(admin)])
    level = await permissions.get_agent_access_level_for_user_id(db, admin.id, agent)

    assert level == "manage"


class _RelationshipStatusDb:
    def __init__(self, source):
        self.source = source

    async def execute(self, _stmt):
        return _ScalarResult(self.source)

    async def scalar(self, _stmt):
        return None


@pytest.mark.asyncio
async def test_agent_relationship_status_rejects_cross_tenant_edge_before_creator_permissions():
    source = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        status="ready",
        expires_at=None,
    )
    target = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        status="ready",
        expires_at=None,
    )
    rel = SimpleNamespace(
        agent_id=source.id,
        target_agent_id=target.id,
        target_agent=target,
        created_by_user_id=uuid.uuid4(),
    )

    status = await permissions.evaluate_agent_relationship_status(_RelationshipStatusDb(source), rel)

    assert status == {
        "access_allowed": False,
        "access_status": "restricted",
        "access_status_reason": "different_tenant",
    }


@pytest.mark.asyncio
async def test_agent_relationship_status_requires_original_creator_to_still_manage_both_agents(monkeypatch):
    tenant_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    source = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        access_mode="company",
        status="ready",
        expires_at=None,
    )
    target = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        access_mode="company",
        status="ready",
        expires_at=None,
    )
    rel = SimpleNamespace(
        agent_id=source.id,
        target_agent_id=target.id,
        target_agent=target,
        created_by_user_id=creator_id,
    )

    async def cannot_manage(_db, _user_id, _agent):
        return False

    monkeypatch.setattr(permissions, "user_can_manage_agent_id", cannot_manage)

    status = await permissions.evaluate_agent_relationship_status(
        _RelationshipStatusDb(source),
        rel,
        current_user_id=uuid.uuid4(),
    )

    assert status["access_allowed"] is False
    assert status["access_status"] == "restricted"
    assert status["access_status_reason"] == "relationship_creator_no_longer_has_access_to_both_agents"
