"""Legacy image installations migrate onto the observable media tool contract."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.llm import LLMModel
from app.models.tool import AgentTool, Tool
from app.services.agent_tools import execute_tool, get_agent_tools_for_llm
from app.services.read_media_compat import migrate_read_image
from app.services.tool_config import get_tenant_tool_config, set_tenant_tool_config
from app.services.tool_seeder import seed_builtin_tools
from app.services.turn_tool_settings import scene_tool_settings_scope
import test_media_ai_runtime as runtime_tests

context = runtime_tests.context

pytestmark = pytest.mark.asyncio


async def legacy(state):
    async with async_session() as db:
        old = await db.scalar(select(Tool).where(Tool.name == "read_image"))
        if old is None:
            old = Tool(name="read_image", display_name="Legacy image", type="builtin", source="builtin",
                       enabled=True, is_default=True, config={})
            db.add(old)
        else:
            old.source, old.enabled = "builtin", True
        model = LLMModel(tenant_id=state.tenant_id, provider="openai", model="vision-test", label="Vision",
                         api_key_encrypted="test", base_url="http://provider.test/v1", purposes=["conversation"],
                         supports_vision=True, input_modalities=["text", "image"], enabled=True)
        db.add(model)
        await db.flush()
        db.add(AgentTool(agent_id=state.agent_id, tool_id=old.id, enabled=True, config={"model_id": str(model.id)}))
        await set_tenant_tool_config(db, state.tenant_id, "read_image", {"model_id": str(model.id)})
        await set_tenant_tool_config(db, state.tenant_id, "read_media", {})
        await db.commit()
        return old.id, model.id


async def test_seed_merges_assignments_and_projects_old_scene_without_rewriting(context):
    old_id, model_id = await legacy(context)
    await seed_builtin_tools()
    await seed_builtin_tools()
    async with async_session() as db:
        old = await db.get(Tool, old_id)
        assert old.source == "legacy" and not old.enabled and not old.is_default
        new = await db.scalar(select(Tool).where(Tool.name == "read_media"))
        assigned = list(await db.scalars(select(AgentTool).where(
            AgentTool.agent_id == context.agent_id, AgentTool.tool_id.in_([old_id, new.id]))))
        assert len(assigned) == 1 and assigned[0].tool_id == new.id and assigned[0].enabled
        assert assigned[0].config["understanding_model_id"] == str(model_id)
        assert "media_understanding" in (await db.get(LLMModel, model_id)).purposes
        assert (await get_tenant_tool_config(db, context.tenant_id, "read_media"))["understanding_model_id"] == str(model_id)
    tools = await get_agent_tools_for_llm(context.agent_id)
    names = [item["function"]["name"] for item in tools]
    assert names.count("read_media") == 1 and "read_image" not in names
    schema = next(item["function"]["parameters"] for item in tools if item["function"]["name"] == "read_media")
    assert "maxItems" not in schema["properties"]["files"]
    scene = {"scene_tools": [{"tool_id": str(old_id), "enabled": True, "config": {"model_id": str(model_id)}}]}
    original = json.dumps(scene)
    async with scene_tool_settings_scope(context.agent_id, scene) as scope:
        assert "read_media" in scope.enabled_names and "read_image" not in scope.enabled_names
        assert scope.configs["read_media"]["understanding_model_id"] == str(model_id)
        assert "read_media" in {item["function"]["name"] for item in await get_agent_tools_for_llm(context.agent_id)}
    assert json.dumps(scene) == original


async def test_explicit_new_disable_wins_and_old_call_uses_same_permission(context):
    old_id, _ = await legacy(context)
    async with async_session() as db:
        new = await db.scalar(select(Tool).where(Tool.name == "read_media"))
        db.add(AgentTool(agent_id=context.agent_id, tool_id=new.id, enabled=False, config={}))
        await migrate_read_image(db)
        await db.commit()
    result = json.loads(await execute_tool("read_image", {"image_paths": ["image.png"]},
        context.agent_id, context.user_id, session_id=context.session_id,
        tool_call_id=context.tool_call_id, turn_anchor_id=context.turn_anchor_id, skip_autonomy=True))
    assert result["code"] == "toolDisabled"
    async with async_session() as db:
        assert await db.scalar(select(AgentTool).where(AgentTool.agent_id == context.agent_id, AgentTool.tool_id == old_id)) is None


async def test_legacy_assignment_without_tenant_does_not_abort_startup_migration(
    context,
    monkeypatch,
):
    from app.models.agent import Agent
    from types import SimpleNamespace

    old_id, model_id = await legacy(context)
    async with async_session() as db:
        assignment = await db.scalar(select(AgentTool).where(
            AgentTool.agent_id == context.agent_id,
            AgentTool.tool_id == old_id,
        ))
        assignment.config = {"fallback_model_id": str(model_id)}
        real_get = db.get

        async def get_with_legacy_tenantless_agent(model, identity, *args, **kwargs):
            if model is Agent and identity == context.agent_id:
                return SimpleNamespace(tenant_id=None)
            return await real_get(model, identity, *args, **kwargs)

        monkeypatch.setattr(db, "get", get_with_legacy_tenantless_agent)
        await migrate_read_image(db)
        await db.commit()

    async with async_session() as db:
        new = await db.scalar(select(Tool).where(Tool.name == "read_media"))
        assignment = await db.scalar(select(AgentTool).where(
            AgentTool.agent_id == context.agent_id,
            AgentTool.tool_id == new.id,
        ))
        assert assignment is not None and assignment.enabled is True
        assert assignment.config == {
            "understanding_model_id": str(model_id),
            "fallback_model_id": str(model_id),
        }
        assert await db.scalar(select(AgentTool).where(
            AgentTool.agent_id == context.agent_id,
            AgentTool.tool_id == old_id,
        )) is None


async def test_legacy_batch_call_is_one_durable_async_media_input(context):
    await legacy(context)
    await seed_builtin_tools()
    paths = [f"https://media.example/image-{index}.png" for index in range(8)]
    result = json.loads(await execute_tool("read_image", {"image_paths": paths},
        context.agent_id, context.user_id, session_id=context.session_id,
        tool_call_id=context.tool_call_id, turn_anchor_id=context.turn_anchor_id, skip_autonomy=True))
    assert result["status"] == "queued"
    from app.models.audit import ChatMessage
    async with async_session() as db:
        row = await db.get(ChatMessage, uuid.UUID(result["task_id"]))
        request = row.message_meta["media_request"]
        assert request["tool"] == "read_media"
        assert [item["source"] for item in request["arguments"]["files"]] == paths
        assert "Transcribe" in request["arguments"]["prompt"]
        assert "api_key" not in request["config"] and "fallback_connection" not in request["config"]


async def test_project_shared_media_uses_existing_exact_file_ticket(context):
    from pathlib import Path
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlsplit

    from app.config import get_settings
    from app.services.agent_file_urls import verify_agent_file_ticket
    from app.services.agent_runtime_workspace import AgentRuntimeWorkspace, bind_agent_runtime_workspace
    from app.services.read_media_compat import read_project_media
    from app.services.storage import get_storage_backend
    from app.models.audit import ChatMessage
    from test_media_ai_provider import PNG

    await legacy(context)
    await seed_builtin_tools()
    project_id = uuid.uuid4()
    prefix = f"projects/{context.tenant_id}/{project_id}/repo"
    repo = Path(get_settings().STORAGE_LOCAL_ROOT) / prefix
    workspace = AgentRuntimeWorkspace(context.agent_id, repo / ".agents" / str(context.agent_id),
        prefix + f"/.agents/{context.agent_id}", project_id=project_id, project_repo_root=repo)
    storage = get_storage_backend()
    await storage.write_bytes(prefix + "/assets/image.png", PNG)
    with bind_agent_runtime_workspace(workspace):
        receipt = json.loads(await read_project_media(SimpleNamespace(id=project_id),
            {"files": ["assets/image.png"], "prompt": "Read shared image", "workspace": "project"},
            agent_id=context.agent_id, execution_user_id=context.user_id, session_id=context.session_id,
            tool_call_id=context.tool_call_id, turn_anchor_id=context.turn_anchor_id))
    assert receipt["status"] == "queued"
    async with async_session() as db:
        anchor = await db.get(ChatMessage, uuid.UUID(receipt["task_id"]))
    request = anchor.message_meta["media_request"]
    assert request["arguments"]["files"][0]["source"] == "assets/image.png"
    assert request["input_workspace"] == "project"
    from app.services.read_media_compat import media_input_workspace
    from app.services.media_ai_io import load_media
    with bind_agent_runtime_workspace(workspace), media_input_workspace(context.agent_id, request):
        media = await load_media(context.agent_id, request["arguments"]["files"])
    params = parse_qs(urlsplit(media[0].url).query)
    payload = verify_agent_file_ticket(context.agent_id, "assets/image.png", params["im_ticket"][0])
    assert payload["storage_key"] == prefix + "/assets/image.png"
    assert await storage.read_bytes(payload["storage_key"]) == PNG
    assert anchor.message_meta["media_request"]["workspace"] == workspace.as_session_config()


async def test_old_disabled_assignment_survives_default_backfill(context):
    old_id, _ = await legacy(context)
    async with async_session() as db:
        old_assignment = await db.scalar(select(AgentTool).where(
            AgentTool.agent_id == context.agent_id, AgentTool.tool_id == old_id))
        old_assignment.enabled = False
        new = await db.scalar(select(Tool).where(Tool.name == "read_media"))
        new.is_default = False  # Exercise the actual rollout, including backfill.
        await db.commit()
    await seed_builtin_tools()
    async with async_session() as db:
        new = await db.scalar(select(Tool).where(Tool.name == "read_media"))
        assignment = await db.scalar(select(AgentTool).where(
            AgentTool.agent_id == context.agent_id, AgentTool.tool_id == new.id))
        assert new.is_default is True and assignment.enabled is False
        generation = await db.scalar(select(Tool).where(Tool.name == "generate_media"))
        assert generation.is_default is False
    assert "read_media" not in {item["function"]["name"] for item in await get_agent_tools_for_llm(context.agent_id)}


async def test_shared_loop_accepts_historical_name_under_canonical_permission(context):
    from app.services.active_turns import active_turn_boundary
    from app.services.llm import call_llm
    from execution_provider_fixture import provider

    _, model_id = await legacy(context)
    await seed_builtin_tools()
    call = {"id": "legacy-read", "type": "function", "function": {
        "name": "read_image", "arguments": json.dumps({"image_paths": ["first.png", "second.png"]})}}
    async with provider([{"tool_calls": [call]}, {"content": "Task accepted"}]) as (url, requests):
        async with async_session() as db:
            model = await db.get(LLMModel, model_id)
            model.base_url = url
            model.api_protocol = "openai_compatible"
            await db.commit()
        async with active_turn_boundary():
            reply = await call_llm(model, [{"role": "user", "content": "Read the old image request"}],
                "Media tester", "Assistant", agent_id=context.agent_id, user_id=context.user_id,
                session_id=context.session_id, turn_anchor_id=context.turn_anchor_id,
                prepared_turn_context=("Assistant", ""))
        assert reply == "Task accepted" and len(requests) == 2
        names = {item["function"]["name"] for item in requests[0]["tools"]}
        assert "read_media" in names and "read_image" not in names
        result = json.loads(next(item["content"] for item in requests[1]["messages"] if item["role"] == "tool"))
        assert result["status"] == "queued"
