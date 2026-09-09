"""Model pool behavior through real HTTP, storage and provider dispatch."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import async_session, engine
from app.main import app
from app.models.llm import LLMModel
from app.models.tenant import Tenant
from app.services.llm.client_registry import create_llm_client, resolve_api_protocol
from app.services.llm.client_openai_compatible import OpenAICompatibleClient
from app.services.llm.client_openai_responses import OpenAIResponsesClient
from app.services.llm.runtime_model import RuntimeLLMModel
from app.services.llm_model_config import clone_tenant_llm_model
from app.services.tool_config import get_tenant_tool_config
from tests.test_enterprise_llm_tenant_scope import _seed_admin, _seed_tenant

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def isolate_connections():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_unified_pool_defaults_clone_and_conversation_filter():
    tenant = await _seed_tenant("Pool")
    other = await _seed_tenant("OtherPool")
    token = await _seed_admin(tenant.id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as client:
        generated = await client.post("/api/enterprise/llm-models", json={
            "provider": "qwen", "api_protocol": "openai_compatible", "model": "wan-test", "api_key": "test-key",
            "label": "video", "purposes": ["video_generation"], "input_modalities": ["text", "image"],
        })
        assert generated.status_code == 201, generated.text
        video = generated.json()
        assert video["supports_vision"] is True
        assert video["effective_api_protocol"] == "openai_compatible"
        async with async_session() as db:
            assert (await db.get(Tenant, tenant.id)).default_model_id is None
            stored = await db.get(LLMModel, uuid.UUID(video["id"]))
            snapshot = RuntimeLLMModel.from_orm(stored)
            assert snapshot.api_protocol == "openai_compatible"
            assert snapshot.purposes == ("video_generation",)
            clone, _ = await clone_tenant_llm_model(db, source_model_id=stored.id, tenant_id=tenant.id,
                                                   model_key="wan-clone", label="clone")
            assert clone.purposes == stored.purposes and clone.input_modalities == stored.input_modalities
            assert clone.api_protocol == stored.api_protocol
            await db.rollback()
        refused = await client.post(f'/api/enterprise/llm-models/{video["id"]}/set-default')
        assert refused.status_code == 422, refused.text
        rejected_agent = await client.post("/api/agents/", json={"name": "Wrong model", "primary_model_id": video["id"]})
        assert rejected_agent.status_code == 400, rejected_agent.text
        default = await client.put("/api/enterprise/media-model-defaults", json={"video_model_id": video["id"]})
        assert default.status_code == 200, default.text
        wrong = await client.put("/api/enterprise/media-model-defaults", json={"image_model_id": video["id"]})
        assert wrong.status_code == 422
        foreign = await client.get("/api/enterprise/media-model-defaults", params={"tenant_id": str(other.id)})
        assert foreign.status_code == 403
        chat = await client.post("/api/enterprise/llm-models", json={
            "provider": "openai", "api_protocol": "openai_responses", "model": "gpt-test", "api_key": "test-key", "label": "chat",
        })
        assert chat.status_code == 201, chat.text
        listed = await client.get("/api/enterprise/llm-models", params={"purpose": "conversation"})
        assert [row["id"] for row in listed.json()] == [chat.json()["id"]]
        listed = await client.get("/api/enterprise/llm-models", params={"purpose": "video_generation"})
        assert [row["id"] for row in listed.json()] == [video["id"]]
        async with async_session() as db:
            assert (await get_tenant_tool_config(db, tenant.id, "media_ai"))["video_model_id"] == video["id"]
        bad = await client.put(f'/api/enterprise/llm-models/{chat.json()["id"]}', json={"purposes": ["image_generation"]})
        assert bad.status_code == 409
        invalid = await client.post("/api/enterprise/llm-models", json={"provider": "openai", "model": "invalid", "api_key": "test", "label": "invalid", "purposes": []})
        assert invalid.status_code == 422
        deleted = await client.delete(f'/api/enterprise/llm-models/{video["id"]}')
        assert deleted.status_code == 204, deleted.text
        defaults = await client.get("/api/enterprise/media-model-defaults")
        assert defaults.json()["video_model_id"] is None


async def test_explicit_protocol_and_legacy_registry_dispatch():
    assert resolve_api_protocol("openai-response") == "openai_responses"
    assert resolve_api_protocol("qwen") == "openai_compatible"
    assert resolve_api_protocol("qwen", "openai_responses") == "openai_responses"
    responses = create_llm_client("qwen", "test", "qwen-test", api_protocol="openai_responses")
    legacy = create_llm_client("qwen", "test", "qwen-test")
    try:
        assert isinstance(responses, OpenAIResponsesClient)
        assert isinstance(legacy, OpenAICompatibleClient)
    finally:
        await responses.close()
        await legacy.close()


@pytest.mark.parametrize(("stored_limit", "request_fields", "expected_limit", "expected_effort"), [
    (None, {}, 1024, "high"),
    (512, {}, 512, "high"),
    (4096, {"max_output_tokens": 256, "reasoning_effort": "low"}, 256, "low"),
])
async def test_saved_model_connection_test_uses_protocol_snapshot(
    monkeypatch, stored_limit, request_fields, expected_limit, expected_effort,
):
    from app.api import enterprise_routes_models as routes
    tenant = await _seed_tenant("Protocol")
    token = await _seed_admin(tenant.id)
    captured = {}
    stream = AsyncMock(return_value=SimpleNamespace(content="ok"))
    def factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(stream=stream, close=AsyncMock())
    monkeypatch.setattr(routes, "create_llm_client", factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as client:
        created = await client.post("/api/enterprise/llm-models", json={
            "provider": "qwen", "api_protocol": "openai_responses", "model": "qwen-test", "api_key": "saved-test", "label": "responses",
            "max_output_tokens": stored_limit, "reasoning_effort": "high",
        })
        assert created.status_code == 201, created.text
        tested = await client.post("/api/enterprise/llm-test", json={"provider": "qwen", "model": "qwen-test", "model_id": created.json()["id"], **request_fields})
        assert tested.status_code == 200 and tested.json()["success"], tested.text
        assert captured["api_protocol"] == "openai_responses"
        assert captured["api_key"] == "saved-test"
        assert stream.await_args.kwargs["max_tokens"] == expected_limit
        assert stream.await_args.kwargs["reasoning_effort"] == expected_effort
        stream.side_effect = RuntimeError("response.incomplete: max_output_tokens")
        incomplete = await client.post("/api/enterprise/llm-test", json={
            "provider": "qwen", "model": "qwen-test", "model_id": created.json()["id"], **request_fields,
        })
        assert incomplete.status_code == 200
        assert incomplete.json()["success"] is False
        assert "response.incomplete" in incomplete.json()["error"]


async def test_model_metadata_migration_preserves_existing_protocol_and_vision():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    spec = importlib.util.spec_from_file_location("model_pool_migration", Path(__file__).parents[1] / "alembic/versions/202609091430_model_pool_capabilities.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = f"pool_migration_{uuid.uuid4().hex}"
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        await conn.execute(text("CREATE TABLE llm_models (id integer, provider text, supports_vision boolean)"))
        await conn.execute(text("INSERT INTO llm_models VALUES (1, 'openai-response', true), (2, 'qwen', false)"))
        def upgrade(connection):
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        def downgrade(connection):
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
        await conn.run_sync(upgrade)
        rows = (await conn.execute(text("SELECT provider, api_protocol, purposes, input_modalities FROM llm_models ORDER BY id"))).all()
        assert rows == [("openai-response", None, ["conversation"], ["text", "image"]), ("qwen", None, ["conversation"], ["text"])]
        await conn.run_sync(downgrade)
        rows = (await conn.execute(text("SELECT * FROM llm_models ORDER BY id"))).all()
        assert rows == [(1, "openai-response", True), (2, "qwen", False)]
        await conn.run_sync(upgrade)
        assert (await conn.execute(text("SELECT input_modalities FROM llm_models WHERE id=1"))).scalar_one() == ["text", "image"]
        await conn.execute(text('SET LOCAL search_path TO public'))
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
