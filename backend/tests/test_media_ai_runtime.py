"""PostgreSQL tool enablement, shared config, generation and restart behavior."""

import asyncio
import importlib
import json
import pkgutil
import uuid
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

import app.models
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services import media_ai_jobs, media_ai_runtime, media_ai_tools
from app.services.agent_tools import get_agent_tools_for_llm
from app.services.agent_tools_config_runtime import _get_tool_config, invalidate_tool_config_cache
from app.services.chat_history import persist_tool_call_row
from app.services.conversation_turn_lifecycle import transition_conversation_turn
from app.services.media_ai_io import MediaAIError
from app.services.media_ai_provider import connection
from app.services.storage import agent_storage_key, get_storage_backend
from app.services.tool_config import get_tenant_tool_config, set_tenant_tool_config
from app.services.tool_seeder import seed_builtin_tools
from app.services.turn_tool_settings import scene_tool_settings_scope

from test_media_ai_provider import MP4, PNG, WAV

for module in pkgutil.iter_modules(app.models.__path__, "app.models."):
    importlib.import_module(module.name)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def context():
    await seed_builtin_tools()
    async with async_session() as db:
        tenant = Tenant(name="Media tests", slug=f"media-{uuid.uuid4().hex}")
        db.add(tenant)
        await db.flush()
        user = User(tenant_id=tenant.id, display_name="Media tester", role="org_admin", is_active=True)
        db.add(user)
        await db.flush()
        agent = Agent(name="Media tester", tenant_id=tenant.id, creator_id=user.id, status="idle")
        db.add(agent)
        await db.flush()
        session = ChatSession(agent_id=agent.id, user_id=user.id, title="Media", source_channel="web")
        db.add(session)
        await db.flush()
        anchor = ChatMessage(agent_id=agent.id, user_id=user.id, role="user", conversation_id=str(session.id), content="Create media")
        db.add(anchor)
        await db.flush()
        await transition_conversation_turn(db, agent_id=agent.id, conversation_id=str(session.id), turn_anchor_id=anchor.id, status="running")
        await set_tenant_tool_config(db, tenant.id, "read_media", {"api_key": "test-key"})
        tool = await db.scalar(select(Tool).where(Tool.name == "generate_media"))
        db.add(AgentTool(agent_id=agent.id, tool_id=tool.id, enabled=True))
        await db.commit()
        state = SimpleNamespace(agent_id=agent.id, user_id=user.id, tenant_id=tenant.id,
                                session_id=str(session.id), turn_anchor_id=anchor.id,
                                tool_name="generate_media", tool_call_id=f"call_{uuid.uuid4().hex}",
                                arguments={"output_type": "image", "prompt": "a blue circle"})
    yield state
    invalidate_tool_config_cache(state.agent_id)
    await engine.dispose()


async def running_row(state):
    async with async_session() as db:
        row_id = await persist_tool_call_row(
            db, agent_id=state.agent_id, user_id=state.user_id, conversation_id=state.session_id,
            evt={"name": "generate_media", "call_id": state.tool_call_id, "args": state.arguments,
                 "status": "running", "round_id": "media-round", "round_tool_index": 0},
            turn_anchor_id=state.turn_anchor_id,
        )
        await db.commit()
        return await db.get(ChatMessage, row_id)


async def test_seeded_runtime_is_opt_in_and_company_config_is_shared(context):
    tools = await get_agent_tools_for_llm(context.agent_id)
    names = {tool["function"]["name"] for tool in tools}
    assert "generate_media" in names
    assert "read_media" not in names
    assert next(t for t in tools if t["function"]["name"] == "generate_media")["function"]["parameters"]["required"] == ["prompt", "output_type"]
    assert (await _get_tool_config(context.agent_id, "generate_media"))["api_key"] == "test-key"
    async with async_session() as db:
        assert (await get_tenant_tool_config(db, context.tenant_id, "generate_media"))["api_key"] == "test-key"
        assert await get_tenant_tool_config(db, uuid.uuid4(), "generate_media") == {}
        understanding = await db.scalar(select(Tool).where(Tool.name == "read_media"))
    async with scene_tool_settings_scope(context.agent_id, {"scene_tools": [
        {"tool_id": str(understanding.id), "enabled": True, "config": {}},
    ]}):
        assert (await _get_tool_config(context.agent_id, "read_media"))["api_key"] == "test-key"
        scene_tools = await get_agent_tools_for_llm(context.agent_id)
        assert "generate_media" not in {tool["function"]["name"] for tool in scene_tools}


