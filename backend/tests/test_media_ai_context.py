"""Media snapshots, shared compaction and native streaming over observable boundaries."""

import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction
from app.services import media_ai_runtime, subagent_runtime
from app.services.media_ai_model import media_context_model
from app.services.llm.client import LLMResponse
from app.services.turn_tool_settings import scene_tool_settings_scope
from app.models.tool import Tool
from test_media_ai_provider import mock_http
from test_media_ai_sessions import submit, run_task, enable_read
import test_media_ai_sessions as session_tests
import test_media_ai_runtime as runtime_tests

context = runtime_tests.context
providers = session_tests.providers
pytestmark = pytest.mark.asyncio


async def test_scene_connection_is_frozen_for_queued_execution(context, providers, monkeypatch):
    async with async_session() as db:
        tool = await db.scalar(select(Tool).where(Tool.name == "generate_media"))
    async with scene_tool_settings_scope(context.agent_id, {"scene_tools": [
        {"tool_id": str(tool.id), "enabled": True, "config": {"api_key": "scene-secret"}},
    ]}):
        receipt = await submit(context)
    observed = []
    original = media_ai_runtime.request

    async def capture(config, *args, **kwargs):
        observed.append(config["api_key"])
        return await original(config, *args, **kwargs)

    monkeypatch.setattr(media_ai_runtime, "request", capture)
    await run_task(receipt)
    assert observed == ["scene-secret"]
    async with async_session() as db:
        anchor = await db.get(ChatMessage, uuid.UUID(receipt["task_id"]))
        assert "scene-secret" not in json.dumps(anchor.message_meta)


async def test_media_summary_uses_configured_enterprise_model_streaming_protocol(context, monkeypatch):
    from app.services.llm.compactor import _summarize_via_llm

    await enable_read(context)
    receipt = await submit(context, files=["image.png"])
    async with async_session() as db:
        anchor = await db.get(ChatMessage, uuid.UUID(receipt["task_id"]))
        agent = await db.get(Agent, context.agent_id)
    model = await media_context_model(agent, anchor.message_meta["media_request"])
    calls = []

    def upstream(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["stream"] is True
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            text=(
                'data: {"choices":[{"delta":{"content":"Summary text"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                'data: [DONE]\n\n'
            ),
        )

    mock_http(monkeypatch, upstream)
    text, _ = await _summarize_via_llm(span_text="Remember the blue circle", prior_summary=None, model=model)
    assert text == "Summary text" and len(calls) == 1


async def test_queued_media_compacts_completed_turns_and_keeps_native_context(context, providers, monkeypatch):
    await enable_read(context)
    calls = []

    async def understand(config, prompt, media, *, history):
        calls.append(history)
        return LLMResponse(content="The circle is blue.", usage={"prompt_tokens": 250000})

    monkeypatch.setattr(media_ai_runtime, "understand_response", understand)
    # Force the shared deterministic fallback; it still archives the exact source
    # and persists a validated summary through the real database/storage path.
    from app.services.llm import compactor

    async def unavailable(**kwargs):
        raise RuntimeError("summary unavailable")

    monkeypatch.setattr(compactor, "_summarize_via_llm", unavailable)
    first = await submit(context, files=["workspace/source.png"], prompt="Describe the blue circle and preserve its source")
    receipts = [first]
    for index in range(5):
        receipts.append(await submit(context, session_id=first["session_id"], prompt=f"Question {index}: what color is the circle?"))
    results = await run_task(first)
    assert len(results) == 6
    assert all(row.message_meta["media_result"]["status"] == "completed" for row in results)
    async with async_session() as db:
        markers = (await db.scalars(select(ChatCompaction).where(ChatCompaction.session_id == first["session_id"]))).all()
        assert markers
        assert any("workspace/source.png" in marker.summary_text for marker in markers)
        last = await db.get(ChatMessage, uuid.UUID(receipts[-1]["task_id"]))
        assert last.compacted_into is None
    assert any("conversation-summary" in str(item["content"]) for item in calls[-1])


