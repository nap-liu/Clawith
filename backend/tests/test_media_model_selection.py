"""Enterprise media selection, immutable tasks, and legacy migration over PostgreSQL."""

import json
import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data, get_current_admin
from app.database import async_session
from app.models.audit import ChatMessage
from app.models.llm import LLMModel
from app.models.subagent_run import SubagentRun
from app.models.tenant import Tenant
from app.models.user import User
from app.services.agent_tools_config_runtime import invalidate_tool_config_cache
from app.services.media_ai_model import media_context_model
from app.services.media_ai_tools import execute_media_tool
from app.services.media_model_migration import materialize_legacy_config, migrate_legacy_media_configs
from app.services.tool_config import get_tenant_tool_config, set_tenant_tool_config
import test_media_ai_runtime as media_fixtures

pytestmark = pytest.mark.asyncio
context = media_fixtures.context


async def make_model(context, *, tenant_id=None, **kwargs):
    async with async_session() as db:
        model = LLMModel(
            tenant_id=tenant_id or context.tenant_id, provider="custom", model="media-small",
            label="Media test", base_url="http://model-proxy.test/team/v1",
            api_key_encrypted=encrypt_data("model-secret", get_settings().SECRET_KEY),
            api_protocol="openai_responses", purposes=["image_generation", "media_understanding"],
            input_modalities=["text", "image", "audio", "video"], enabled=True,
            context_window=32000, context_usage_ratio=0.7, max_output_tokens=4096,
        )
        for key, value in kwargs.items():
            setattr(model, key, value)
        db.add(model)
        await db.flush()
        await set_tenant_tool_config(db, context.tenant_id, "read_media", {
            "image_model_id": str(model.id), "understanding_model_id": str(model.id),
        })
        await db.commit()
    invalidate_tool_config_cache(context.agent_id)
    return model


async def submit(context, **arguments):
    context.tool_call_id = f"call_{uuid.uuid4().hex}"
    context.arguments = {"prompt": "A blue circle", "output_type": "image", **arguments}
    return json.loads(await execute_media_tool(context))


async def request_for(receipt):
    async with async_session() as db:
        row = await db.get(ChatMessage, uuid.UUID(receipt["task_id"]))
        return row.message_meta["media_request"]


async def test_default_freezes_real_model_and_protocol_without_public_credentials(context):
    model = await make_model(context)
    receipt = await submit(context)
    assert receipt["status"] == "queued"
    request = await request_for(receipt)
    assert request["config"]["model_id"] == str(model.id)
    assert request["config"]["api_protocol"] == "openai_responses"
    assert request["config"]["base_url"] == "http://model-proxy.test/team/v1"
    assert "model-secret" not in json.dumps(request)
    assert "runtime_model" not in request["config"]
    frozen = json.loads(decrypt_data(request["connection_ref"], get_settings().SECRET_KEY))
    assert frozen["api_key"] == "model-secret"
    assert frozen["runtime_model"]["id"] == str(model.id)
    async with async_session() as db:
        changed = await db.get(LLMModel, model.id)
        changed.model = "changed-after-submit"
        changed.api_protocol = "openai_compatible"
        changed.enabled = False
        await db.commit()
    runtime = await media_context_model(context, request)
    assert runtime.id == model.id and runtime.model == "media-small"
    assert runtime.api_protocol == "openai_responses"
    # Repeated admission resolves the accepted task before reading mutable config.
    assert json.loads(await execute_media_tool(context)) == receipt


@pytest.mark.parametrize("override", [{"enabled": False}, {"purposes": ["conversation"]}])
async def test_disabled_or_wrong_purpose_model_rejected_before_queue(context, override):
    model = await make_model(context, **override)
    response = await submit(context, model_id=str(model.id))
    assert response["status"] == "failed" and response["code"] == "modelUnavailable"
    async with async_session() as db:
        assert await db.scalar(select(func.count()).select_from(SubagentRun).where(
            SubagentRun.parent_session_id == uuid.UUID(context.session_id),
        )) == 0


