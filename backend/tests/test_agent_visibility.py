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


def _where_clause(stmt) -> str:
    """Extract the SQL WHERE substring. ``str(select)`` includes the
    SELECT projection (every column name like ``agents.access_mode``),
    which would pollute filter-shape assertions; we only care about
    what's in the WHERE clause."""
    sql = str(stmt)
    idx = sql.find("\nWHERE")
    if idx == -1:
        idx = sql.find(" WHERE")
    return sql[idx:] if idx != -1 else ""


def test_build_visible_agents_query_platform_admin_sees_everything_in_tenant():
    """platform_admin is a cross-tenant operator; sees own tenant fully,
    including other users' private agents."""
    admin = make_user(role="platform_admin", tenant_id=None)

    where = _where_clause(build_visible_agents_query(admin, tenant_id=uuid.uuid4()))

    assert "agents.tenant_id" in where
    # No access_mode filter in WHERE — platform_admin sees private agents too.
    assert "access_mode" not in where
    # No per-user grant filter either.
    assert "agent_permissions" not in where


def test_build_visible_agents_query_org_admin_hides_others_private():
    """org_admin is a tenant-level manager. Sees own + non-private agents
    (company + custom). Other users' private agents are filtered out
    to preserve v1.9.3 privacy guarantee."""
    admin = make_user(role="org_admin")

    where = _where_clause(build_visible_agents_query(admin))

    assert "agents.tenant_id" in where
    assert "agents.creator_id" in where
    assert "access_mode" in where
    # org_admin shouldn't need per-user grant table — non-private is enough.
    assert "agent_permissions" not in where


def test_build_visible_agents_query_regular_user_uses_explicit_grants():
    """Regular users see own creations, company-visible agents, and any
    agent explicitly added to a custom roster they're on."""
    user = make_user(role="member")

    where = _where_clause(build_visible_agents_query(user))

    assert "agents.tenant_id" in where
    assert "agents.creator_id" in where
    # Regular user needs both company-mode filter and explicit-grant filter.
    assert "access_mode" in where
    assert "agent_permissions.scope_type" in where
    assert "agent_permissions.scope_id" in where


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ScalarsResult:
    """Stub mimicking sqlalchemy Result.scalars().all() chain."""

    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

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
async def test_global_agent_access_still_allows_cross_tenant_platform_admin():
    admin = SimpleNamespace(
        id=uuid.uuid4(), role="platform_admin", tenant_id=uuid.uuid4(), is_active=True,
    )
    agent = _make_agent(
        creator_id=uuid.uuid4(), tenant_id=uuid.uuid4(), access_mode="private",
    )

    resolved, level = await permissions.check_agent_access(
        _AccessLevelDb([_ScalarResult(agent)]), admin, agent.id
    )

    assert resolved is agent
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

    async def scalar(self, _stmt):
        return None


@pytest.mark.asyncio
async def test_agent_relationship_status_rejects_cross_tenant_edge_before_creator_permissions():
    source = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), status="ready", expires_at=None,
    )
    target = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), status="ready", expires_at=None,
    )
    rel = SimpleNamespace(
        agent_id=source.id,
        target_agent_id=target.id,
        target_agent=target,
        created_by_user_id=uuid.uuid4(),
    )

    status = await permissions.evaluate_agent_relationship_status(
        _RelationshipStatusDb(source), rel
    )

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

    async def can_access(_db, user_id, _agent):
        return user_id == creator_id

    # Source side checks manage; target side now checks view (manage implies view,
    # so a creator who manages both passes both gates).
    monkeypatch.setattr(permissions, "user_can_manage_agent_id", can_access)
    monkeypatch.setattr(permissions, "user_can_view_agent_id", can_access)

    status = await permissions.evaluate_agent_relationship_status(
        _RelationshipStatusDb(source),
        rel,
    )

    assert status["access_allowed"] is True
    assert status["access_status"] == "active"
