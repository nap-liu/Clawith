"""Observable asynchronous media sessions over the real PostgreSQL child worker."""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun
from app.models.tool import AgentTool, Tool
from app.services import media_ai_context, media_ai_jobs, media_ai_runtime, media_ai_tools, subagent_runtime
from app.services.media_ai_io import MediaInput
from app.services.llm.client import LLMResponse
from app.services.storage import agent_storage_key, get_storage_backend
from test_media_ai_provider import PNG
import test_media_ai_runtime as runtime_tests

context = runtime_tests.context
pytestmark = pytest.mark.asyncio


async def submit(state, *, session_id=None, prompt="A blue circle", files=None):
    state.tool_call_id = f"call_{uuid.uuid4().hex}"
    state.arguments = {"prompt": prompt}
    if state.tool_name == "generate_media":
        state.arguments["output_type"] = "image"
    if session_id:
        state.arguments["session_id"] = session_id
    if files is not None:
        state.arguments["files"] = files
    return json.loads(await media_ai_tools.execute_media_tool(state))


async def run_task(receipt):
    claimed = await subagent_runtime._claim_subagent(uuid.UUID(receipt["session_id"]), with_token=True)
    assert claimed is not None
    await subagent_runtime.execute_claimed_subagent(claimed[0], lease_owner=claimed[1])
    async with async_session() as db:
        return (await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == receipt["session_id"], ChatMessage.role == "assistant",
        ).order_by(ChatMessage.created_at, ChatMessage.id))).all()


async def enable_read(state):
    state.tool_name = "read_media"
    async with async_session() as db:
        tool = await db.scalar(select(Tool).where(Tool.name == "read_media"))
        db.add(AgentTool(agent_id=state.agent_id, tool_id=tool.id, enabled=True))
        await db.commit()


@pytest.fixture
def providers(monkeypatch):
    calls = []

    async def generate(config, path, payload, **kwargs):
        calls.append(payload)
        return {"output": {"choices": [{"message": {"content": [{"image": "https://result.example/image.png"}]}}]}}

    async def download(*args, **kwargs):
        return PNG

    async def load(agent_id, files):
        return [MediaInput(item if isinstance(item, str) else item["source"], "image/png", PNG) for item in files]

    async def no_llm(*args, **kwargs):
        pytest.fail("Media jobs must not invoke the Agent LLM loop")

    monkeypatch.setattr(media_ai_runtime, "request", generate)
    monkeypatch.setattr(media_ai_runtime, "load_media", load)
    monkeypatch.setattr(media_ai_context, "load_media", load)
    monkeypatch.setattr(media_ai_jobs, "download_media", download)
    from app.services import channel_llm
    monkeypatch.setattr(channel_llm, "_call_agent_llm", no_llm)
    return calls


async def test_entry_returns_durable_job_without_provider_wait_or_child_llm(context, providers):
    from app.services.chat_message_serializer import serialize_chat_message_for_client, serialize_tool_call_for_client

    receipt = await submit(context)
    assert receipt["status"] == "queued"
    assert receipt["task_id"] != receipt["session_id"]
    assert providers == []
    duplicate = json.loads(await media_ai_tools.execute_media_tool(context))
    assert duplicate == receipt
    async with async_session() as db:
        child = await db.get(ChatSession, uuid.UUID(receipt["session_id"]))
        run = await db.get(SubagentRun, child.id)
        assert child.im_config["executor"] == "media"
        assert run.model is None and run.model_id is None
    results = await run_task(receipt)
    assert len(results) == 1
    assert len(providers) == 1
    assert results[0].message_meta["media_result"]["status"] == "completed"
    assert results[0].message_meta["attachments"][0]["kind"] == "image"
    assert results[0].message_meta["subagent_wake"] is True
    assert serialize_chat_message_for_client(results[0])["mediaTaskId"] == receipt["task_id"]
    async with async_session() as db:
        internal = await db.scalar(select(ChatMessage).where(
            ChatMessage.conversation_id == receipt["session_id"], ChatMessage.role == "tool_call",
        ))
        assert serialize_tool_call_for_client(internal)["mediaTaskId"] == receipt["task_id"]
        internal.message_meta = {"turn_anchor_id": receipt["task_id"]}
        assert "mediaTaskId" not in serialize_tool_call_for_client(internal)