async def test_image_generation_saves_file_and_repeated_call_does_not_submit(context, monkeypatch):
    await running_row(context)
    submitted = []
    async def provider(*args, **kwargs):
        submitted.append(args)
        return {"output": {"choices": [{"message": {"content": [{"image": "https://results.example/image.png"}]}}]},
                "usage": {"image_count": 1}}
    async def download(*_, **__):
        return PNG
    monkeypatch.setattr(media_ai_runtime, "request", provider)
    monkeypatch.setattr(media_ai_jobs, "download_media", download)
    from test_media_ai_sessions import run_task
    receipt = json.loads(await media_ai_tools.execute_media_tool(context))
    duplicate = json.loads(await media_ai_tools.execute_media_tool(context))
    assert receipt == duplicate and submitted == []
    completed = await run_task(receipt)
    first = completed[0].message_meta["media_result"]
    assert first["status"] == "completed"
    assert len(submitted) == 1
    path = first["files"][0]["path"]
    assert await get_storage_backend().read_bytes(agent_storage_key(context.agent_id, path)) == PNG
    assert path in first["markdown"]
    async with async_session() as db:
        rows = (await db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == context.session_id))).all()
        assert "test-key" not in json.dumps([row.message_meta for row in rows])


@pytest.mark.parametrize("kind,data", [("video", MP4), ("audio", WAV)])
async def test_generation_delivers_playable_attachment_and_survives_reentry(context, monkeypatch, kind, data):
    context.arguments = {"output_type": kind, "prompt": "hello"}
    row = await running_row(context)
    config = connection({"api_key": "test-key"})
    job, claimed = await media_ai_jobs.create_intent(row, config, kind, context.agent_id)
    assert claimed
    upstream = {"output": {"task_id": "opaque-task-id"}} if kind == "video" else {"output": {"audio": {"url": "https://results.example/audio.wav"}}}
    await media_ai_jobs.accept_result(row, job, upstream)
    gets = []
    async def provider(*args, **kwargs):
        gets.append(args)
        if len(gets) == 1:
            raise httpx.ConnectError("temporary connection issue")
        return {"output": {"task_status": "SUCCEEDED", "video_url": "https://results.example/video.mp4"}}
    async def download(*_, **__):
        return data
    async def no_wait(_):
        return None
    monkeypatch.setattr(media_ai_jobs, "request", provider)
    monkeypatch.setattr(media_ai_jobs, "download_media", download)
    monkeypatch.setattr(media_ai_jobs.asyncio, "sleep", no_wait)
    async with async_session() as db:
        row = await db.get(ChatMessage, row.id)
    async def guard():
        return True
    result = json.loads(await media_ai_jobs.recover_generation(row, execution_agent_id=context.agent_id, guard=guard))
    assert result["status"] == "completed"
    assert result["delivery"]["status"] == "sent"
    receipt_id = uuid.UUID(result["delivery"]["message_id"])
    async with async_session() as db:
        receipt = await db.get(ChatMessage, receipt_id)
        assert receipt.id == row.id
        assert receipt.message_meta["attachments"][0]["kind"] == kind
        assert receipt.message_meta["media_job"]["status"] == "completed"
        assert json.loads(receipt.content)["name"] == "generate_media"
        assert json.loads(receipt.content)["status"] == "done"
    if kind == "video":
        assert len(gets) == 2
        assert all(args[1] == "/api/v1/tasks/opaque-task-id" for args in gets)


async def test_unknown_submission_is_not_resubmitted(context, monkeypatch):
    row = await running_row(context)
    config = connection({"api_key": "test-key"})
    job, _ = await media_ai_jobs.create_intent(row, config, "image", context.agent_id)
    async def forbidden(*_, **__):
        pytest.fail("An unknown submission must not be resubmitted")
    monkeypatch.setattr(media_ai_runtime, "request", forbidden)
    with pytest.raises(MediaAIError, match="submissionUnknown"):
        await media_ai_jobs.finish_generation(row, job, config)


async def test_stopped_turn_cannot_save_or_deliver(context):
    row = await running_row(context)
    config = connection({"api_key": "test-key"})
    job, _ = await media_ai_jobs.create_intent(row, config, "image", context.agent_id)
    async with async_session() as db:
        await transition_conversation_turn(db, agent_id=context.agent_id, conversation_id=context.session_id,
                                          turn_anchor_id=context.turn_anchor_id, status="cancelled")
        await db.commit()
    with pytest.raises(asyncio.CancelledError):
        await media_ai_jobs.finish_generation(row, job, config)


async def test_paths_cannot_escape_agent_workspace(context):
    from app.services.media_ai_io import load_media
    with pytest.raises(MediaAIError, match="invalidPath"):
        await load_media(context.agent_id, ["../another-agent/private.png"])


