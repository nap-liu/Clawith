"""Tests for app/mcp_server/auth.py — PAT extraction and resolution.

Uses a minimal fake Context that exposes request_context.transport.headers,
matching the StreamableHTTP transport shape.

asyncio_mode = "auto" (pyproject); no @pytest.mark.asyncio needed.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import async_session, engine
from app.models.tenant import Tenant
from app.models.user import Identity, User


# ── engine isolation ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


# ── seed helpers (mirrored from test_pat_service.py) ─────────────────────────


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


async def _seed_user(tenant_id=None, name: str = "U") -> User:
    async with async_session() as db:
        ident = Identity(
            username=f"u_{uuid.uuid4().hex[:12]}",
            email=f"{uuid.uuid4().hex[:12]}@t.local",
            password_hash="x",
        )
        db.add(ident)
        await db.flush()
        u = User(
            identity_id=ident.id,
            display_name=name,
            role="member",
            is_active=True,
            tenant_id=tenant_id,
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        return u


# ── minimal fake MCP Context ─────────────────────────────────────────────────


class _FakeTransport:
    def __init__(self, headers: dict):
        self.headers = headers


class _FakeRequestContext:
    def __init__(self, headers: dict):
        self.transport = _FakeTransport(headers)


class _FakeCtx:
    """Minimal stand-in for mcp.server.fastmcp.Context."""

    def __init__(self, headers: dict | None = None):
        self.request_context = _FakeRequestContext(headers or {})


class _FakeRequest:
    def __init__(self, headers: dict):
        self.headers = headers


class _FakeCtxRequestOnly:
    """Ctx with no transport headers — exercises the ctx.request.headers fallback."""

    def __init__(self, headers: dict | None = None):
        self.request_context = None  # forces the fallback branch in _bearer_from_ctx
        self.request = _FakeRequest(headers or {})


# ── tests ─────────────────────────────────────────────────────────────────────


async def test_no_authorization_header_returns_none_none():
    """No Authorization header → (None, None)."""
    from app.mcp_server.auth import resolve_pat_user

    ctx = _FakeCtx(headers={})
    tenant = await _seed_tenant()
    await _seed_user(tenant_id=tenant.id)
    # user is seeded but ctx has no auth — must not match anything
    async with async_session() as db:
        u, t = await resolve_pat_user(ctx, db)
    assert u is None
    assert t is None


async def test_valid_bearer_token_returns_user_and_tenant():
    """A valid Bearer PAT resolves to the issuing user and their tenant."""
    from app.services.pat_service import issue_pat
    from app.mcp_server.auth import resolve_pat_user

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)

    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="mcp-test")

    ctx = _FakeCtx(headers={"authorization": f"Bearer {token}"})

    async with async_session() as db:
        returned_user, returned_tenant_id = await resolve_pat_user(ctx, db)

    assert returned_user is not None, "Should resolve to a user"
    assert returned_user.id == user.id
    assert returned_tenant_id == tenant.id


async def test_non_bearer_scheme_returns_none_none():
    """Authorization header with non-Bearer scheme → (None, None)."""
    from app.services.pat_service import issue_pat
    from app.mcp_server.auth import resolve_pat_user

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)

    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="mcp-basic")

    ctx = _FakeCtx(headers={"authorization": f"Basic {token}"})

    async with async_session() as db:
        u, t = await resolve_pat_user(ctx, db)

    assert u is None
    assert t is None


async def test_garbage_authorization_value_returns_none_none():
    """Random garbage in Authorization → (None, None)."""
    from app.mcp_server.auth import resolve_pat_user

    ctx = _FakeCtx(headers={"authorization": "totally-garbage-value"})

    async with async_session() as db:
        u, t = await resolve_pat_user(ctx, db)

    assert u is None
    assert t is None


async def test_revoked_token_returns_none_none():
    """After revoke, the same Bearer token resolves to (None, None)."""
    from app.services.pat_service import issue_pat, revoke_pat
    from app.mcp_server.auth import resolve_pat_user

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)

    async with async_session() as db:
        token, row = await issue_pat(db, user=user, name="mcp-revoke")

    async with async_session() as db:
        await revoke_pat(db, user=user, token_id=row.id)

    ctx = _FakeCtx(headers={"Authorization": f"Bearer {token}"})

    async with async_session() as db:
        u, t = await resolve_pat_user(ctx, db)

    assert u is None
    assert t is None


async def test_request_headers_fallback_resolves_user():
    """When transport headers are absent, ctx.request.headers is used (fallback)."""
    from app.services.pat_service import issue_pat
    from app.mcp_server.auth import resolve_pat_user

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)

    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="mcp-fallback")

    ctx = _FakeCtxRequestOnly(headers={"authorization": f"Bearer {token}"})

    async with async_session() as db:
        returned_user, returned_tenant_id = await resolve_pat_user(ctx, db)

    assert returned_user is not None, "fallback path must resolve the user"
    assert returned_user.id == user.id
    assert returned_tenant_id == tenant.id
