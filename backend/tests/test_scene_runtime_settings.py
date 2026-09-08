"""Scene settings through persisted revisions and the shared execution boundary."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tool import AgentTool, Tool
from app.schemas.scene import SceneConfig, SceneSaveRequest, ScenePublishRequest
from app.services.scene_service import (
    save_scene, publish_scene, rollback_scene, get_scene, serialize_scene,
    serialize_published_scene, project_scene_manifest, build_scene_channel_context,
)
from app.services.turn_tool_settings import with_scene_tool_settings, current_tool_settings
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.agent_tools_config_runtime import _get_tool_config
from app.services.agent_context_extensions import _collect_extension_prompts
from test_im_scene_command_integration import _seed_scene_runtime

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def dispose_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def setup_scene():
    agent_id, user_id = await _seed_scene_runtime()
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        tool = Tool(name=f"scene_search_{uuid.uuid4().hex[:8]}", display_name="Search",
                    description="Search", type="builtin", source="admin", tenant_id=agent.tenant_id,
                    enabled=True, config={"limit": 5}, config_schema={"fields": [{"key": "token", "type": "password"}]})
        db.add(tool)
        await db.flush()
        db.add(AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=False, config={"limit": 10}))
        await db.commit()
        return agent_id, user_id, agent.tenant_id, tool.id, tool.name


async def persist(agent_id, user_id, tenant_id, tools, **extra):
    async with async_session() as db:
        await save_scene(db, agent_id=agent_id, tenant_id=tenant_id, scene_key="warranty",
                         data=SceneSaveRequest(name="Support", expected_revision=1, tools=tools, **extra),
                         created_by_user_id=user_id)
        await publish_scene(db, agent_id=agent_id, scene_key="warranty",
                            data=ScenePublishRequest(expected_revision=1), created_by_user_id=user_id)
        scene, revision = await get_scene(db, agent_id, "warranty")
        manifest = serialize_published_scene(scene, revision)
        await db.commit()
        return build_scene_channel_context(manifest, source_channel="web", display_name="Web", client_surface="web")


async def test_defaults_and_revision_roundtrip_keep_secrets_private():
    assert SceneConfig().include_memory is True
    assert SceneConfig().include_soul is True
    assert SceneConfig().tools is None
    aid, uid, tid, tool_id, name = await setup_scene()
    await persist(aid, uid, tid, [{"tool_id": str(tool_id), "enabled": True, "config": {"limit": 42, "token": "private-value"}}],
                  include_memory=False, include_soul=False)
    async with async_session() as db:
        scene, revision = await get_scene(db, aid, "warranty")
        assert "private-value" not in str(revision.config)
        output = serialize_scene(scene, revision)
        assert output["include_memory"] is False and output["include_soul"] is False
        assert output["tools"][0]["config"]["token"] == "********"
        echoed = await save_scene(db, agent_id=aid, tenant_id=tid, scene_key="warranty",
                                 data=SceneSaveRequest(name="Support", expected_revision=2, tools=output["tools"]),
                                 created_by_user_id=uid)
        assert echoed["tools"] == output["tools"]
        assert "private-value" not in str(echoed)
        assert not {"tools", "include_memory", "include_soul", "mcp_server_overrides"} & project_scene_manifest(output).keys()
        # Old clients omitting new settings preserve them.
        saved = await save_scene(db, agent_id=aid, tenant_id=tid, scene_key="warranty",
                                data=SceneSaveRequest(name="Renamed", expected_revision=2), created_by_user_id=uid)
        assert saved["tools"] == output["tools"] and saved["include_soul"] is False
        await publish_scene(db, agent_id=aid, scene_key="warranty", data=ScenePublishRequest(expected_revision=2), created_by_user_id=uid)
        restored = await rollback_scene(db, agent_id=aid, scene_key="warranty", target_revision=1,
                                        expected_revision=3, created_by_user_id=uid)
        assert restored["tools"] is None and restored["include_memory"] is True and restored["include_soul"] is True
        assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == aid, AgentTool.tool_id == tool_id))
        assert assignment.enabled is False and assignment.config == {"limit": 10}


async def test_custom_tools_parameters_concurrency_and_reset():
    aid, uid, tid, tool_id, name = await setup_scene()
    context = await persist(aid, uid, tid, [{"tool_id": str(tool_id), "enabled": True, "config": {"limit": 42}}])

    @with_scene_tool_settings
    async def run(*, agent_id, channel_context):
        await asyncio.sleep(0)
        tools = await get_agent_tools_for_llm(agent_id)
        config = await _get_tool_config(agent_id, name)
        return {tool["function"]["name"] for tool in tools}, config

    scoped, inherited, empty = await asyncio.gather(
        run(agent_id=aid, channel_context=context),
        run(agent_id=aid, channel_context={}),
        run(agent_id=aid, channel_context={**context, "scene_tools": []}),
    )
    assert name in scoped[0] and scoped[1]["limit"] == 42
    assert name not in inherited[0] and inherited[1]["limit"] == 10
    assert name not in empty[0]
    assert empty[0] <= {"send_media"}
    assert current_tool_settings(aid) is None


async def test_cross_tenant_tool_and_server_rejected():
    aid, uid, tid, tool_id, name = await setup_scene()
    _, _, _, other_tool_id, _ = await setup_scene()
    async with async_session() as db:
        for tools, overrides in [
            ([{"tool_id": str(other_tool_id)}], []),
            ([{"tool_id": str(tool_id)}], [{"server_id": str(uuid.uuid4())}]),
        ]:
            with pytest.raises(HTTPException) as error:
                await save_scene(db, agent_id=aid, tenant_id=tid, scene_key="warranty", created_by_user_id=uid,
                                 data=SceneSaveRequest(name="Invalid", tools=tools, mcp_server_overrides=overrides))
            assert error.value.status_code == 422
            await db.rollback()


async def test_builtin_configuration_keeps_defaults_and_company_layers():
    from app.services.tool_config import set_tenant_tool_config

    aid, uid, tid, tool_id, name = await setup_scene()
    async with async_session() as db:
        tool = await db.get(Tool, tool_id)
        tool.source = "builtin"
        tool.config = {"base_only": "retained", "limit": 5}
        await set_tenant_tool_config(db, tid, name, {"company_only": "inherited", "limit": 20})
        await db.commit()
    context = await persist(aid, uid, tid, [{"tool_id": str(tool_id), "enabled": True, "config": {"limit": 42}}])

    @with_scene_tool_settings
    async def run(*, agent_id, channel_context):
        return await _get_tool_config(agent_id, name)

    assert await run(agent_id=aid, channel_context=context) == {
        "base_only": "retained", "company_only": "inherited", "limit": 42,
    }


@pytest.mark.parametrize("source", ["web", "feishu"])
@pytest.mark.parametrize("soul,memory", [(False, False), (True, False), (False, True), (True, True)])
async def test_shared_failover_uses_scene_context_flags(monkeypatch, source, soul, memory):
    from app.services.llm import caller_failover

    aid, uid, tid, tool_id, name = await setup_scene()
    context = await persist(aid, uid, tid, [], include_soul=soul, include_memory=memory)
    context["source_channel"] = source
    build = AsyncMock(return_value=("static", "dynamic"))
    monkeypatch.setattr(caller_failover, "_build_turn_context", build)
    model_call = AsyncMock(return_value="Completed")
    monkeypatch.setattr(caller_failover, "call_llm", model_call)
    # The serialization decorator is orthogonal to scene settings.
    await caller_failover.call_llm_with_failover.__wrapped__(
        SimpleNamespace(id=uuid.uuid4()), None, [], "Support", "Assistant",
        agent_id=aid, user_id=uid, channel_context=context,
    )
    assert build.call_args.kwargs["include_soul"] is soul
    assert build.call_args.kwargs["include_memory"] is memory
    assert {tool["function"]["name"] for tool in model_call.call_args.kwargs["prepared_tools"]} <= {"send_media"}
    assert current_tool_settings(aid) is None


async def test_mcp_config_prompt_and_execution_share_settings(monkeypatch):
    from app.services.agent_tools_mcp_runtime import _execute_mcp_tool
    from app.services.mcp_client import MCPClient

    aid, uid, tid, tool_id, name = await setup_scene()
    async with async_session() as db:
        server = MCPServer(name=f"scene_service_{uuid.uuid4().hex[:8]}", display_name="Scene service", tenant_id=tid,
                           base_url_template="https://example.com/mcp", system_prompt_block="base instructions")
        db.add(server)
        await db.flush()
        tool = await db.get(Tool, tool_id)
        tool.type = "mcp"
        tool.mcp_server_id = server.id
        tool.mcp_tool_name = "search"
        server_id = server.id
        await db.commit()
    context = await persist(aid, uid, tid, [{"tool_id": str(tool_id), "enabled": True, "config": {}}],
                            mcp_server_overrides=[{"server_id": str(server_id), "url_template": "https://example.com/scene",
                                                   "system_prompt_block": "scene instructions"}])
    calls = []

    def client_init(self, url, **kwargs):
        calls.append(url)

    monkeypatch.setattr(MCPClient, "__init__", client_init)
    monkeypatch.setattr(MCPClient, "call_tool", AsyncMock(return_value="scene result"))

    @with_scene_tool_settings
    async def run(*, agent_id, channel_context):
        prompts = await _collect_extension_prompts(agent_id)
        assert "scene instructions" in "\n".join(prompts)
        return await _execute_mcp_tool(name, {}, agent_id=agent_id, user_id=uid)

    assert await run(agent_id=aid, channel_context=context) == "scene result"
    assert calls == ["https://example.com/scene"]
    assert "scene instructions" not in "\n".join(await _collect_extension_prompts(aid))


async def test_disabled_dispatch_and_scope_reset_on_cancellation():
    from app.services.agent_tools_execute_tool_preflight import execute_tool_preflight

    aid, uid, tid, tool_id, name = await setup_scene()
    context = await persist(aid, uid, tid, [])

    @with_scene_tool_settings
    async def run(*, agent_id, channel_context):
        from app.services.llm.failure_outcome import render_message
        assert await execute_tool_preflight(name, {}, agent_id, uid) == render_message("sceneRuntime.toolDisabled")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run(agent_id=aid, channel_context=context)
    assert current_tool_settings(aid) is None
