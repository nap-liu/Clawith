"""API-level tests for /api/personal-access-tokens.

Tests the PAT management REST endpoints:
  POST   /api/personal-access-tokens      create
  GET    /api/personal-access-tokens      list
  DELETE /api/personal-access-tokens/{id} revoke

Auth pattern: httpx.AsyncClient + ASGITransport + JWT from create_access_token,
mirrored from test_mcp_server_created_by.py and test_mcp_servers_api.py.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import create_access_token
from app.database import async_session, engine
from app.models.personal_access_token import PersonalAccessToken
from app.models.tenant import Tenant
from app.models.user import Identity, User

pytestmark = pytest.mark.asyncio


# ── engine isolation ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _isolate():
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


async def _seed_user(tenant_id=None, role: str = "member") -> tuple[User, str]:
    """Create a user and return (user, jwt_token)."""
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        ident = Identity(
            username=f"u_{suffix}",
            email=f"{suffix}@t.local",
            password_hash="x",
        )
        db.add(ident)
        await db.flush()
        u = User(
            identity_id=ident.id,
            display_name="U",
            role=role,
            is_active=True,
            tenant_id=tenant_id,
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
        token = create_access_token(str(u.id), role)
        return u, token


# ── shared client fixture ──────────────────────────────────────────────────────


@pytest.fixture
async def client():
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── tests ─────────────────────────────────────────────────────────────────────


async def test_create_pat_returns_plaintext_token_and_prefix(client):
    """POST creates a PAT; response includes plaintext token (clw_ prefix) and token_prefix."""
    tenant = await _seed_tenant()
    user, token = await _seed_user(tenant_id=tenant.id)

    resp = await client.post(
        "/api/personal-access-tokens",
        json={"name": "my-mcp-token"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 201), resp.text
    body = resp.json()
    assert body["token"].startswith("clw_"), f"Expected clw_ prefix, got: {body['token']!r}"
    assert body["token_prefix"] == body["token"][:8]
    assert body["name"] == "my-mcp-token"
    assert "id" in body
    assert "created_at" in body


async def test_create_pat_row_exists_in_db(client):
    """PAT row is written to DB after successful POST."""
    tenant = await _seed_tenant()
    user, token = await _seed_user(tenant_id=tenant.id)

    resp = await client.post(
        "/api/personal-access-tokens",
        json={"name": "db-check-token"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code in (200, 201), resp.text
    pat_id = uuid.UUID(resp.json()["id"])

    async with async_session() as db:
        result = await db.execute(
            select(PersonalAccessToken).where(PersonalAccessToken.id == pat_id)
        )
        row = result.scalar_one_or_none()

    assert row is not None
    assert row.user_id == user.id
    assert row.tenant_id == tenant.id


async def test_list_pats_does_not_include_plaintext_token(client):
    """GET returns list without the plaintext `token` field."""
    tenant = await _seed_tenant()
    user, jwt = await _seed_user(tenant_id=tenant.id)

    # Create one PAT first
    await client.post(
        "/api/personal-access-tokens",
        json={"name": "list-me"},
        headers={"Authorization": f"Bearer {jwt}"},
    )

    resp = await client.get(
        "/api/personal-access-tokens",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert isinstance(items, list)
    assert len(items) >= 1

    found = next((it for it in items if it["name"] == "list-me"), None)
    assert found is not None
    assert "token" not in found, "Plaintext token must NOT appear in list response"
    assert "token_prefix" in found


async def test_delete_own_token_succeeds(client):
    """DELETE revokes the caller's own token; subsequent GET excludes it."""
    tenant = await _seed_tenant()
    user, jwt = await _seed_user(tenant_id=tenant.id)

    # Create a PAT
    create_resp = await client.post(
        "/api/personal-access-tokens",
        json={"name": "to-revoke"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert create_resp.status_code in (200, 201)
    pat_id = create_resp.json()["id"]

    # Revoke it
    del_resp = await client.delete(
        f"/api/personal-access-tokens/{pat_id}",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert del_resp.status_code in (200, 204), del_resp.text

    # Should no longer appear in list
    list_resp = await client.get(
        "/api/personal-access-tokens",
        headers={"Authorization": f"Bearer {jwt}"},
    )
    ids_in_list = [it["id"] for it in list_resp.json()]
    assert pat_id not in ids_in_list


async def test_delete_another_users_token_returns_404(client):
    """DELETE of another user's token returns 404 (ownership check)."""
    tenant = await _seed_tenant()
    owner, owner_jwt = await _seed_user(tenant_id=tenant.id)
    attacker, attacker_jwt = await _seed_user(tenant_id=tenant.id)

    # Owner creates a PAT
    create_resp = await client.post(
        "/api/personal-access-tokens",
        json={"name": "owners-token"},
        headers={"Authorization": f"Bearer {owner_jwt}"},
    )
    assert create_resp.status_code in (200, 201)
    pat_id = create_resp.json()["id"]

    # Attacker tries to delete it — must get 404
    del_resp = await client.delete(
        f"/api/personal-access-tokens/{pat_id}",
        headers={"Authorization": f"Bearer {attacker_jwt}"},
    )
    assert del_resp.status_code == 404, del_resp.text


async def test_create_pat_without_tenant_returns_400(client):
    """User with no tenant_id gets 400 on PAT creation."""
    # seed user with no tenant
    user, jwt = await _seed_user(tenant_id=None)

    resp = await client.post(
        "/api/personal-access-tokens",
        json={"name": "no-tenant"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert resp.status_code == 400, resp.text


async def test_unauthenticated_create_returns_401_or_403(client):
    """No Authorization header → 401 or 403."""
    resp = await client.post(
        "/api/personal-access-tokens",
        json={"name": "anon"},
    )
    assert resp.status_code in (401, 403), resp.text


async def test_unauthenticated_list_returns_401_or_403(client):
    """No Authorization header → 401 or 403 on GET."""
    resp = await client.get("/api/personal-access-tokens")
    assert resp.status_code in (401, 403), resp.text
