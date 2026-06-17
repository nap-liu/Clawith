"""PAT (Personal Access Token) service tests.

Covers: issue, verify, revoke, list — against real Postgres clawith_test DB.
asyncio_mode = "auto" (pyproject); no @pytest.mark.asyncio needed.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.database import async_session, engine
from app.models.tenant import Tenant
from app.models.user import Identity, User


# ── engine isolation (same pattern as test_session_introspection) ─────────────


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


# ── tests ─────────────────────────────────────────────────────────────────────


async def test_issue_pat_returns_plaintext_with_clw_prefix():
    """issue_pat returns a plaintext token starting with 'clw_'."""
    from app.services.pat_service import issue_pat

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)

    async with async_session() as db:
        token, row = await issue_pat(db, user=user, name="my-token")

    assert token.startswith("clw_"), f"Expected 'clw_' prefix, got: {token!r}"


async def test_issue_pat_stores_hash_not_plaintext():
    """Stored token_hash is sha256 hex (64 chars), not the plaintext."""
    from app.services.pat_service import issue_pat

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)

    async with async_session() as db:
        token, row = await issue_pat(db, user=user, name="my-token")

    expected_hash = hashlib.sha256(token.encode()).hexdigest()
    assert row.token_hash == expected_hash, "Stored hash must be sha256 of plaintext"
    assert len(row.token_hash) == 64, "sha256 hex digest is 64 chars"
    assert row.token_hash != token, "Plaintext must not be stored"


async def test_issue_pat_stores_correct_prefix():
    """token_prefix == token[:8]."""
    from app.services.pat_service import issue_pat

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)

    async with async_session() as db:
        token, row = await issue_pat(db, user=user, name="my-token")

    assert row.token_prefix == token[:8], "token_prefix must be the first 8 chars of the plaintext"


async def test_issue_pat_raises_if_user_has_no_tenant():
    """issue_pat raises ValueError when user.tenant_id is None."""
    from app.services.pat_service import issue_pat

    user = await _seed_user(tenant_id=None)

    async with async_session() as db:
        with pytest.raises(ValueError):
            await issue_pat(db, user=user, name="bad-token")


async def test_verify_pat_success_returns_user_and_tenant():
    """verify_pat returns (user, tenant_id) for a valid token."""
    from app.services.pat_service import issue_pat, verify_pat

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)

    async with async_session() as db:
        token, _ = await issue_pat(db, user=user, name="test-token")

    async with async_session() as db:
        returned_user, returned_tenant_id = await verify_pat(db, token)

    assert returned_user is not None, "verify_pat should return a user"
    assert returned_user.id == user.id
    assert returned_tenant_id == tenant.id


async def test_verify_pat_updates_last_used_at():
    """verify_pat refreshes last_used_at on the PAT row."""
    from app.services.pat_service import issue_pat, verify_pat
    from app.models.personal_access_token import PersonalAccessToken
    from sqlalchemy import select

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)

    async with async_session() as db:
        token, row = await issue_pat(db, user=user, name="test-token")
        pat_id = row.id

    assert row.last_used_at is None, "last_used_at should be None before first use"

    before = datetime.now(timezone.utc)

    async with async_session() as db:
        await verify_pat(db, token)

    async with async_session() as db:
        result = await db.execute(select(PersonalAccessToken).where(PersonalAccessToken.id == pat_id))
        pat = result.scalar_one()

    assert pat.last_used_at is not None, "last_used_at must be set after verify"
    # Postgres/asyncpg may return tz-naive; normalise to UTC for comparison
    stored = pat.last_used_at
    if stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    assert stored >= before, "last_used_at must be >= time before verify call"


async def test_verify_pat_after_revoke_returns_none():
    """After revoke_pat, verify_pat returns (None, None)."""
    from app.services.pat_service import issue_pat, revoke_pat, verify_pat

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)

    async with async_session() as db:
        token, row = await issue_pat(db, user=user, name="to-revoke")

    async with async_session() as db:
        revoked = await revoke_pat(db, user=user, token_id=row.id)

    assert revoked is True

    async with async_session() as db:
        returned_user, returned_tenant_id = await verify_pat(db, token)

    assert returned_user is None
    assert returned_tenant_id is None


async def test_verify_pat_garbage_token_returns_none():
    """Random non-clw_ token returns (None, None)."""
    from app.services.pat_service import verify_pat

    async with async_session() as db:
        u, t = await verify_pat(db, "not_a_real_token_xyz")

    assert u is None
    assert t is None


async def test_verify_pat_clw_prefix_but_wrong_hash_returns_none():
    """A token with clw_ prefix but no matching hash returns (None, None)."""
    from app.services.pat_service import verify_pat

    async with async_session() as db:
        u, t = await verify_pat(db, "clw_totally_made_up_token_that_does_not_exist")

    assert u is None
    assert t is None


async def test_verify_pat_expired_token_returns_none():
    """An expired PAT returns (None, None) even if not revoked."""
    from app.services.pat_service import issue_pat, verify_pat
    from app.models.personal_access_token import PersonalAccessToken
    from sqlalchemy import select, update

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)

    # Issue with a far-future expiry first, then backdate it
    async with async_session() as db:
        token, row = await issue_pat(db, user=user, name="expiring-token")
        pat_id = row.id

    # Manually set expires_at to the past
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    async with async_session() as db:
        await db.execute(
            update(PersonalAccessToken).where(PersonalAccessToken.id == pat_id).values(expires_at=past)
        )
        await db.commit()

    async with async_session() as db:
        u, t = await verify_pat(db, token)

    assert u is None, "Expired token should return None user"
    assert t is None, "Expired token should return None tenant_id"


async def test_revoke_pat_wrong_user_returns_false():
    """revoke_pat returns False when token doesn't belong to the given user."""
    from app.services.pat_service import issue_pat, revoke_pat

    tenant = await _seed_tenant()
    user1 = await _seed_user(tenant_id=tenant.id, name="U1")
    user2 = await _seed_user(tenant_id=tenant.id, name="U2")

    async with async_session() as db:
        token, row = await issue_pat(db, user=user1, name="u1-token")

    async with async_session() as db:
        revoked = await revoke_pat(db, user=user2, token_id=row.id)

    assert revoked is False, "Cannot revoke another user's token"


async def test_list_pats_excludes_revoked():
    """list_pats returns only non-revoked tokens for the user."""
    from app.services.pat_service import issue_pat, revoke_pat, list_pats

    tenant = await _seed_tenant()
    user = await _seed_user(tenant_id=tenant.id)

    async with async_session() as db:
        _, row1 = await issue_pat(db, user=user, name="keep")
        _, row2 = await issue_pat(db, user=user, name="revoke-me")

    async with async_session() as db:
        await revoke_pat(db, user=user, token_id=row2.id)

    async with async_session() as db:
        pats = await list_pats(db, user=user)

    ids = [p.id for p in pats]
    assert row1.id in ids, "Non-revoked token should appear in list"
    assert row2.id not in ids, "Revoked token must not appear in list"
