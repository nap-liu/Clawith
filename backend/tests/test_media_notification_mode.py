"""Completed media tests retain notification policy when their session continues."""
import uuid

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.subagent_run import SubagentRun
from app.services import subagent_runtime
from app.services.agent_tools_config_runtime import _get_tool_config
from app.services.media_ai_sessions import enqueue_media
from app.services.media_model_selection import resolve_media_model
import test_media_ai_sessions as media_tests
from tests.test_subagent_runtime import (
    _dispose_engine_between_tests,  # noqa: F401
    _make_context,
)

pytestmark = pytest.mark.asyncio
context = media_tests.context
providers = media_tests.providers
run_task = media_tests.run_task


@pytest.mark.parametrize("notify", [False, True])
async def test_completed_media_followup_retains_notification_mode(context, providers, notify):
    config = await resolve_media_model(context, await _get_tool_config(context.agent_id, context.tool_name))
    first = await enqueue_media(context, config, notify_parent=notify)
    first_results = await run_task(first)
    assert first_results[-1].message_meta["subagent_wake"] is notify
    context.tool_call_id = f"followup-{uuid.uuid4()}"
    context.arguments = {"session_id": first["session_id"], "output_type": "image", "prompt": "Make it red"}
    second = await enqueue_media(context, config, notify_parent=notify)
    assert second["session_id"] == first["session_id"]
    results = await run_task(second)
    assert len(results) == 2 and len(providers) == 2
    assert all(row.message_meta["media_result"]["status"] == "completed" for row in results)
    assert all(row.message_meta["subagent_wake"] is notify for row in results)
    assert all((row.message_meta.get("subagent_dispatch_state") == "pending") is notify for row in results)
    async with async_session() as db:
        run = await db.get(SubagentRun, uuid.UUID(second["session_id"]))
        assert run.mode == ("async" if notify else "sync")
        assert run.status == "completed"
        assert run.execution_user_id == context.user_id
    assert "image" in providers[1]["input"]["messages"][0]["content"][0]


async def test_completed_regular_sync_subagent_followup_still_notifies_parent():
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await subagent_runtime.create_subagent(
        agent_id=agent_id, execution_user_id=user_id, parent_session_id=str(parent_id),
        origin_tool_call_id="first", task="First request", mode="sync", turn_anchor_id=anchor_id,
    )
    assert await subagent_runtime._claim_subagent(run.id) == run.id
    first, _ = await subagent_runtime._load_or_start_input(run.id)
    assert await subagent_runtime._finish_subagent_turn(
        run_id=run.id, anchor_id=first.id, reply="First answer", failed=False,
    )
    await subagent_runtime.append_subagent_message(
        agent_id=agent_id, execution_user_id=user_id, parent_session_id=str(parent_id),
        subagent_id=str(run.id), origin_tool_call_id="second", message="Follow up",
    )
    assert await subagent_runtime._claim_subagent(run.id) == run.id
    second, _ = await subagent_runtime._load_or_start_input(run.id)
    assert await subagent_runtime._finish_subagent_turn(
        run_id=run.id, anchor_id=second.id, reply="Follow-up answer", failed=False,
    )
    async with async_session() as db:
        refreshed = await db.get(SubagentRun, run.id)
        reply = await db.scalar(select(ChatMessage).where(
            ChatMessage.conversation_id == str(run.id), ChatMessage.role == "assistant",
            ChatMessage.message_meta["turn_anchor_id"].as_string() == str(second.id),
        ))
        assert refreshed.mode == "async" and refreshed.status == "completed"
        assert reply.content == "Follow-up answer"
        assert reply.message_meta["subagent_wake"] is True
        assert reply.message_meta["subagent_dispatch_state"] == "pending"
