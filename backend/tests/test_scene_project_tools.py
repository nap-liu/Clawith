"""Scene assignments retain the project's independent protocol authorization."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.mcp_server import MCPServer
from app.models.project import Project, ProjectMemberSnapshot
from app.models.tool import AgentTool, Tool
from app.schemas.scene import ScenePublishRequest, SceneSaveRequest
from app.services import subagent_runtime as runtime
from app.services.agent_tools_execute_tool_preflight import execute_tool_preflight
from app.services.llm import caller_failover
from app.services.llm.failure_outcome import render_message
from app.services.scene_service import load_turn_scene_context, publish_scene, save_scene
from app.services.turn_tool_settings import current_tool_settings, restore_turn_tool_settings
from tests.test_subagent_runtime import _make_context

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def isolated_engine():
    await engine.dispose()
    yield
    await engine.dispose()


async def project_scene():
    aid, uid, parent_id, parent_anchor = await _make_context(parent_channel="project", project=True)
    child, _ = await runtime.create_subagent(
        agent_id=aid, execution_user_id=uid, parent_session_id=str(parent_id),
        origin_tool_call_id=str(uuid.uuid4()), task="Review project evidence", mode="async",
        turn_anchor_id=parent_anchor, input_metadata={"project_dispatch": True},
    )
    async with async_session() as db:
        agent = await db.get(Agent, aid)
        server = MCPServer(name=f"scene_project_{uuid.uuid4().hex}", display_name="Project service", tenant_id=agent.tenant_id,
                           base_url_template="https://example.com/mcp")
        db.add(server)
        await db.flush()
        tools = []
        for kind in ("scene_enabled", "scene_disabled", "unapproved_mcp"):
            tool = Tool(name=f"{kind}_{uuid.uuid4().hex[:10]}", display_name=kind, description=kind,
                        type="mcp" if kind == "unapproved_mcp" else "builtin", source="admin",
                        tenant_id=agent.tenant_id, enabled=True,
                        mcp_server_id=server.id if kind == "unapproved_mcp" else None,
                        parameters_schema={"type": "object", "properties": {}})
            db.add(tool)
            await db.flush()
            db.add(AgentTool(agent_id=aid, tool_id=tool.id, enabled=kind != "scene_enabled"))
            tools.append(tool)
        await save_scene(db, agent_id=aid, tenant_id=agent.tenant_id, scene_key="project-tools",
                         created_by_user_id=uid, data=SceneSaveRequest(name="Project tools", tools=[
                             {"tool_id": tool.id, "enabled": index != 1}
                             for index, tool in enumerate(tools)
                         ]))
        await publish_scene(db, agent_id=aid, scene_key="project-tools",
                            data=ScenePublishRequest(expected_revision=0), created_by_user_id=uid)
        anchor = await db.scalar(select(ChatMessage).where(
            ChatMessage.conversation_id == str(child.id), ChatMessage.role == "user"))
        anchor.message_meta = {**dict(anchor.message_meta or {}), "scene_key": "project-tools", "scene_revision": 1}
        await db.commit()
        context = await load_turn_scene_context(db, agent_id=aid, session_id=str(child.id), turn_anchor_id=anchor.id)
        return aid, uid, parent_id, child.id, anchor.id, [tool.name for tool in tools], context


async def test_project_scene_preparation_and_provider_keep_authorized_protocol(monkeypatch):
    aid, uid, _, child_id, anchor_id, names, context = await project_scene()
    async with restore_turn_tool_settings(aid, child_id, anchor_id):
        prepared = await runtime.prepare_subagent_tools(aid, child_id, execution_user_id=uid)
    assert current_tool_settings(aid) is None
    available = {tool["function"]["name"] for tool in prepared}
    assert names[0] in available
    assert names[1] not in available
    assert names[2] not in available  # Scene settings cannot grant an absent project MCP capability.
    assert {"project_get_context", "project_message_agent"} <= available
    assert "project_set_status" not in available  # Participant role remains authoritative.
    model_call = AsyncMock(return_value="Completed")
    monkeypatch.setattr(caller_failover, "_build_turn_context", AsyncMock(return_value=("static", "dynamic")))
    monkeypatch.setattr(caller_failover, "call_llm", model_call)
    for expected_tools in (prepared, []):
        await caller_failover.call_llm_with_failover.__wrapped__(
            SimpleNamespace(id=uuid.uuid4()), None, [], "Agent", "Assistant", agent_id=aid,
            user_id=uid, channel_context=context, prepared_tools=expected_tools,
        )
        received = model_call.call_args.kwargs["prepared_tools"]
        assert {tool["function"]["name"] for tool in received} == {
            tool["function"]["name"] for tool in expected_tools
        }
    assert current_tool_settings(aid) is None


async def test_scene_project_dispatch_retains_member_role_and_identity_authority():
    aid, uid, parent_id, child_id, anchor_id, names, _ = await project_scene()
    async with restore_turn_tool_settings(aid, child_id, anchor_id):
        async def dispatch(name, *, user_id=uid, session_id=child_id):
            return await execute_tool_preflight(name, {}, aid, user_id, session_id=str(session_id),
                                                turn_anchor_id=anchor_id)

        context = await dispatch("project_list_work_items")
        assert json.loads(context) == []
        assert await dispatch(names[1]) == render_message("sceneRuntime.toolDisabled")
        assert "current role" in await dispatch("project_set_status")
        assert "authorized project Subagent" in await dispatch("project_get_context", user_id=uuid.uuid4())
        assert "authorized project Subagent" in await dispatch("project_get_context", session_id=parent_id)
        async with async_session() as db:
            child = await db.get(ChatSession, child_id)
            project = await db.get(Project, child.project_id)
            assert project.status == "running"
            member = await db.scalar(select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id, ProjectMemberSnapshot.agent_id == aid))
            member.is_enabled = False
            await db.commit()
        assert "active runtime member" in await dispatch("project_get_context")
