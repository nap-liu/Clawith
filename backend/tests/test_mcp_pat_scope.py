"""TDD test for PAT scope column (Task A1) + scope-aware service (Task A2)."""

from __future__ import annotations

import uuid

import pytest

from app.database import async_session, engine
from app.models.personal_access_token import PersonalAccessToken
from app.models.tenant import Tenant
from app.models.user import Identity, User


# ── engine isolation ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


# ── seed helpers ──────────────────────────────────────────────────────────────


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def _seed_user(tenant_id=None) -> User:
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


# ── Task A1: model column default ─────────────────────────────────────────────


def test_pat_model_has_scope_default_read():
    # python-side column default must resolve to "read"
    assert PersonalAccessToken.__table__.c.scope.default.arg == "read"


# ── Task A2: scope-aware service ──────────────────────────────────────────────


async def test_issue_pat_write_scope_round_trips():
    from app.services.pat_service import issue_pat, verify_pat_with_scope

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    async with async_session() as db:
        token, row = await issue_pat(db, user=user, name="ci", scope="write")
    assert row.scope == "write"
    async with async_session() as db:
        u, tid, scope = await verify_pat_with_scope(db, token)
    assert u.id == user.id and tid == tenant.id and scope == "write"


async def test_issue_pat_defaults_to_read():
    from app.services.pat_service import issue_pat, verify_pat_with_scope

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    async with async_session() as db:
        token, row = await issue_pat(db, user=user, name="ro")
    assert row.scope == "read"
    async with async_session() as db:
        _u, _t, scope = await verify_pat_with_scope(db, token)
    assert scope == "read"


async def test_issue_pat_rejects_bad_scope():
    from app.services.pat_service import issue_pat

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    async with async_session() as db:
        with pytest.raises(ValueError):
            await issue_pat(db, user=user, name="x", scope="admin")


async def test_verify_pat_still_returns_two_tuple():
    # Regression guard: verify_pat must stay a 2-tuple.
    from app.services.pat_service import issue_pat, verify_pat

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="t")
    async with async_session() as db:
        result = await verify_pat(db, token)
    assert len(result) == 2 and result[0].id == user.id


# ── Task A3: MCP auth-layer PatContext ────────────────────────────────────────

from types import SimpleNamespace


def _ctx(token):
    # primary header path: ctx.request_context.request.headers
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers=headers)))


async def test_resolve_pat_context_carries_scope():
    from app.services.pat_service import issue_pat
    from app.mcp_server.auth import resolve_pat_context

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="w", scope="write")
    async with async_session() as db:
        pc = await resolve_pat_context(_ctx(token), db)
    assert pc is not None and pc.scope == "write" and pc.user.id == user.id and pc.tenant_id == tenant.id


async def test_resolve_pat_user_still_two_tuple():
    from app.services.pat_service import issue_pat
    from app.mcp_server.auth import resolve_pat_user

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)
    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="r")
    async with async_session() as db:
        result = await resolve_pat_user(_ctx(token), db)
    assert len(result) == 2 and result[0].id == user.id


async def test_resolve_pat_context_none_when_unauth():
    from app.mcp_server.auth import resolve_pat_context

    async with async_session() as db:
        assert await resolve_pat_context(_ctx(None), db) is None


def test_require_write_gate():
    from types import SimpleNamespace
    from app.mcp_server.auth import require_write

    assert require_write(SimpleNamespace(scope="write")) is True
    assert require_write(SimpleNamespace(scope="read")) is False