@pytest.mark.parametrize("stop_root", [True, False])
async def test_stop_parent_cancels_nested_media_jobs(context, providers, stop_root):
    from app.services.conversation_turn_lifecycle import transition_conversation_turn
    from app.services.turn_control import stop_session_turn_tree

    original_session = context.session_id
    parent, _ = await subagent_runtime.create_subagent(
        agent_id=context.agent_id, execution_user_id=context.user_id,
        parent_session_id=original_session, origin_tool_call_id="nested-stop",
        name="Review", task="Review", mode="async", turn_anchor_id=context.turn_anchor_id,
    )
    async with async_session() as db:
        anchor = await db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == str(parent.id)))
        await transition_conversation_turn(db, agent_id=context.agent_id, conversation_id=str(parent.id),
                                          turn_anchor_id=anchor.id, status="running")
        await db.commit()
    context.session_id, context.turn_anchor_id = str(parent.id), anchor.id
    receipt = await submit(context)
    await stop_session_turn_tree(agent_id=context.agent_id, session_id=original_session if stop_root else parent.id, reason="test")
    async with async_session() as db:
        media = await db.get(ChatMessage, uuid.UUID(receipt["task_id"]))
        assert media.message_meta["subagent_input_state"] == "cancelled"
        assert media.message_meta["turn_status"] == "cancelled"
    assert await subagent_runtime._claim_subagent(uuid.UUID(receipt["session_id"])) is None


async def test_task_status_is_exposed_through_real_session_http_route(context, providers):
    from fastapi import FastAPI
    from app.api.chat_sessions import router
    from app.core.security import get_current_user
    from app.models.user import User

    receipt = await submit(context)
    await run_task(receipt)
    app = FastAPI()
    app.include_router(router)
    async with async_session() as db:
        user = await db.get(User, context.user_id)
    app.dependency_overrides[get_current_user] = lambda: user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f'/api/agents/{context.agent_id}/sessions/{receipt["session_id"]}', params={"task_id": receipt["task_id"]})
        assert response.status_code == 200, response.text
        assert response.json()["runtime"]["task_id"] == receipt["task_id"]
        assert response.json()["runtime"]["status"] == "completed"


@pytest.mark.parametrize("followup_options", [None, {}, {"seed": 29}])
async def test_generation_continuation_preserves_or_replaces_native_parameters(context, providers, followup_options):
    from app.services.media_ai_tools import execute_media_tool

    original = {"seed": 17, "watermark": False}
    context.arguments = {"output_type": "image", "prompt": "A blue circle", "parameters": original}
    first = json.loads(await execute_media_tool(context))
    context.tool_call_id = f"call_{uuid.uuid4().hex}"
    context.arguments = {"output_type": "image", "prompt": "Make it red", "session_id": first["session_id"]}
    if followup_options is not None:
        context.arguments["parameters"] = followup_options
    second = json.loads(await execute_media_tool(context))
    results = await run_task(first)
    expected = original if followup_options is None else followup_options
    assert len(results) == 2
    assert providers[0]["parameters"] == {"n": 1, "size": "1024*1024", **original}
    assert providers[1]["parameters"] == {"n": 1, "size": "1024*1024", **expected}
    assert "image" in providers[1]["input"]["messages"][0]["content"][0]
    assert results[1].message_meta["media_result"]["task_id"] == second["task_id"]
    assert results[1].message_meta["media_context"]["native_parameters"] == expected


async def test_media_header_secret_is_frozen_only_in_encrypted_connection(context, providers, monkeypatch):
    from app.services import media_ai_tools

    resolve = media_ai_tools.resolve_media_model
    generate = media_ai_runtime.request
    configured = {"X-Provider-Key": "private-routing-value"}
    observed = []

    async def resolve_with_headers(*args, **kwargs):
        return {**await resolve(*args, **kwargs), "extra_headers": dict(configured)}

    async def capture(config, *args, **kwargs):
        observed.append(config["extra_headers"])
        return await generate(config, *args, **kwargs)

    monkeypatch.setattr(media_ai_tools, "resolve_media_model", resolve_with_headers)
    monkeypatch.setattr(media_ai_runtime, "request", capture)
    receipt = await submit(context)
    configured["X-Provider-Key"] = "changed-after-admission"
    results = await run_task(receipt)
    assert results[0].message_meta["media_result"]["status"] == "completed"
    assert observed == [{"X-Provider-Key": "private-routing-value"}]
    async with async_session() as db:
        rows = (await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == receipt["session_id"],
        ))).all()
        for row in rows:
            assert "private-routing-value" not in json.dumps(row.message_meta or {})