async def test_queued_turns_are_separate_and_each_completes_with_its_own_result(context, providers):
    first = await submit(context, prompt="A blue circle")
    second = await submit(context, session_id=first["session_id"], prompt="Make it red")
    assert first["session_id"] == second["session_id"]
    assert first["task_id"] != second["task_id"]
    results = await run_task(first)
    assert len(results) == 2
    assert [row.message_meta["media_result"]["task_id"] for row in results] == [first["task_id"], second["task_id"]]
    assert all(row.message_meta["subagent_wake"] for row in results)
    assert len(providers) == 2
    assert len(providers[0]["input"]["messages"][0]["content"]) == 1
    assert "image" in providers[1]["input"]["messages"][0]["content"][0]
    assert providers[1]["input"]["messages"][0]["content"][-1]["text"] == "Make it red"
    assert "Make it red" not in providers[0]["input"]["messages"][0]["content"][-1]["text"]
    async with async_session() as db:
        run = await db.get(SubagentRun, uuid.UUID(first["session_id"]))
        assert run.status == "completed" and run.lease_owner is None


async def test_read_followup_receives_prior_answer_even_when_queued_before_it(context, providers, monkeypatch):
    await enable_read(context)
    histories = []

    async def understand(config, prompt, media, *, history):
        histories.append(history)
        return LLMResponse(content="There is a blue circle" if not history else "The circle is blue", usage={"prompt_tokens": 123})

    monkeypatch.setattr(media_ai_runtime, "understand_response", understand)
    first = await submit(context, prompt="Describe this", files=["image.png"])
    second = await submit(context, session_id=first["session_id"], prompt="What color is it?")
    results = await run_task(first)
    assert len(results) == 2 and histories[0] == []
    assert histories[1][-1] == {"role": "assistant", "content": "There is a blue circle"}
    assert histories[1][0]["content"][1]["type"] == "image_url"
    assert results[1].message_meta["media_result"]["task_id"] == second["task_id"]


async def test_completed_session_can_continue_and_wrong_parent_cannot_take_it_over(context, providers):
    first = await submit(context)
    await run_task(first)
    second = await submit(context, session_id=first["session_id"], prompt="Make it green")
    assert second["status"] == "queued"
    results = await run_task(second)
    assert len(results) == 2
    parent_id = context.session_id
    async with async_session() as db:
        other = ChatSession(agent_id=context.agent_id, user_id=context.user_id, source_channel="web", title="Other")
        db.add(other)
        await db.commit()
        context.session_id = str(other.id)
    rejected = await submit(context, session_id=first["session_id"])
    assert rejected["code"] == "contextRequired"
    context.session_id = parent_id
    rejected = await submit(context, session_id=parent_id)
    assert rejected["code"] == "contextRequired"


async def test_outer_tool_recovery_returns_original_receipt_without_resubmission(context, providers):
    from test_media_ai_runtime import running_row
    from app.services.media_ai_sessions import recover_media_submission

    row = await running_row(context)
    receipt = json.loads(await media_ai_tools.execute_media_tool(context))
    recovered = json.loads(await recover_media_submission(row))
    assert recovered == receipt and providers == []