async def test_cross_tenant_model_is_rejected(context):
    async with async_session() as db:
        other = Tenant(name="Other", slug=uuid.uuid4().hex)
        db.add(other)
        await db.commit()
    model = await make_model(context, tenant_id=other.id)
    response = await submit(context, model_id=str(model.id))
    assert response["code"] == "modelUnavailable"


async def test_session_inherits_selection_but_refreshes_model_config_each_turn(context):
    original = await make_model(context)
    first = await submit(context)
    replacement = await make_model(context, model="another-model")
    async with async_session() as db:
        selected = await db.get(LLMModel, original.id)
        selected.base_url = "https://new-proxy.test/path/v1"
        await db.commit()
    second = await submit(context, session_id=first["session_id"])
    second_request = await request_for(second)
    assert second_request["config"]["model_id"] == str(original.id)
    assert second_request["config"]["base_url"] == "https://new-proxy.test/path/v1"
    third = await submit(context, session_id=first["session_id"], model_id=str(replacement.id))
    assert (await request_for(third))["config"]["model_id"] == str(replacement.id)


async def test_legacy_tenant_migration_is_idempotent_and_removes_inline_credentials(context):
    async with async_session() as db:
        assert await migrate_legacy_media_configs(db) >= 1
        await db.commit()
        config = await get_tenant_tool_config(db, context.tenant_id, "read_media")
        assert "api_key" not in config and "base_url" not in config
        assert len(config) == 4 and all(key.endswith("_model_id") for key in config)
        count = await db.scalar(select(func.count()).select_from(LLMModel).where(
            LLMModel.tenant_id == context.tenant_id,
        ))
        assert count == 4
        await migrate_legacy_media_configs(db)
        assert await db.scalar(select(func.count()).select_from(LLMModel).where(
            LLMModel.tenant_id == context.tenant_id,
        )) == count


async def test_published_legacy_override_materializes_enterprise_model_without_editing_revision(context):
    from app.models.tool import Tool
    from app.services.turn_tool_settings import scene_tool_settings_scope

    async with async_session() as db:
        await migrate_legacy_media_configs(db)
        await db.commit()
        tool = await db.scalar(select(Tool).where(Tool.name == "generate_media"))
    scene = {"scene_tools": [{"tool_id": str(tool.id), "enabled": True,
                              "config": {"image_model": "legacy-custom-image"}}]}
    before = json.dumps(scene)
    async with scene_tool_settings_scope(context.agent_id, scene):
        receipt = await submit(context)
    assert receipt["status"] == "queued"
    request = await request_for(receipt)
    async with async_session() as db:
        model = await db.get(LLMModel, uuid.UUID(request["config"]["model_id"]))
        assert model.model == "legacy-custom-image" and model.tenant_id == context.tenant_id
    assert json.dumps(scene) == before


async def test_disabled_legacy_model_is_not_recreated_to_bypass_disable(context):
    async with async_session() as db:
        converted = await materialize_legacy_config(db, context.tenant_id, {"api_key": "test-key"})
        selected = await db.get(LLMModel, uuid.UUID(converted["image_model_id"]))
        selected.enabled = False
        await db.commit()
    response = await submit(context)
    assert response["code"] == "modelUnavailable"


async def test_old_global_defaults_do_not_override_new_enterprise_selection_after_seed(context):
    from app.models.tool import Tool
    from app.services.media_ai_contract import MEDIA_AI_DEFAULTS
    from app.services.tool_seeder import seed_builtin_tools

    model = await make_model(context, provider="qwen", model="chosen-enterprise-image",
                             base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                             api_protocol="openai_compatible")
    async with async_session() as db:
        tools = (await db.scalars(select(Tool).where(Tool.name.in_(["read_media", "generate_media"])))).all()
        for tool in tools:
            tool.config = dict(MEDIA_AI_DEFAULTS)
        await db.commit()
    await seed_builtin_tools()
    invalidate_tool_config_cache(context.agent_id)
    receipt = await submit(context)
    assert receipt["status"] == "queued"
    assert (await request_for(receipt))["config"]["model_id"] == str(model.id)
    async with async_session() as db:
        tools = (await db.scalars(select(Tool).where(Tool.name.in_(["read_media", "generate_media"])))).all()
        assert all(tool.config == {} for tool in tools)


