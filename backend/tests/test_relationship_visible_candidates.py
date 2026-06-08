"""Tests for "manager can relate to any *visible* agent" — 数字员工关系.

Covers three production changes:
  1. search_visible_agents: candidate list filtered by visibility, not manage.
  2. save_agent_relationships: target must be visible (not necessarily managed).
  3. evaluate_agent_relationship_status: runtime gate relaxes the *target*
     side from manage -> view (source side stays manage).

Unit tests (stub DB) cover the permission helpers; integration tests
(in-memory aiosqlite, real ORM) cover the API endpoint functions.
"""

import importlib
import pkgutil
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.relationships as rel_api
from app.api.relationships import (
    AgentRelationshipBatchIn,
    AgentRelationshipIn,
    save_agent_relationships,
    search_visible_agents,
)
from app.core import permissions


# ─── Stub helpers (mirror test_agent_visibility.py) ───────────────────────


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ScalarsResult:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values


class _QueuedDb:
    """Session stub that returns queued results in order, one per execute()."""

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


# ─── Change 3a: user_can_view_agent_id helper ─────────────────────────────


@pytest.mark.asyncio
async def test_user_can_view_agent_id_true_for_company_use_access():
    """A regular user can *view* (use-level) a company agent they don't manage."""
    tenant = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant, is_active=True)
    agent = _make_agent(
        creator_id=uuid.uuid4(), tenant_id=tenant, access_mode="company", company_access_level="use"
    )
    # get_agent_access_level_for_user_id executes: (1) user lookup, (2) permissions lookup
    db = _QueuedDb([_ScalarResult(user), _ScalarsResult([])])

    assert await permissions.user_can_view_agent_id(db, user.id, agent) is True


@pytest.mark.asyncio
async def test_user_can_view_agent_id_false_for_others_private():
    """A regular user cannot view someone else's private agent → not a candidate."""
    tenant = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant, is_active=True)
    agent = _make_agent(creator_id=uuid.uuid4(), tenant_id=tenant, access_mode="private")
    db = _QueuedDb([_ScalarResult(user), _ScalarsResult([])])

    assert await permissions.user_can_view_agent_id(db, user.id, agent) is False


# ─── Change 3b: evaluate_agent_relationship_status relaxes target to view ──


class _SourceOnlyDb:
    """Returns the source agent for the single source lookup evaluate() does."""

    def __init__(self, source):
        self.source = source

    async def execute(self, _stmt):
        return _ScalarResult(self.source)


def _ready_agent(tenant_id, *, creator_id=None, access_mode="company"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=creator_id or uuid.uuid4(),
        access_mode=access_mode,
        status="ready",
        expires_at=None,
    )


@pytest.mark.asyncio
async def test_relationship_active_when_creator_manages_source_and_only_views_target(monkeypatch):
    """The whole point: creator manages the SOURCE and merely *sees* the TARGET
    (e.g. a company agent they only have use-access to) → relationship is active."""
    tenant = uuid.uuid4()
    creator_id = uuid.uuid4()
    source = _ready_agent(tenant, creator_id=creator_id, access_mode="private")
    target = _ready_agent(tenant, access_mode="company")
    rel = SimpleNamespace(
        agent_id=source.id,
        target_agent_id=target.id,
        target_agent=target,
        created_by_user_id=creator_id,
    )

    async def manages_source_only(_db, user_id, agent):
        return user_id == creator_id and agent is source

    async def views_target(_db, user_id, agent):
        return user_id == creator_id  # creator can see (use) the company target

    monkeypatch.setattr(permissions, "user_can_manage_agent_id", manages_source_only)
    monkeypatch.setattr(permissions, "user_can_view_agent_id", views_target)

    status = await permissions.evaluate_agent_relationship_status(_SourceOnlyDb(source), rel)

    assert status["access_allowed"] is True
    assert status["access_status"] == "active"


@pytest.mark.asyncio
async def test_relationship_restricted_when_creator_cannot_view_target(monkeypatch):
    """If the creator can no longer even see the target, the relationship is revoked."""
    tenant = uuid.uuid4()
    creator_id = uuid.uuid4()
    source = _ready_agent(tenant, creator_id=creator_id, access_mode="private")
    target = _ready_agent(tenant, access_mode="private")
    rel = SimpleNamespace(
        agent_id=source.id,
        target_agent_id=target.id,
        target_agent=target,
        created_by_user_id=creator_id,
    )

    async def manages_source_only(_db, user_id, agent):
        return user_id == creator_id and agent is source

    async def views_nothing(_db, _user_id, _agent):
        return False

    monkeypatch.setattr(permissions, "user_can_manage_agent_id", manages_source_only)
    monkeypatch.setattr(permissions, "user_can_view_agent_id", views_nothing)

    status = await permissions.evaluate_agent_relationship_status(_SourceOnlyDb(source), rel)

    assert status["access_allowed"] is False
    assert status["access_status"] == "restricted"


# ─── Integration harness: in-memory aiosqlite, real ORM ───────────────────


def _register_all_models():
    """Import every model module so FK targets resolve during create_all."""
    import app.models as m

    for _, name, _ in pkgutil.iter_modules(m.__path__):
        importlib.import_module(f"app.models.{name}")


@pytest.fixture
async def session():
    _register_all_models()
    from app.database import Base
    from app.models.agent import Agent, AgentPermission
    from app.models.org import AgentAgentRelationship
    from app.models.user import User

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        User.__table__,
        Agent.__table__,
        AgentPermission.__table__,
        AgentAgentRelationship.__table__,
    ]
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    Session = async_sessionmaker(eng, expire_on_commit=False)
    async with Session() as s:
        yield s
    await eng.dispose()