async def test_restart_after_provider_result_saves_once_without_reposting(context, providers, monkeypatch):
    receipt = await submit(context)
    original_finish = media_ai_runtime.finish_generation

    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(media_ai_runtime, "finish_generation", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await run_task(receipt)
    assert len(providers) == 1
    monkeypatch.setattr(media_ai_runtime, "finish_generation", original_finish)
    results = await run_task(receipt)
    assert len(providers) == 1 and len(results) == 1
    item = results[0].message_meta["attachments"][0]
    assert await get_storage_backend().read_bytes(agent_storage_key(context.agent_id, item["path"])) == PNG


async def test_interrupted_read_reports_failure_without_replaying_model(context, providers, monkeypatch):
    await enable_read(context)
    count = 0

    async def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        raise asyncio.CancelledError

    monkeypatch.setattr(media_ai_runtime, "understand_response", interrupted)
    receipt = await submit(context, files=["image.png"])
    with pytest.raises(asyncio.CancelledError):
        await run_task(receipt)
    results = await run_task(receipt)
    assert count == 1
    assert results[0].message_meta["media_result"]["code"] == "analysisInterrupted"


async def test_stop_one_running_job_preserves_next_queued_turn(context, providers, monkeypatch):
    await enable_read(context)
    entered = asyncio.Event()
    block = asyncio.Event()
    calls = []

    async def analyze(config, prompt, media, **kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            entered.set()
            await block.wait()
        return LLMResponse(content="Done")

    monkeypatch.setattr(media_ai_runtime, "understand_response", analyze)
    first = await submit(context, files=["image.png"], prompt="First")
    second = await submit(context, session_id=first["session_id"], files=["image.png"], prompt="Second")
    worker = asyncio.create_task(run_task(first))
    await asyncio.wait_for(entered.wait(), 5)
    status = await subagent_runtime.stop_subagent(
        agent_id=context.agent_id, parent_session_id=context.session_id,
        subagent_id=first["session_id"], execution_user_id=context.user_id, task_id=first["task_id"],
    )
    assert status == "cancelled"
    with pytest.raises(asyncio.CancelledError):
        await worker
    results = await run_task(second)
    assert len(results) == 1 and calls == ["First", "Second"]
    assert results[0].message_meta["media_result"]["task_id"] == second["task_id"]


async def test_cancel_queued_job_does_not_cancel_predecessor(context, providers):
    first = await submit(context)
    second = await submit(context, session_id=first["session_id"])
    await subagent_runtime.stop_subagent(
        agent_id=context.agent_id, parent_session_id=context.session_id,
        subagent_id=first["session_id"], execution_user_id=context.user_id, task_id=second["task_id"],
    )
    results = await run_task(first)
    assert len(results) == 1 and len(providers) == 1
    async with async_session() as db:
        cancelled = await db.get(ChatMessage, uuid.UUID(second["task_id"]))
        assert cancelled.message_meta["turn_status"] == "cancelled"


async def test_task_detail_reports_each_turn_not_latest_session_status(context, providers):
    from app.api.chat_session_access import _build_session_detail_out

    first = await submit(context)
    await run_task(first)
    second = await submit(context, session_id=first["session_id"])
    async with async_session() as db:
        child = await db.get(ChatSession, uuid.UUID(first["session_id"]))
        old = await _build_session_detail_out(db, child, "mine", task_id=uuid.UUID(first["task_id"]))
        new = await _build_session_detail_out(db, child, "mine", task_id=uuid.UUID(second["task_id"]))
        assert old.runtime.status == "completed"
        assert new.runtime.status == "queued"
        assert new.runtime.executor == "media"


async def test_child_agent_media_completion_reenters_parent_inbox_without_normal_resume(context, providers, monkeypatch):
    from app.services.conversation_turn_lifecycle import transition_conversation_turn

    run, _ = await subagent_runtime.create_subagent(
        agent_id=context.agent_id, execution_user_id=context.user_id,
        parent_session_id=context.session_id, origin_tool_call_id="delegate-parent",
        name="Media reviewer", task="Review media", mode="async", turn_anchor_id=context.turn_anchor_id,
    )
    async with async_session() as db:
        parent_anchor = await db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == str(run.id)))
        await transition_conversation_turn(db, agent_id=context.agent_id, conversation_id=str(run.id),
                                          turn_anchor_id=parent_anchor.id, status="running")
        await db.commit()
    context.session_id = str(run.id)
    context.turn_anchor_id = parent_anchor.id
    receipt = await submit(context)
    async with async_session() as db:
        parent_run = await db.get(SubagentRun, run.id)
        parent_run.status = "completed"
        parent_anchor = await db.get(ChatMessage, parent_anchor.id)
        parent_anchor.message_meta = {**parent_anchor.message_meta, "subagent_input_state": "done"}
        await transition_conversation_turn(db, agent_id=context.agent_id, conversation_id=str(run.id),
                                          turn_anchor_id=parent_anchor.id, status="completed")
        await db.commit()
    results = await run_task(receipt)

    async def forbidden(*args, **kwargs):
        pytest.fail("Nested completion must not use normal resume_turn")

    from app.services import turn_recovery
    monkeypatch.setattr(turn_recovery, "resume_turn", forbidden)
    assert await subagent_runtime._dispatch_parent_event(results[0].id)
    assert await subagent_runtime._dispatch_parent_event(results[0].id)
    async with async_session() as db:
        parent_run = await db.get(SubagentRun, run.id)
        inbox = (await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == str(run.id),
            ChatMessage.message_meta["child_message_id"].as_string() == str(results[0].id),
        ))).all()
        assert parent_run.status == "queued"
        assert len(inbox) == 1 and inbox[0].message_meta["subagent_input_state"] == "pending"
        assert inbox[0].message_meta["attachments"][0]["kind"] == "image"