async def test_model_test_http_enqueues_existing_worker_without_parent_llm(context):
    from app.api.enterprise_routes_media_test import router

    model = await make_model(context)
    async with async_session() as db:
        user = await db.get(User, context.user_id)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_admin] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/enterprise/llm-models/{model.id}/media-test", json={
            "agent_id": str(context.agent_id), "purpose": "image_generation", "prompt": "A circle",
        })
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt["status"] == "queued" and receipt["agent_id"] == str(context.agent_id)
    async with async_session() as db:
        run = await db.get(SubagentRun, uuid.UUID(receipt["session_id"]))
        assert run.status == "queued" and run.mode == "sync" and run.model_id is None
    assert (await request_for(receipt))["config"]["model_id"] == str(model.id)


async def test_scene_binding_survives_company_and_selected_model_edits_then_delete(context):
    from app.models.tenant_setting import TenantSetting
    from app.models.tool import Tool
    from app.services.media_legacy_bindings import BINDINGS_KEY
    from app.services.turn_tool_settings import scene_tool_settings_scope

    async with async_session() as db:
        await migrate_legacy_media_configs(db)
        await db.commit()
        defaults = await get_tenant_tool_config(db, context.tenant_id, "read_media")
        tool = await db.scalar(select(Tool).where(Tool.name == "generate_media"))
    scene = {"scene_tools": [{"tool_id": str(tool.id), "enabled": True,
                              "config": {"image_model": "immutable-scene-image"}}]}
    original = json.dumps(scene)
    async with scene_tool_settings_scope(context.agent_id, scene):
        first = await submit(context)
    frozen = await request_for(first)
    selected_id = uuid.UUID(frozen["config"]["model_id"])
    async with async_session() as db:
        count = await db.scalar(select(func.count()).select_from(LLMModel).where(LLMModel.tenant_id == context.tenant_id))
        selected = await db.get(LLMModel, selected_id)
        selected.model, selected.base_url = "admin-new-model", "https://admin.test/v1"
        selected.api_key_encrypted = encrypt_data("admin-new-key", get_settings().SECRET_KEY)
        company = await db.get(LLMModel, uuid.UUID(defaults["image_model_id"]))
        company.base_url = "https://changed-company.test/compatible-mode/v1"
        company.api_key_encrypted = encrypt_data("changed-company-key", get_settings().SECRET_KEY)
        await db.commit()
    async with scene_tool_settings_scope(context.agent_id, scene):
        second = await submit(context)
    current = await request_for(second)
    assert current["config"]["model_id"] == str(selected_id)
    current_connection = json.loads(decrypt_data(current["connection_ref"], get_settings().SECRET_KEY))
    assert current_connection["model"] == "admin-new-model"
    assert current_connection["base_url"] == "https://admin.test/v1"
    assert current_connection["api_key"] == "admin-new-key"
    assert (await request_for(first)) == frozen
    async with async_session() as db:
        await db.delete(await db.get(LLMModel, uuid.UUID(defaults["image_model_id"])))
        await db.commit()
    async with scene_tool_settings_scope(context.agent_id, scene):
        after_company_delete = await submit(context)
    assert (await request_for(after_company_delete))["config"]["model_id"] == str(selected_id)
    async with async_session() as db:
        selected = await db.get(LLMModel, selected_id)
        selected.purposes = ["conversation"]
        await db.commit()
    async with scene_tool_settings_scope(context.agent_id, scene):
        assert (await submit(context))["code"] == "modelUnavailable"
    async with async_session() as db:
        selected = await db.get(LLMModel, selected_id)
        assert selected.purposes == ["conversation"]
        await db.delete(selected)
        await db.commit()
    async with scene_tool_settings_scope(context.agent_id, scene):
        assert (await submit(context))["code"] == "modelUnavailable"
    async with async_session() as db:
        assert await db.scalar(select(func.count()).select_from(LLMModel).where(LLMModel.tenant_id == context.tenant_id)) == count - 2
        marker = await db.get(TenantSetting, (context.tenant_id, BINDINGS_KEY))
        assert all(secret not in json.dumps(marker.value) for secret in ["test-key", "admin-new-key", "changed-company-key"])
    assert json.dumps(scene) == original


