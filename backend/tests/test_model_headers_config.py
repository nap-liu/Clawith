"""Header persistence, tenant secrecy and connection-test behavior through HTTP."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from app.database import async_session, engine
from app.main import app
from app.models.llm import LLMModel
from app.models.user import User
from app.services.llm.runtime_model import RuntimeLLMModel
from app.services.model_headers import resolve_model_headers
from tests.test_enterprise_llm_tenant_scope import _seed_admin, _seed_tenant

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def isolate_connections():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_headers_defaults_encryption_clone_probe_and_member_secrecy(monkeypatch):
    from app.api import enterprise_routes_models as routes

    tenant = await _seed_tenant("Headers")
    token = await _seed_admin(tenant.id)
    observed = []

    def client_factory(**kwargs):
        observed.append(kwargs)
        return SimpleNamespace(stream=AsyncMock(return_value=SimpleNamespace(content="ok")), close=AsyncMock())

    monkeypatch.setattr(routes, "create_llm_client", client_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as client:
        created = await client.post("/api/enterprise/llm-models", json={
            "provider": "qwen", "model": "qwen-test", "api_key": "test-key", "label": "Headers",
        })
        assert created.status_code == 201, created.text
        assert created.json()["extra_headers"] == {"X-DashScope-Wait-Timeout": "120"}
        model_id = created.json()["id"]
        endpoint = f"/api/enterprise/llm-models/{model_id}"
        custom = {"X-DashScope-Wait-Timeout": "60", "X-Custom-Token": "secret-header-value"}
        changed = await client.put(endpoint, json={"extra_headers": custom})
        assert changed.status_code == 200 and changed.json()["extra_headers"] == custom
        async with async_session() as db:
            stored = await db.get(LLMModel, uuid.UUID(model_id))
            assert "secret-header-value" not in stored.extra_headers_encrypted
            snapshot = RuntimeLLMModel.from_orm(stored)
        clone = await client.post(endpoint + "/clone", json={"model": "qwen-clone", "label": "Clone"})
        assert clone.status_code == 200 and clone.json()["extra_headers"] == custom
        unchanged = await client.put(endpoint, json={"label": "Renamed"})
        assert unchanged.json()["extra_headers"] == custom
        probe = {"provider": "qwen", "model": "qwen-test", "model_id": model_id}
        tested = await client.post("/api/enterprise/llm-test", json=probe)
        assert tested.json()["success"] and observed[-1]["extra_headers"] == custom
        await client.post("/api/enterprise/llm-test", json={**probe, "extra_headers": {}})
        assert observed[-1]["extra_headers"] == {}
        cleared = await client.put(endpoint, json={"extra_headers": {}})
        assert cleared.json()["extra_headers"] == {}
        assert resolve_model_headers(snapshot) == custom
        reset = await client.put(endpoint, json={"extra_headers": None})
        assert reset.json()["extra_headers"] == {"X-DashScope-Wait-Timeout": "120"}
        await client.post("/api/enterprise/llm-test", json={**probe, "base_url": "https://new.example/v1"})
        assert observed[-1]["extra_headers"] is None  # Derive defaults from the edited endpoint.
        for bad in ({"Bad\r\nName": "value"}, {"X-Test": "value\r\nInjected: yes"}):
            rejected = await client.put(endpoint, json={"extra_headers": bad})
            assert rejected.status_code == 422
        other = await _seed_tenant("HeadersOther")
        foreign = await client.get("/api/enterprise/llm-models", params={"tenant_id": str(other.id)})
        assert foreign.status_code == 403
        async with async_session() as db:
            await db.execute(update(User).where(User.tenant_id == tenant.id).values(role="member"))
            await db.commit()
        listed = await client.get("/api/enterprise/llm-models")
        assert listed.status_code == 200
        assert all(row["extra_headers"] == {} for row in listed.json())
        assert "secret-header-value" not in listed.text
        denied = await client.put(endpoint, json={"extra_headers": custom})
        assert denied.status_code == 403