@pytest.mark.parametrize("kind", ["audio", "video"])
async def test_async_result_delivers_to_original_web_session_once(context, providers, monkeypatch, kind):
    from test_media_ai_provider import WAV, MP4

    async def generate(*args, **kwargs):
        return {"output": {"audio": {"url": "https://result.example/audio.wav"}}} if kind == "audio" else {"output": {"video_url": "https://result.example/video.mp4"}}

    async def download(*args, **kwargs):
        return WAV if kind == "audio" else MP4

    monkeypatch.setattr(media_ai_runtime, "request", generate)
    monkeypatch.setattr(media_ai_jobs, "download_media", download)
    context.arguments = {"output_type": kind, "prompt": "Hello"}
    receipt = json.loads(await media_ai_tools.execute_media_tool(context))
    results = await run_task(receipt)
    delivery = results[0].message_meta["media_result"]["delivery"]
    assert delivery["status"] == "sent"
    async with async_session() as db:
        row = await db.get(ChatMessage, uuid.UUID(delivery["message_id"]))
        assert row.conversation_id == context.session_id
        assert row.message_meta["attachments"][0]["kind"] == kind
        assert row.message_meta["delivery"]["status"] == "sent"
        assert json.loads(row.content)["status"] == "done"


async def test_responses_snapshot_survives_media_completion_and_followup(context, providers, monkeypatch):
    await enable_read(context)
    snapshot = {"protocol": "openai_responses", "endpoint": "https://models.example/v1", "model": "test-model",
                "response_id": "response-1", "output": [{"type": "reasoning", "encrypted_content": "opaque-state"}]}
    histories = []

    async def understand(config, prompt, media, *, history):
        histories.append(history)
        return LLMResponse(content="A circle", responses_snapshot=snapshot)

    monkeypatch.setattr(media_ai_runtime, "understand_response", understand)
    first = await submit(context, files=["image.png"])
    second = await submit(context, session_id=first["session_id"], prompt="Which shape?")
    rows = await run_task(first)
    assert len(rows) == 2
    assert rows[0].message_meta["responses_snapshot"] == snapshot
    assert "opaque-state" not in json.dumps(rows[0].message_meta["media_result"])
    assert histories[1][-1]["responses_snapshot"] == snapshot
    async with async_session() as db:
        anchor = await db.get(ChatMessage, uuid.UUID(first["task_id"]))
        assert anchor.message_meta["media_responses_snapshot"] == snapshot
    assert rows[1].message_meta["media_result"]["task_id"] == second["task_id"]