async def test_concurrent_legacy_binding_materializes_only_once_and_keeps_deleted_reference(context):
    import asyncio

    async def migrate():
        async with async_session() as db:
            result = await materialize_legacy_config(db, context.tenant_id, {"api_key": "concurrent-test-key"})
            await db.commit()
            return result

    left, right = await asyncio.gather(migrate(), migrate())
    assert left == right
    async with async_session() as db:
        assert await db.scalar(select(func.count()).select_from(LLMModel).where(LLMModel.tenant_id == context.tenant_id)) == 4
        await db.delete(await db.get(LLMModel, uuid.UUID(left["image_model_id"])))
        await db.commit()
    assert await migrate() == left
    async with async_session() as db:
        assert await db.scalar(select(func.count()).select_from(LLMModel).where(LLMModel.tenant_id == context.tenant_id)) == 3


async def test_scene_only_image_model_migrates_once_then_preserves_purpose_revocation(context):
    from app.services.turn_tool_settings import scene_tool_settings_scope
    from app.services.tool_seeder import seed_builtin_tools
    import test_read_image_handler as image_tests

    old_id, _ = await image_tests.legacy(context)
    model = await make_model(context, purposes=["conversation"], supports_vision=True)
    await seed_builtin_tools()
    scene = {"scene_tools": [{"tool_id": str(old_id), "enabled": True,
                              "config": {"model_id": str(model.id)}}]}
    context.tool_name = "read_media"
    context.arguments = {"prompt": "Read image", "files": ["https://media.test/image.png"]}
    async with scene_tool_settings_scope(context.agent_id, scene):
        first = json.loads(await execute_media_tool(context))
    assert first["status"] == "queued", first
    async with async_session() as db:
        selected = await db.get(LLMModel, model.id)
        assert "media_understanding" in selected.purposes
        selected.purposes = ["conversation"]
        await db.commit()
    context.tool_call_id = uuid.uuid4().hex
    async with scene_tool_settings_scope(context.agent_id, scene):
        second = json.loads(await execute_media_tool(context))
    assert second["code"] == "modelUnavailable"
    async with async_session() as db:
        assert (await db.get(LLMModel, model.id)).purposes == ["conversation"]


async def test_upgrade_without_receipts_does_not_regrant_old_image_purpose(context):
    from app.models.tenant_setting import TenantSetting
    from app.models.tool import Tool
    from app.services.media_legacy_bindings import BINDINGS_KEY
    from app.services.turn_tool_settings import scene_tool_settings_scope
    import test_read_image_handler as image_tests

    old_id, model_id = await image_tests.legacy(context)
    async with async_session() as db:
        old = await db.get(Tool, old_id)
        old.source, old.enabled = "legacy", False
        old.config = {"_read_media_was_enabled": True}
        marker = await db.get(TenantSetting, (context.tenant_id, BINDINGS_KEY))
        if marker:
            await db.delete(marker)
        await db.commit()
    scene = {"scene_tools": [{"tool_id": str(old_id), "enabled": True,
                              "config": {"model_id": str(model_id)}}]}
    context.tool_name = "read_media"
    context.arguments = {"prompt": "Read image", "files": ["https://media.test/image.png"]}
    async with scene_tool_settings_scope(context.agent_id, scene):
        result = json.loads(await execute_media_tool(context))
    assert result["code"] == "modelUnavailable"
    async with async_session() as db:
        assert (await db.get(LLMModel, model_id)).purposes == ["conversation"]