async def test_real_dispatch_enforces_tool_enablement(context, monkeypatch):
    from app.services.agent_tools import execute_tool
    async def forbidden(*_, **__):
        pytest.fail("Disabled tool must never call a provider")
    monkeypatch.setattr(media_ai_runtime, "understand", forbidden)
    result = await execute_tool("read_media", {"prompt": "summarize", "files": ["private.wav"]},
                                agent_id=context.agent_id, user_id=context.user_id,
                                session_id=context.session_id, tool_call_id="disabled-call",
                                turn_anchor_id=context.turn_anchor_id)
    assert "completed" not in result
    assert json.loads(result)["code"] == "toolDisabled"


async def test_common_recovery_closes_original_tool_with_media_result(context, monkeypatch):
    from app.services import turn_recovery
    row = await running_row(context)
    config = connection({"api_key": "test-key"})
    job, _ = await media_ai_jobs.create_intent(row, config, "image", context.agent_id)
    await media_ai_jobs.accept_result(row, job, {"output": {"choices": [{"message": {
        "content": [{"image": "https://results.example/image.png"}],
    }}]}})
    async def download(*_, **__):
        return PNG
    monkeypatch.setattr(media_ai_jobs, "download_media", download)
    async with async_session() as db:
        anchor = await db.get(ChatMessage, context.turn_anchor_id)
        origin = await turn_recovery._load_recovery_origin(db, anchor)
        assert origin is not None
    async with async_session() as db:
        count = await turn_recovery._complete_unfinished_tool_calls(
            db, anchor, ctx_size=100, expected_origin=origin,
            execution_agent_id=context.agent_id, release_db_before_execution=True,
        )
    assert count == 1
    async with async_session() as db:
        rows = (await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == context.session_id, ChatMessage.role == "tool_call",
        ))).all()
        done = [json.loads(row.content) for row in rows if json.loads(row.content).get("status") == "done"]
        assert done and all(json.loads(item["result"])["type"] == "media_generation" for item in done)


@pytest.mark.parametrize("kind,data", [("image", PNG), ("audio", WAV), ("video", MP4)])
async def test_a2a_uses_execution_workspace_not_canonical_history_owner(context, monkeypatch, kind, data):
    context.arguments["output_type"] = kind
    row = await running_row(context)
    async with async_session() as db:
        peer = Agent(name="Media peer", tenant_id=context.tenant_id, creator_id=context.user_id, status="idle")
        db.add(peer)
        await db.flush()
        session = await db.get(ChatSession, uuid.UUID(context.session_id))
        session.source_channel = "agent"
        session.peer_agent_id = peer.id
        await db.commit()
        peer_id = peer.id
    context.agent_id = peer_id
    found = await media_ai_jobs.find_generation_row(context)
    assert found.id == row.id
    job, _ = await media_ai_jobs.create_intent(row, connection({"api_key": "test-key"}), kind, peer_id)
    output = {"image": {"choices": [{"message": {"content": [{"image": "https://results.example/image.png"}]}}]},
              "audio": {"audio": {"url": "https://results.example/audio.wav"}},
              "video": {"video_url": "https://results.example/video.mp4"}}[kind]
    await media_ai_jobs.accept_result(row, job, {"output": output})
    async def download(*_, **__):
        return data
    monkeypatch.setattr(media_ai_jobs, "download_media", download)
    result = json.loads(await media_ai_jobs.finish_generation(row, job, connection({"api_key": "test-key"})))
    storage = get_storage_backend()
    assert await storage.exists(agent_storage_key(peer_id, result["files"][0]["path"]))
    assert not await storage.exists(agent_storage_key(row.agent_id, result["files"][0]["path"]))
    assert result["status"] == "completed"
    if kind != "image":
        assert result["delivery"]["status"] == "unsupported"
        assert result["delivery"]["code"] == "CHANNEL_MEDIA_UNSUPPORTED"


async def test_standard_binary_generation_persists_file_and_result_without_binary_metadata(context, monkeypatch):
    calls = []

    async def provider(*args, **kwargs):
        calls.append(args)
        return {"_bytes": PNG, "usage": {"total_tokens": 3}}

    monkeypatch.setattr(media_ai_runtime, "request", provider)
    from test_media_ai_sessions import run_task

    receipt = json.loads(await media_ai_tools.execute_media_tool(context))
    rows = await run_task(receipt)
    assert len(calls) == 1
    result = rows[0].message_meta["media_result"]
    assert result["status"] == "completed"
    assert await get_storage_backend().read_bytes(agent_storage_key(context.agent_id, result["files"][0]["path"])) == PNG
    async with async_session() as db:
        tool = await db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == receipt["session_id"],
                                                        ChatMessage.role == "tool_call"))
        job = tool.message_meta["media_job"]
        assert job["status"] == "completed"
        assert '"_bytes"' not in json.dumps(tool.message_meta)
    repeated = json.loads(await media_ai_tools.execute_media_tool(context))
    assert repeated["task_id"] == receipt["task_id"]
    assert repeated["status"] == "completed"
    assert len(calls) == 1
