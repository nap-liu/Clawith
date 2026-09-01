"""A2A tool turns keep execution and durable conversation identities separate."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.chat_history import persist_assistant_reply_row
from app.services.conversation_turn_lifecycle import (
    conversation_turn_snapshot_for_session,
    transition_conversation_turn,
)
from app.services.llm import caller
from app.services.llm.caller import call_llm
from app.services.llm.tool_output_store import ToolOutputRewrite
from tests.test_llm_throttle_retry import (
    _FakeModel,
    _patch_call_llm_collaborators,
    _stop_response,
    _ThrottleScriptClient,
    _tool_response,
)
from tests.test_turn_recovery import _make_agent_with_model

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _clean_messages_and_dispose_engine():
    async with async_session() as db:
        await db.execute(delete(ChatMessage))
        await db.commit()
    yield
    await engine.dispose()


async def test_a2a_tool_turn_persists_under_session_owner_and_executes_as_target(monkeypatch):
    session_agent_id, owner_user_id = await _make_agent_with_model()
    target_agent_id = uuid.UUID(int=session_agent_id.int + 1)
    assert session_agent_id == min(session_agent_id, target_agent_id, key=str)
    async with async_session() as db:
        session_agent = await db.get(Agent, session_agent_id)
        target = Agent(
            id=target_agent_id,
            name=f"Target_{uuid.uuid4().hex[:8]}",
            creator_id=owner_user_id,
            tenant_id=session_agent.tenant_id,
            primary_model_id=session_agent.primary_model_id,
            status="idle",
        )
        db.add(target)
        await db.flush()

        session = ChatSession(
            agent_id=session_agent_id,
            peer_agent_id=target_agent_id,
            source_channel="agent",
            external_conv_id=f"a2a-test:{uuid.uuid4()}",
            title="A2A identity test",
        )
        db.add(session)
        await db.flush()
        session_id = str(session.id)

        anchor = ChatMessage(
            agent_id=session_agent_id,
            user_id=owner_user_id,
            sender_agent_id=session_agent_id,
            role="user",
            content="read a file",
            conversation_id=session_id,
            message_meta={"execution_agent_id": str(target_agent_id)},
        )
        db.add(anchor)
        await db.flush()
        anchor_id = anchor.id
        await transition_conversation_turn(
            db,
            agent_id=session_agent_id,
            conversation_id=session_id,
            turn_anchor_id=anchor_id,
            status="running",
        )
        await db.commit()

    client = _ThrottleScriptClient(
        [
            _tool_response(
                {
                    "id": "call_a2a_read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path": "notes.txt"}',
                    },
                }
            ),
            _stop_response("target reply"),
        ]
    )
    _patch_call_llm_collaborators(monkeypatch, client)
    monkeypatch.setattr(
        caller,
        "get_agent_tools_for_llm",
        AsyncMock(
            return_value=[
                {
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {}},
                }
            ]
        ),
    )

    execution_agent_ids: list[uuid.UUID] = []

    async def fake_execute_tool(_name, _args, **kwargs):
        execution_agent_ids.append(uuid.UUID(str(kwargs["agent_id"])))
        return "original tool result"

    monkeypatch.setattr(caller, "execute_tool", fake_execute_tool)
    monkeypatch.setattr(
        caller,
        "enforce_message_budget",
        AsyncMock(
            return_value=[
                ToolOutputRewrite(
                    tool_call_id="call_a2a_read",
                    final_content="rewritten tool result",
                )
            ]
        ),
    )

    reply = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "read a file"}],
        agent_name="Target",
        role_description="",
        agent_id=target_agent_id,
        user_id=owner_user_id,
        session_id=session_id,
        turn_anchor_id=anchor_id,
        turn_anchor_agent_id=session_agent_id,
    )
    assert reply == "target reply"
    assert execution_agent_ids == [target_agent_id]

    async with async_session() as db:
        await persist_assistant_reply_row(
            db,
            agent_id=session_agent_id,
            user_id=owner_user_id,
            conversation_id=session_id,
            content=reply,
            turn_anchor_id=anchor_id,
            sender_agent_id=target_agent_id,
        )
        await db.commit()

    async with async_session() as db:
        tool_rows = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == session_id,
                        ChatMessage.role == "tool_call",
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )
        assistant = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == session_id,
                    ChatMessage.role == "assistant",
                )
            )
        ).scalar_one()
        stored_session = await db.get(ChatSession, uuid.UUID(session_id))

    assert [json.loads(row.content)["status"] for row in tool_rows] == ["running", "done"]
    assert all(row.agent_id == session_agent_id for row in tool_rows)
    assert not any(row.agent_id == target_agent_id for row in tool_rows)
    assert json.loads(tool_rows[-1].content)["result"] == "rewritten tool result"
    assert assistant.agent_id == session_agent_id
    assert assistant.sender_agent_id == target_agent_id
    assert conversation_turn_snapshot_for_session(stored_session).status == "completed"
