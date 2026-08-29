"""Observable tenant-boundary tests for enterprise LLM model routes."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import create_access_token, encrypt_data
from app.database import async_session, engine
from app.main import app
from app.models.llm import LLMModel
from app.models.tenant import Tenant
from app.models.user import Identity, User

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_connections():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_tenant(name: str) -> Tenant:
    async with async_session() as db:
        tenant = Tenant(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
        db.add(tenant)
        await db.commit()
        await db.refresh(tenant)
        return tenant


async def _seed_admin(tenant_id: uuid.UUID) -> str:
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        identity = Identity(
            username=f"admin_{suffix}",
            email=f"admin_{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="Tenant admin",
            role="org_admin",
            is_active=True,
            tenant_id=tenant_id,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return create_access_token(str(user.id), user.role)


async def _seed_model(tenant_id: uuid.UUID, label: str) -> LLMModel:
    from app.api.enterprise import settings

    async with async_session() as db:
        model = LLMModel(
            tenant_id=tenant_id,
            provider="openai",
            model="gpt-test",
            label=label,
            api_key_encrypted=encrypt_data("secret-test-key", settings.SECRET_KEY),
            enabled=True,
            context_window=32_000,
            context_usage_ratio=0.7,
        )
        db.add(model)
        await db.commit()
        await db.refresh(model)
        return model


@pytest.fixture
async def seeded_scope():
    tenant_a = await _seed_tenant("TenantA")
    tenant_b = await _seed_tenant("TenantB")
    token = await _seed_admin(tenant_a.id)
    own_model = await _seed_model(tenant_a.id, "own")
    foreign_model = await _seed_model(tenant_b.id, "foreign")
    return tenant_a, tenant_b, token, own_model, foreign_model


async def test_org_admin_cannot_access_any_foreign_model_mutation(seeded_scope):
    tenant_a, tenant_b, token, _own_model, foreign_model = seeded_scope
    headers = {"Authorization": f"Bearer {token}"}
    create_body = {
        "provider": "openai",
        "model": "gpt-test",
        "api_key": "new-secret",
        "label": "cross-tenant",
        "context_window": 32_000,
        "context_usage_ratio": 0.7,
    }
    test_body = {
        "provider": "openai",
        "model": "gpt-test",
        "model_id": str(foreign_model.id),
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        responses = [
            await client.get(
                "/api/enterprise/llm-models",
                params={"tenant_id": str(tenant_b.id)},
                headers=headers,
            ),
            await client.post(
                "/api/enterprise/llm-models",
                params={"tenant_id": str(tenant_b.id)},
                json=create_body,
                headers=headers,
            ),
            await client.put(
                f"/api/enterprise/llm-models/{foreign_model.id}",
                json={"label": "hijacked"},
                headers=headers,
            ),
            await client.post(
                f"/api/enterprise/llm-models/{foreign_model.id}/set-default",
                headers=headers,
            ),
            await client.delete(
                f"/api/enterprise/llm-models/{foreign_model.id}",
                headers=headers,
            ),
            await client.post(
                "/api/enterprise/llm-test",
                json=test_body,
                headers=headers,
            ),
        ]

    assert tenant_a.id != tenant_b.id
    assert [response.status_code for response in responses] == [403] * len(responses)


async def test_org_admin_can_manage_models_inside_own_tenant(seeded_scope):
    tenant_a, _tenant_b, token, own_model, _foreign_model = seeded_scope
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get("/api/enterprise/llm-models", headers=headers)
        updated = await client.put(
            f"/api/enterprise/llm-models/{own_model.id}",
            json={"label": "own-updated"},
            headers=headers,
        )
        defaulted = await client.post(
            f"/api/enterprise/llm-models/{own_model.id}/set-default",
            headers=headers,
        )
        created = await client.post(
            "/api/enterprise/llm-models",
            json={
                "provider": "openai",
                "model": "gpt-second",
                "api_key": "second-secret",
                "label": "second",
                "context_window": 32_000,
                "context_usage_ratio": 0.7,
            },
            headers=headers,
        )

    assert listed.status_code == 200, listed.text
    assert {item["id"] for item in listed.json()} == {str(own_model.id)}
    assert updated.status_code == 200, updated.text
    assert updated.json()["label"] == "own-updated"
    assert defaulted.status_code == 204, defaulted.text
    assert created.status_code == 201, created.text

    async with async_session() as db:
        tenant = await db.get(Tenant, tenant_a.id)
        assert tenant is not None
        assert tenant.default_model_id == own_model.id
