import uuid
from types import SimpleNamespace

import pytest

from app.core import permissions
from app.core.permissions import build_visible_agents_query


def make_user(**overrides):
    values = {
        "id": uuid.uuid4(),
        "role": "member",
        "tenant_id": uuid.uuid4(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_visible_agents_query_restricts_to_same_tenant_and_visible_permissions():
    user = make_user()

    stmt = build_visible_agents_query(user)
    sql = str(stmt)

    assert "agents.tenant_id" in sql
    assert "agents.creator_id" in sql
    assert "agent_permissions.scope_type" in sql
    assert "agent_permissions.scope_id" in sql


def test_build_visible_agents_query_platform_admin_sees_everything_in_tenant():
    """platform_admin is a cross-tenant operator; sees own tenant fully,
    including other users' private agents."""
    admin = make_user(role="platform_admin", tenant_id=None)

    sql = str(build_visible_agents_query(admin, tenant_id=uuid.uuid4()))

    assert "agents.tenant_id" in sql
    # No access_mode filter — platform_admin sees private agents too.
    assert "access_mode" not in sql
    # No per-user grant filter either.
    assert "agent_permissions" not in sql


def test_build_visible_agents_query_org_admin_hides_others_private():
    """org_admin is a tenant-level manager. Sees own + non-private agents
    (company + custom). Other users' private agents are filtered out
    to preserve v1.9.3 privacy guarantee."""
    admin = make_user(role="org_admin")

    sql = str(build_visible_agents_query(admin))

    assert "agents.tenant_id" in sql
    assert "agents.creator_id" in sql
    assert "access_mode" in sql
    # org_admin shouldn't need per-user grant table — non-private is enough.
    assert "agent_permissions" not in sql


def test_build_visible_agents_query_regular_user_uses_explicit_grants():
    """Regular users see own creations, company-visible agents, and any
    agent explicitly added to a custom roster they're on."""
    user = make_user(role="member")

    sql = str(build_visible_agents_query(user))

    assert "agents.tenant_id" in sql
    assert "agents.creator_id" in sql
    # Regular user needs both company-mode filter and explicit-grant filter.
    assert "access_mode" in sql
    assert "agent_permissions.scope_type" in sql
    assert "agent_permissions.scope_id" in sql


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ScalarsResult:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


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
async def test_access_level_org_admin_cannot_manage_others_private():
    """org_admin must NOT see someone else's private agent — v1.9.3 privacy."""
    tenant = uuid.uuid4()
    admin = SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=tenant, is_active=True)
    creator = uuid.uuid4()
    agent = _make_agent(creator_id=creator, tenant_id=tenant, access_mode="private")

    # First db.execute resolves the user; second resolves agent_permissions
    db = _AccessLevelDb([_ScalarResult(admin), _ScalarsResult([])])
    level = await permissions.get_agent_access_level_for_user_id(db, admin.id, agent)

    assert level is None


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
    assert status["access_status_reason"] == "relationship_creator_no_longer_manages_both_agents"


@pytest.mark.asyncio
async def test_agent_relationship_status_active_when_original_creator_still_manages_both_agents(monkeypatch):
    tenant_id = uuid.uuid4()
    creator_id = uuid.uuid4()
    source = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        access_mode="custom",
        status="ready",
        expires_at=None,
    )
    target = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
        access_mode="private",
        status="ready",
        expires_at=None,
    )
    rel = SimpleNamespace(
        agent_id=source.id,
        target_agent_id=target.id,
        target_agent=target,
        created_by_user_id=creator_id,
    )

    async def can_manage(_db, user_id, _agent):
        return user_id == creator_id

    monkeypatch.setattr(permissions, "user_can_manage_agent_id", can_manage)

    status = await permissions.evaluate_agent_relationship_status(
        _RelationshipStatusDb(source),
        rel,
    )

    assert status["access_allowed"] is True
    assert status["access_status"] == "active"
