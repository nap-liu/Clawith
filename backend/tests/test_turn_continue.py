"""Observable continuation behavior against isolated PostgreSQL."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.chat_history import persist_assistant_reply_row
from app.services.conversation_turn_lifecycle import (
    conversation_turn_snapshot_for_session,
    transition_conversation_turn,
)
from app.services.llm.failure_outcome import make_llm_failure
from app.services.turn_continue import prepare_continue
from test_turn_recovery import _make_agent_with_model

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def dispose_engine():
    yield
    await engine.dispose()


async def failed_turn(channel="web", code="provider_request_failed", tools=True):
    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id, user_id=user_id, source_channel=channel,
            external_conv_id=f"route-{uuid.uuid4()}" if channel != "web" else None,
            title="Continue test",
        )
        db.add(session)
        await db.flush()
        anchor = ChatMessage(
            agent_id=agent_id, user_id=user_id, conversation_id=str(session.id),
            role="user", content="Read the report and finish the analysis.",
        )
        db.add(anchor)
        await db.flush()
        await transition_conversation_turn(
            db, agent_id=agent_id, conversation_id=str(session.id),
            turn_anchor_id=anchor.id, status="running",
        )
        await db.commit()
        if tools:
            db.add(ChatMessage(
                agent_id=agent_id, user_id=user_id, conversation_id=str(session.id),
                role="tool_call", content=json.dumps({
                    "name": "read_file", "args": {"path": "report.txt"},
                    "call_id": "read-1", "status": "done", "result": "Saved report contents",
                }), message_meta={"turn_anchor_id": str(anchor.id)},
            ))
            await db.commit()
        failure_id = await persist_assistant_reply_row(
            db, agent_id=agent_id, user_id=user_id, conversation_id=str(session.id),
            content=make_llm_failure(code=code, message_key="errors.providerRequestFailed"),
            turn_anchor_id=anchor.id,
        )
        await db.commit()
        return agent_id, user_id, session.id, anchor.id, failure_id


async def claim(ids):
    agent_id, user_id, session_id, *_ = ids
    async with async_session() as db:
        result = await prepare_continue(
            db, agent_id=agent_id, session_id=session_id, actor_user_id=user_id,
        )
        await db.commit()
        return result


@pytest.mark.parametrize("tools", [False, True])
async def test_continue_preserves_anchor_and_tool_results_and_completes(monkeypatch, tools):
    from app.services import turn_recovery

    ids = await failed_turn(tools=tools)
    agent_id, user_id, session_id, anchor_id, failure_id = ids
    result = await claim(ids)
    assert result["action"] == "continue_accepted"
    assert result["_continue_anchor_id"] == str(anchor_id)
    captured = []

    async def model(db, execution_agent, text, **kwargs):
        captured.append(kwargs)
        assert text == ""
        assert kwargs["continue_turn"] is True
        assert kwargs["turn_anchor_id"] == anchor_id
        return "Analysis completed."

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", model)
    monkeypatch.setattr(turn_recovery, "_deliver_recovered_reply", AsyncMock(return_value=True))
    async with async_session() as db:
        # An intervening status command must not turn into model dialogue.
        db.add(ChatMessage(
            agent_id=agent_id, user_id=user_id, conversation_id=str(session_id),
            role="assistant", content="Command status", message_meta={"artifact_role": "command_reply"},
        ))
        await db.commit()
        anchor = await db.get(ChatMessage, anchor_id)
    assert await turn_recovery.resume_turn(anchor)
    history = captured[0]["history"]
    assert history[0]["content"] == "Read the report and finish the analysis."
    assert [m["role"] for m in history] == (["user", "assistant", "tool"] if tools else ["user"])
    if tools:
        assert history[-1]["content"] == "Saved report contents"
        assert history[-1]["tool_call_id"] == "read-1"
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        snapshot = conversation_turn_snapshot_for_session(session)
        assert (snapshot.anchor_id, snapshot.generation, snapshot.status) == (anchor_id, 2, "completed")
        failure = await db.get(ChatMessage, failure_id)
        assert failure.message_meta["turn_status"] == "failed"
        assert failure.message_meta["continued_by"] == str(user_id)
        users = list((await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == str(session_id), ChatMessage.role == "user",
        ))).all())
        assert [row.id for row in users] == [anchor_id]
    assert (await claim(ids))["action"] == "continue_unavailable"


async def test_concurrent_commands_admit_once():
    ids = await failed_turn()
    results = await asyncio.gather(claim(ids), claim(ids))
    assert sorted(item["action"] for item in results) == ["continue_accepted", "continue_busy"]


async def test_continue_respects_stop_and_session_ownership():
    from app.services.turn_control import stop_session_turn_tree

    ids = await failed_turn()
    assert (await claim((uuid.uuid4(), *ids[1:])))['action'] == 'continue_unavailable'
    await claim(ids)
    await stop_session_turn_tree(agent_id=ids[0], session_id=ids[2], reason='User stop')
    assert (await claim(ids))['action'] == 'continue_unavailable'


@pytest.mark.parametrize("code", ["tool_round_limit", "tool_loop_period_2"])
async def test_continue_does_not_bypass_tool_safety_limits(code):
    assert (await claim(await failed_turn(code=code)))["action"] == "continue_unavailable"


async def test_channel_command_commits_before_dispatch_and_replays_once(monkeypatch):
    from app.services import turn_continue
    from app.services.channel_command_reply import prepare_channel_command_reply
    from app.services.channel_commands import is_channel_command

    ids = await failed_turn("dingtalk")
    agent_id, user_id, session_id, anchor_id, _ = ids
    dispatched = []

    async def dispatch(value):
        async with async_session() as other:
            session = await other.get(ChatSession, session_id)
            assert conversation_turn_snapshot_for_session(session).status == "running"
            dispatched.append(value)

    monkeypatch.setattr(turn_continue, "dispatch_continue", dispatch)
    assert is_channel_command("/CONTINUE")
    assert not is_channel_command("/continue something")
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        kwargs = dict(command="/continue", agent_id=agent_id, user_id=user_id,
                      external_user_id=None, external_conv_id=session.external_conv_id,
                      source_channel="dingtalk", provider_event_id="event-continue")
        result = await prepare_channel_command_reply(db, **kwargs)
        assert result["action"] == "continue_accepted"
        assert result["should_deliver"] is True
        replay = await prepare_channel_command_reply(db, **kwargs)
        assert replay["replayed"] is True
        assert replay["should_deliver"] is False
    assert dispatched == [str(anchor_id)]


async def test_web_command_uses_shared_claim_without_appending_a_user_turn(monkeypatch):
    from app.api import websocket_continue

    ids = await failed_turn()
    agent_id, user_id, session_id, anchor_id, _ = ids
    dispatch = AsyncMock()
    monkeypatch.setattr(websocket_continue, "dispatch_continue", dispatch)
    socket = SimpleNamespace(agent_id=agent_id, user_id=user_id, conv_id=str(session_id), lang="en",
                             _check_quotas=AsyncMock(return_value=True), _send_current_turn_event=AsyncMock())
    await websocket_continue.handle_continue(socket, {"content": "/continue", "message_id": "client-1"})
    dispatch.assert_awaited_once_with(str(anchor_id))
    assert socket._send_current_turn_event.call_args.args[0]["content"] == "Continuing the previous task."
    assert (await claim(ids))["action"] == "continue_busy"


async def test_continued_failure_can_fail_again_and_be_explicitly_continued(monkeypatch):
    from app.services import turn_recovery

    ids = await failed_turn()
    await claim(ids)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", AsyncMock(return_value=make_llm_failure(
        code="provider_request_failed", message_key="errors.providerRequestFailed",
    )))
    monkeypatch.setattr(turn_recovery, "_deliver_recovered_reply", AsyncMock(return_value=True))
    async with async_session() as db:
        anchor = await db.get(ChatMessage, ids[3])
    assert await turn_recovery.resume_turn(anchor)
    assert (await claim(ids))["action"] == "continue_accepted"


async def test_committed_continue_is_discovered_after_restart():
    from app.services.turn_recovery import _load_recoverable_anchors

    ids = await failed_turn()
    await claim(ids)
    async with async_session() as db:
        anchors = await _load_recoverable_anchors(db)
    assert ids[3] in {row.id for row in anchors}
