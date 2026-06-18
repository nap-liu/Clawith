"""Smoke test for POST /api/agents/ (create_agent route).

Verifies that the REST route creates a native agent end-to-end.
Run BEFORE and AFTER the route-body swap to confirm behaviour is preserved.

Auth pattern: httpx.AsyncClient + ASGITransport + JWT from create_access_token,
mirrored from test_pat_api.py.
"""

from __future__ import annotations
import uuid
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from app.core.security import create_access_token
from app.database import async_session, engine
from app.models.tenant import Tenant
from app.models.user import Identity, User


@pytest.fixture(autouse=True)
async def _isolate():
    await engine.dispose(); yield; await engine.dispose()


async def _seed_tenant() -> Tenant:
    async with async_session() as db:
        t = Tenant(name="T", slug=f"t-{uuid.uuid4().hex[:10]}")
        db.add(t); await db.commit(); await db.refresh(t); return t


async def _seed_user_jwt(tenant_id=None, role="member"):
    async with async_session() as db:
        s = uuid.uuid4().hex[:12]
        ident = Identity(username=f"u_{s}", email=f"{s}@t.local", password_hash="x")
        db.add(ident); await db.flush()
        u = User(identity_id=ident.id, display_name="U", role=role, is_active=True, tenant_id=tenant_id)
        db.add(u); await db.commit(); await db.refresh(u)
        return u, create_access_token(str(u.id), role)


@pytest.fixture
async def client():
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_create_agent_route_creates_native_agent(client):
    tenant = await _seed_tenant()
    user, jwt = await _seed_user_jwt(tenant_id=tenant.id)
    r = await client.post("/api/agents/", json={"name": "RouteScout", "role_description": "r"},
                          headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["name"] == "RouteScout"
    agent_id = uuid.UUID(body["id"])
    from app.models.agent import Agent
    async with async_session() as db:
        a = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    assert a is not None and a.creator_id == user.id and a.tenant_id == tenant.id and a.access_mode == "company"
