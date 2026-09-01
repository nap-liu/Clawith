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
from app.models.llm import LLMModel
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


async def _seed_model(tenant_id) -> LLMModel:
    async with async_session() as db:
        model = LLMModel(
            tenant_id=tenant_id,
            provider="openai",
            model=f"route-model-{uuid.uuid4().hex[:8]}",
            label="Route model",
            api_key_encrypted="test",
            enabled=True,
        )
        db.add(model); await db.commit(); await db.refresh(model); return model


@pytest.fixture
async def client():
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_create_agent_route_creates_native_agent(client):
    tenant = await _seed_tenant()
    user, jwt = await _seed_user_jwt(tenant_id=tenant.id)
    model = await _seed_model(tenant.id)
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
    assert a.daily_memory_load_days == 0

    updated = await client.patch(
        f"/api/agents/{agent_id}",
        json={
            "name": "Updated RouteScout",
            "role_description": "Updated role",
            "bio": "Updated bio",
            "welcome_message": "Updated welcome",
            "avatar_url": "https://example.test/avatar.png",
            "autonomy_policy": {"mode": "supervised"},
            "primary_model_id": str(model.id),
            "fallback_model_id": str(model.id),
            "temperature": 1.2,
            "context_window_size": 120,
            "daily_memory_load_days": 3,
            "max_tool_rounds": 60,
            "max_tokens_per_day": 12000,
            "max_tokens_per_month": 240000,
            "max_triggers": 25,
            "min_poll_interval_min": 10,
            "webhook_rate_limit": 4,
            "heartbeat_enabled": False,
            "heartbeat_interval_minutes": 300,
            "heartbeat_active_hours": "08:00-20:00",
            "timezone": "Asia/Shanghai",
            "im_thinking_output_enabled": True,
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert updated.status_code == 200, updated.text
    updated_body = updated.json()
    assert updated_body["name"] == "Updated RouteScout"
    assert updated_body["primary_model_id"] == str(model.id)
    assert updated_body["temperature"] == 1.2
    assert updated_body["daily_memory_load_days"] == 3
    assert updated_body["context_window_size"] == 120
    assert updated_body["im_thinking_output_enabled"] is True

    disabled = await client.patch(
        f"/api/agents/{agent_id}",
        json={"daily_memory_load_days": 0, "temperature": 0},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["daily_memory_load_days"] == 0
    assert disabled.json()["temperature"] == 0
    async with async_session() as db:
        persisted = await db.get(Agent, agent_id)
        assert persisted.daily_memory_load_days == 0
        assert persisted.temperature == 0
        assert persisted.name == "Updated RouteScout"
        assert persisted.avatar_url == "https://example.test/avatar.png"
        assert persisted.primary_model_id == model.id
        assert persisted.max_tool_rounds == 60
        assert persisted.heartbeat_enabled is False
        assert persisted.timezone == "Asia/Shanghai"

    other_tenant = await _seed_tenant()
    foreign_model = await _seed_model(other_tenant.id)
    rejected = await client.patch(
        f"/api/agents/{agent_id}",
        json={
            "name": "Must Not Persist",
            "primary_model_id": str(foreign_model.id),
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert rejected.status_code == 422
    async with async_session() as db:
        persisted = await db.get(Agent, agent_id)
        assert persisted.name == "Updated RouteScout"
        assert persisted.primary_model_id == model.id
