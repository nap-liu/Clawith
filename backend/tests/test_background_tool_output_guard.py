"""Durable background entry shares measured usage, tool shaping and confirmations."""

import json

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.activity_log import DailyTokenUsage
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.services.active_turns import active_turn_boundary
from app.services.agent_runtime_workspace import standard_agent_runtime_workspace
from app.services.llm.caller import call_agent_llm_with_tools
from app.services.llm.tool_output_store import PERSISTED_OPEN
from execution_provider_fixture import provider
from test_agent_execution_integration import isolated_engine, seed  # noqa: F401

pytestmark = pytest.mark.asyncio


def tool(name, arguments, call_id="tool-1"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


async def background(aid, uid, sid, prompt="Process the report"):
    async with active_turn_boundary(), async_session() as db:
        result = await call_agent_llm_with_tools(
            db, aid, "Assistant", prompt, session_id=str(sid), execution_user_id=uid,
            max_rounds=3,
        )
        assert not db.in_transaction()
    return result


async def rows(aid):
    async with async_session() as db:
        return list(await db.scalars(select(ChatMessage).where(
            ChatMessage.agent_id == aid).order_by(ChatMessage.created_at)))


async def test_background_context_uses_provider_usage_not_local_text_size():
    async with provider([{"content": "provider accepted"}]) as (url, requests):
        aid, uid, sid, _, snapshot = await seed(url, "trigger")
        async with async_session() as db:
            model = await db.get(LLMModel, snapshot.id)
            model.context_window = 1000
            model.max_output_tokens = 100
            await db.commit()
        assert await background(aid, uid, sid, "数" * 1000) == "provider accepted"
        assert len(requests) == 1
        assert "数" * 1000 in str(requests[0]["messages"])
        async with async_session() as db:
            usage = await db.scalar(select(DailyTokenUsage).where(DailyTokenUsage.agent_id == aid))
            assert (usage.input_tokens, usage.output_tokens, usage.tokens_used) == (100, 10, 110)
            assert usage.estimated_tokens == 0
            sessions = list(await db.scalars(select(ChatSession).where(ChatSession.agent_id == aid)))
            assert all(session.context_terminated_reason is None for session in sessions)


async def test_large_read_file_result_is_materialized_before_second_model_round():
    read = tool("read_file", {"path": "workspace/report.txt"})
    async with provider([{"content": "Reading", "tool_calls": [read]},
                         {"content": "done"}]) as (url, requests):
        aid, uid, sid, _, _ = await seed(url, "trigger")
        workspace = standard_agent_runtime_workspace(aid)
        huge = "x" * 200000
        workspace.local_path("workspace/report.txt").write_text(huge)
        assert await background(aid, uid, sid) == "done"
        assert len(requests) == 2
        tool_message = next(msg for msg in requests[1]["messages"] if msg["role"] == "tool")
        view = tool_message["content"]
        assert PERSISTED_OPEN in view and len(view) < len(huge) and huge not in view
        saved = view.split("Full output saved to:", 1)[1].split()[0]
        assert huge in workspace.local_path(saved).read_text()
        stored = [json.loads(row.content) for row in await rows(aid) if row.role == "tool_call"]
        assert any(row.get("result") == view for row in stored)


async def test_background_tool_round_content_becomes_confirmation_intro():
    read = tool("read_file", {"path": "workspace/report.txt"}, "read-1")
    confirm = tool("request_confirmation", {"title": "Confirm", "summary": "Continue?"}, "confirm-1")
    async with provider([{"content": "background answer", "tool_calls": [read]},
                         {"tool_calls": [confirm]}]) as (url, requests):
        aid, uid, sid, _, _ = await seed(url, "trigger")
        assert await background(aid, uid, sid) == ""
        assert len(requests) == 2
        stored = await rows(aid)
        pending = [row for row in stored if row.role == "tool_call"
                   and json.loads(row.content).get("name") == "request_confirmation"]
        assert len(pending) == 1
        assert json.loads(pending[0].content)["status"] == "pending"
        assert any(row.role == "assistant" and row.content == "background answer" for row in stored)
        async with async_session() as db:
            anchor = await db.get(ChatMessage, pending[0].message_meta["turn_anchor_id"])
            assert anchor.message_meta["turn_status"] == "suspended"


async def test_background_confirmation_round_id_is_unique_per_execution():
    confirm = tool("request_confirmation", {"title": "Confirm", "summary": "Continue?"})
    async with provider([{"content": "confirm this run", "tool_calls": [confirm]}]) as (url, requests):
        aid, uid, sid, _, _ = await seed(url, "trigger")
        for _ in range(2):
            assert await background(aid, uid, sid) == ""
        assert len(requests) == 2
        pending = [row for row in await rows(aid) if row.role == "tool_call"
                   and json.loads(row.content).get("name") == "request_confirmation"]
        assert len(pending) == 2
        assert len({json.loads(row.content)["round_id"] for row in pending}) == 2
        assert len({row.message_meta["turn_anchor_id"] for row in pending}) == 2
        assert len({row.conversation_id for row in pending}) == 2
        assert all(json.loads(row.content)["status"] == "pending" for row in pending)
