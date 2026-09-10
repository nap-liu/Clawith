"""Created child references survive synchronous waits and durable history."""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services import subagent_runtime
from app.services.chat_history import (
    close_running_tool_calls_for_stop,
    expand_tool_call_row,
    persist_tool_call_row,
)
from app.services.chat_message_serializer import (
    merge_tool_call_update_for_client,
    serialize_tool_call_for_client,
)
from app.services.llm.caller import _process_tool_call
from tests.test_subagent_runtime import (
    _dispose_engine_between_tests,  # noqa: F401
    _make_context,
)

pytestmark = pytest.mark.asyncio


async def _tool_rows(parent_id):
    async with async_session() as db:
        return list((await db.scalars(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(parent_id),
                ChatMessage.role == "tool_call",
            ).order_by(ChatMessage.created_at, ChatMessage.id)
        )).all())


@pytest.mark.parametrize("terminal", ["failure", "stop", "recovery"])
async def test_sync_child_is_visible_before_result_and_survives_history(monkeypatch, terminal):
    monkeypatch.setenv("AGENT_EXECUTION_ISOLATION", "0")
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    waiting, release = asyncio.Event(), asyncio.Event()
    events, api_messages = [], []

    async def wait_for_child(run_id):
        waiting.set()
        await release.wait()
        raise subagent_runtime.SubagentError("Child failed after creation")

    async def on_tool_call(event):
        if event.get("session_ref"):
            # The event must already be committed before it reaches transport.
            rows = await _tool_rows(parent_id)
            assert json.loads(rows[-1].content)["session_ref"] == event["session_ref"]
        events.append(event)

    monkeypatch.setattr(subagent_runtime, "run_subagent_sync", wait_for_child)
    call = {"id": "created-child", "function": {
        "name": "run_subagent", "arguments": json.dumps({"task": "Inspect evidence"}),
    }}
    task = asyncio.create_task(_process_tool_call(
        tc=call, api_messages=api_messages, agent_id=agent_id, user_id=user_id,
        session_id=str(parent_id), supports_vision=False, on_tool_call=on_tool_call,
        full_reasoning_content="", allowed_tool_names={"run_subagent"},
        turn_anchor_id=anchor_id, emit_running=terminal == "recovery",
        round_id="round-child", round_tool_index=0,
        assistant_content="Inspecting evidence",
        recovery_prefix_messages=[{"role": "assistant", "content": "Starting"}],
        responses_snapshot={"id": "response-child"},
    ))
    try:
        await asyncio.wait_for(waiting.wait(), 10)
        assert not task.done()
        assert not api_messages  # No partial tool result may reach the model.
        assert all(event["status"] == "running" for event in events)
        reference = events[-1]["session_ref"]
        rows = await _tool_rows(parent_id)
        running = rows[-1]
        assert expand_tool_call_row(running) == []
        payload = json.loads(running.content)
        assert payload["round_id"] == "round-child"
        assert payload["recovery_prefix_messages"][0]["content"] == "Starting"
        assert running.message_meta["responses_snapshot"]["id"] == "response-child"
        async with async_session() as db:
            child = await db.get(ChatSession, uuid.UUID(reference["session_id"]))
            assert child is not None
            assert await db.scalar(select(ChatMessage.id).where(
                ChatMessage.conversation_id == str(child.id), ChatMessage.role == "user",
            ))

        # Recovery admission reuses the committed child for this exact call.
        existing, created = await subagent_runtime.create_subagent(
            agent_id=agent_id, execution_user_id=user_id,
            parent_session_id=str(parent_id), origin_tool_call_id="created-child",
            task="Inspect evidence", turn_anchor_id=anchor_id,
        )
        assert not created and str(existing.id) == reference["session_id"]

        if terminal == "stop":
            async with async_session() as db:
                # Recovery may emit a bare running marker before re-admission.
                await persist_tool_call_row(
                    db, agent_id=agent_id, user_id=user_id,
                    conversation_id=str(parent_id), turn_anchor_id=anchor_id,
                    evt={key: value for key, value in payload.items() if key != "session_ref"},
                )
                await db.commit()
                assert await close_running_tool_calls_for_stop(
                    db, agent_id=agent_id, conversation_id=str(parent_id),
                    turn_anchor_id=anchor_id,
                ) == 1
                await db.commit()
        elif terminal == "recovery":
            from app.services import turn_recovery

            async with async_session() as db:
                anchor = await db.get(ChatMessage, anchor_id)
                origin = await turn_recovery._load_recovery_origin(db, anchor)
                assert await turn_recovery._complete_unfinished_tool_calls(
                    db, anchor, ctx_size=100, expected_origin=origin,
                    release_db_before_execution=True,
                ) == 1
            rows = await _tool_rows(parent_id)
            assert sum(json.loads(row.content)["status"] == "done" for row in rows) == 1
            replay = [message for row in rows for message in expand_tool_call_row(row)]
            assert sum(message["role"] == "tool" for message in replay) == 1
        else:
            release.set()
            await asyncio.wait_for(task, 10)
            assert len(api_messages) == 1
            assert "Child failed" in api_messages[0].content

        final = serialize_tool_call_for_client((await _tool_rows(parent_id))[-1])
        assert final["toolStatus"] == "done"
        assert final["toolSessionRef"] == reference
        initial = serialize_tool_call_for_client(running)
        merged = merge_tool_call_update_for_client(initial, final)
        assert merged["created_at"] == initial["created_at"]
        assert merge_tool_call_update_for_client(merged, initial)["toolStatus"] == "done"
        assert merged["toolSessionRef"] == reference
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_async_creation_progress_crosses_execution_process():
    from app.services.agent_tools import execute_tool

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    references = []

    async def on_progress(reference):
        async with async_session() as db:
            assert await db.get(ChatSession, uuid.UUID(reference["session_id"])) is not None
        references.append(reference)

    result = json.loads(await execute_tool(
        "run_subagent", {"task": "Inspect evidence", "mode": "async"},
        agent_id=agent_id, user_id=user_id, session_id=str(parent_id),
        tool_call_id="async-created-child", turn_anchor_id=anchor_id,
        on_progress=on_progress,
    ))
    assert references == [{
        "session_id": result["session_id"], "execution_agent_id": str(agent_id),
    }]
    assert result["mode"] == "async"