@pytest.mark.parametrize("change", ["edit", "delete"])
async def test_unrecorded_deployed_scene_never_recreates_admin_changed_model(context, change):
    from app.models.tenant_setting import TenantSetting
    from app.models.tool import Tool
    from app.services.media_legacy_bindings import BINDINGS_KEY
    from app.services.turn_tool_settings import scene_tool_settings_scope

    async with async_session() as db:
        await migrate_legacy_media_configs(db)
        tool = await db.scalar(select(Tool).where(Tool.name == "generate_media"))
        await db.commit()
    scene = {"scene_tools": [{"tool_id": str(tool.id), "enabled": True,
                              "config": {"image_model": "old-scene-image"}}]}
    async with scene_tool_settings_scope(context.agent_id, scene):
        first = await submit(context)
    request = await request_for(first)
    async with async_session() as db:
        # Simulate the already-deployed migration, which left IDs but no receipts.
        await db.delete(await db.get(TenantSetting, (context.tenant_id, BINDINGS_KEY)))
        selected = await db.get(LLMModel, uuid.UUID(request["config"]["model_id"]))
        if change == "delete":
            await db.delete(selected)
        else:
            selected.base_url = "https://admin-changed.test/v1"
        await db.commit()
        before = await db.scalar(select(func.count()).select_from(LLMModel).where(LLMModel.tenant_id == context.tenant_id))
    async with scene_tool_settings_scope(context.agent_id, scene):
        assert (await submit(context))["code"] == "modelUnavailable"
    async with async_session() as db:
        assert await db.scalar(select(func.count()).select_from(LLMModel).where(LLMModel.tenant_id == context.tenant_id)) == before


async def test_unrecorded_scene_missing_audio_does_not_block_image_or_fallback(context):
    from app.models.tenant_setting import TenantSetting
    from app.models.tool import Tool
    from app.services.media_legacy_bindings import BINDINGS_KEY
    from app.services.turn_tool_settings import scene_tool_settings_scope

    async with async_session() as db:
        await migrate_legacy_media_configs(db)
        await db.commit()
        defaults = await get_tenant_tool_config(db, context.tenant_id, "read_media")
        tool = await db.scalar(select(Tool).where(Tool.name == "generate_media"))
    scene = {"scene_tools": [{"tool_id": str(tool.id), "enabled": True,
                              "config": {"image_model": "old-valid-image"}}]}
    async with scene_tool_settings_scope(context.agent_id, scene):
        first = await submit(context)
    original = await request_for(first)
    async with async_session() as db:
        await db.delete(await db.get(TenantSetting, (context.tenant_id, BINDINGS_KEY)))
        await db.delete(await db.get(LLMModel, uuid.UUID(defaults["audio_model_id"])))
        # A different, valid company audio default must not replace the missing
        # legacy scene selection when that purpose is requested later.
        replacement = LLMModel(tenant_id=context.tenant_id, provider="custom", model="new-audio",
                               label="Audio", base_url="https://audio.test/v1", enabled=True,
                               api_key_encrypted=encrypt_data("audio-test-key", get_settings().SECRET_KEY),
                               purposes=["audio_generation"], input_modalities=["text"])
        db.add(replacement)
        await db.flush()
        await set_tenant_tool_config(db, context.tenant_id, "read_media", {
            **defaults, "audio_model_id": str(replacement.id),
        })
        await db.commit()
        count = await db.scalar(select(func.count()).select_from(LLMModel).where(LLMModel.tenant_id == context.tenant_id))
    async with scene_tool_settings_scope(context.agent_id, scene):
        image = await submit(context)
    assert image["status"] == "queued"
    assert (await request_for(image))["config"]["model_id"] == original["config"]["model_id"]
    async with scene_tool_settings_scope(context.agent_id, scene):
        audio = await submit(context, output_type="audio")
    assert audio["code"] == "modelUnavailable"
    async with async_session() as db:
        assert await db.scalar(select(func.count()).select_from(LLMModel).where(LLMModel.tenant_id == context.tenant_id)) == count