def _new_user(tenant_id, role="member"):
    from app.models.user import User

    return User(id=uuid.uuid4(), tenant_id=tenant_id, display_name="tester", role=role, is_active=True)


def _new_agent(tenant_id, creator_id, *, name, access_mode="company", company_access_level="use"):
    from app.models.agent import Agent

    return Agent(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        creator_id=creator_id,
        name=name,
        access_mode=access_mode,
        company_access_level=company_access_level,
    )


# ─── Change 1: candidate list filtered by visibility, not manage ──────────


@pytest.mark.asyncio
async def test_candidate_list_includes_visible_company_agent_user_cannot_manage(session):
    tenant = uuid.uuid4()
    user = _new_user(tenant)
    session.add(user)
    await session.flush()

    source = _new_agent(tenant, user.id, name="my-source", access_mode="private")
    public = _new_agent(tenant, uuid.uuid4(), name="public-bot", access_mode="company", company_access_level="use")
    others_private = _new_agent(tenant, uuid.uuid4(), name="secret-bot", access_mode="private")
    session.add_all([source, public, others_private])
    await session.flush()

    result = await search_visible_agents(agent_id=source.id, search=None, current_user=user, db=session)
    ids = {r["id"] for r in result}

    assert str(public.id) in ids  # visible company agent IS a candidate now
    assert str(others_private.id) not in ids  # not visible → not a candidate
    assert str(source.id) not in ids  # cannot relate an agent to itself

    pub = next(r for r in result if r["id"] == str(public.id))
    assert pub["can_manage"] is False  # regular user may relate without managing it


# ─── Change 2: save gate = visible (not manage) ───────────────────────────


@pytest.mark.asyncio
async def test_save_rejects_target_not_visible_to_user(session):
    tenant = uuid.uuid4()
    user = _new_user(tenant)
    session.add(user)
    await session.flush()

    source = _new_agent(tenant, user.id, name="my-source", access_mode="private")
    secret = _new_agent(tenant, uuid.uuid4(), name="secret-bot", access_mode="private")
    session.add_all([source, secret])
    await session.flush()

    data = AgentRelationshipBatchIn(
        relationships=[AgentRelationshipIn(target_agent_id=str(secret.id), relation="collaborator")]
    )
    with pytest.raises(HTTPException) as exc:
        await save_agent_relationships(agent_id=source.id, data=data, current_user=user, db=session)

    assert exc.value.status_code == 403
    assert "not visible" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_save_accepts_visible_but_unmanaged_target(session, monkeypatch):
    tenant = uuid.uuid4()
    user = _new_user(tenant)
    session.add(user)
    await session.flush()

    source = _new_agent(tenant, user.id, name="my-source", access_mode="private")
    public = _new_agent(tenant, uuid.uuid4(), name="public-bot", access_mode="company", company_access_level="use")
    session.add_all([source, public])
    await session.flush()

    async def _noop(*_args, **_kwargs):
        return None

    # relationships.md regeneration touches tables outside this test's schema; the
    # gate under test runs before/independently of it.
    monkeypatch.setattr(rel_api, "_regenerate_relationships_file", _noop)

    data = AgentRelationshipBatchIn(
        relationships=[AgentRelationshipIn(target_agent_id=str(public.id), relation="collaborator")]
    )
    out = await save_agent_relationships(agent_id=source.id, data=data, current_user=user, db=session)
    assert out["status"] == "ok"

    from app.models.org import AgentAgentRelationship

    rows = (
        await session.execute(
            select(AgentAgentRelationship).where(AgentAgentRelationship.agent_id == source.id)
        )
    ).scalars().all()
    assert [str(r.target_agent_id) for r in rows] == [str(public.id)]


@pytest.mark.asyncio
async def test_save_preserves_existing_relationship_to_now_invisible_target(session, monkeypatch):
    """Read/write symmetry: GET /agents returns every relationship unfiltered, so a member
    can *see* a row whose target is not visible to them (e.g. an admin-created link to
    someone else's private agent). A replace-all save replays that whole list, so those
    pre-existing rows must pass through — only *newly added* targets are visibility-checked.
    Otherwise the member is blocked from saving a list they were shown (the 403 bug)."""
    tenant = uuid.uuid4()
    user = _new_user(tenant)
    session.add(user)
    await session.flush()

    source = _new_agent(tenant, user.id, name="my-source", access_mode="private")
    # Someone else's private agent — not visible to this member, but already linked.
    invisible = _new_agent(tenant, uuid.uuid4(), name="secret-bot", access_mode="private")
    session.add_all([source, invisible])
    await session.flush()

    from app.models.org import AgentAgentRelationship

    # Pre-existing relationship (e.g. created earlier by an admin).
    session.add(
        AgentAgentRelationship(
            id=uuid.uuid4(),
            agent_id=source.id,
            target_agent_id=invisible.id,
            relation="collaborator",
            created_by_user_id=uuid.uuid4(),
        )
    )
    await session.flush()

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(rel_api, "_regenerate_relationships_file", _noop)

    # Member replays the full list (including the inherited, invisible-target row).
    data = AgentRelationshipBatchIn(
        relationships=[AgentRelationshipIn(target_agent_id=str(invisible.id), relation="collaborator")]
    )
    out = await save_agent_relationships(agent_id=source.id, data=data, current_user=user, db=session)
    assert out["status"] == "ok"

    rows = (
        await session.execute(
            select(AgentAgentRelationship).where(AgentAgentRelationship.agent_id == source.id)
        )
    ).scalars().all()
    assert [str(r.target_agent_id) for r in rows] == [str(invisible.id)]
