"""Observable catalog, CLI injection and HTTP bridge scene behavior."""

import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import app.api.toolscall as bridge
from app.api.scenes import get_scene_tool_options
from app.database import async_session, engine
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.schemas.scene import SceneSaveRequest, ScenePublishRequest
from app.services.agent_tools_config_runtime import _get_tool_config
from app.services.agent_tools_sandbox_web_ops import build_cli_injection
from app.services.scene_service import save_scene, publish_scene
from app.services.toolscall.capability import prepare_toolscall_launcher
from app.services.turn_tool_settings import current_tool_settings, scene_tool_settings_scope
from test_scene_runtime_settings import setup_scene, persist

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def dispose_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def test_catalog_roundtrip_excludes_foreign_builtin_even_if_assigned():
    aid, uid, tid, _, _ = await setup_scene()
    _, _, foreign_tid, _, _ = await setup_scene()
    async with async_session() as db:
        foreign = Tool(name=f"foreign_{uuid.uuid4().hex}", display_name="Foreign tool", description="Foreign tool",
                       type="builtin", source="builtin", tenant_id=foreign_tid, enabled=True)
        db.add(foreign)
        await db.flush()
        db.add(AgentTool(agent_id=aid, tool_id=foreign.id, enabled=True))
        await db.commit()
        viewer = await db.get(User, uid)
        options = await get_scene_tool_options(aid, viewer, db)
        assert str(foreign.id) not in {tool["id"] for tool in options["tools"]}
        saved = await save_scene(db, agent_id=aid, tenant_id=tid, scene_key="catalog",
                                 data=SceneSaveRequest(name="Catalog", tools=[
                                     {"tool_id": tool["id"], "enabled": tool["enabled"],
                                      "config": tool.get("agent_config") or {}}
                                     for tool in options["tools"]
                                 ]), created_by_user_id=uid)
        assert len(saved["tools"]) == len(options["tools"])


@pytest.mark.parametrize("agent_enabled,scene_enabled", [(True, False), (False, True)])
async def test_cli_injection_uses_scene_assignment_and_configuration(agent_enabled, scene_enabled, monkeypatch, tmp_path):
    from app.services.cli_tools import storage

    monkeypatch.setattr(storage, "BINARY_ROOT", tmp_path)
    aid, uid, tid, tool_id, name = await setup_scene()
    async with async_session() as db:
        tool = await db.get(Tool, tool_id)
        tool.type = "cli"
        tool.config = {"binary": {"sha256": "a" * 64}, "env": {"MODE": "agent"}}
        assignment = await db.scalar(select(AgentTool).where(AgentTool.agent_id == aid, AgentTool.tool_id == tool_id))
        assignment.enabled = agent_enabled
        await db.commit()
    binary = tmp_path / str(tid) / str(tool_id) / ("a" * 64 + ".bin")
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    context = await persist(aid, uid, tid, [{"tool_id": str(tool_id), "enabled": scene_enabled,
                                          "config": {"env": {"MODE": "scene"}}}])
    async with scene_tool_settings_scope(aid, context):
        injection = await build_cli_injection(aid, uid)
    if scene_enabled:
        assert injection["wrappers"] == [{"name": name, "binary_path": str(binary), "env": {"MODE": "scene"}}]
    else:
        assert injection is None
    assert current_tool_settings(aid) is None


async def test_http_bridge_restores_anchor_revision_and_resets_scope(monkeypatch):
    aid, uid, tid, tool_id, name = await setup_scene()
    await persist(aid, uid, tid, [{"tool_id": str(tool_id), "enabled": True, "config": {"limit": 42}}])
    async with async_session() as db:
        session = ChatSession(agent_id=aid, user_id=uid, source_channel="web")
        db.add(session)
        await db.flush()
        anchor = ChatMessage(agent_id=aid, user_id=uid, role="user", content="Read",
                             conversation_id=str(session.id),
                             message_meta={"scene_key": "warranty", "scene_revision": 2})
        db.add(anchor)
        await db.commit()
        session_id, anchor_id = str(session.id), anchor.id
        await save_scene(db, agent_id=aid, tenant_id=tid, scene_key="warranty",
                         data=SceneSaveRequest(name="Support", expected_revision=2, tools=[
                             {"tool_id": str(tool_id), "enabled": True, "config": {"limit": 99}},
                         ]), created_by_user_id=uid)
        await publish_scene(db, agent_id=aid, scene_key="warranty",
                            data=ScenePublishRequest(expected_revision=2), created_by_user_id=uid)
    seed = b"b" * 32
    monkeypatch.setattr(bridge, "get_toolscall_signing_seed", AsyncMock(return_value=seed))
    monkeypatch.setattr(bridge, "toolscall_scope_allows", AsyncMock(return_value=True))

    async def execute(tool_name, arguments, **kwargs):
        config = await _get_tool_config(kwargs["agent_id"], tool_name)
        return str(config["limit"])

    monkeypatch.setattr(bridge, "execute_tool", execute)
    token = prepare_toolscall_launcher({
        "kind": "toolscall", "name": "toolscall", "endpoint": "http://bridge.test/api/internal/toolscall/v1",
        "signing_seed": seed, "scope": "bridge-scene", "agent": str(aid), "user": str(uid),
        "session": session_id, "turn": str(anchor_id), "standard": {name: {}}, "native": [],
    }, ttl_seconds=60)["context_token"]
    app = FastAPI()
    app.include_router(bridge.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/internal/toolscall/v1/call/{name}", json={},
                                     headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.text == "42"
    assert current_tool_settings(aid) is None
    assert (await _get_tool_config(aid, name))["limit"] == 10
