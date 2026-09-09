"""Actual shared callers, PostgreSQL, Redis, tools and HTTP across processes."""

import json
import uuid

import pytest
from sqlalchemy import select

import app.models.registry  # noqa: F401
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.tool import AgentTool, Tool
from app.services.active_turns import active_turn_boundary
from app.services.conversation_turn_lifecycle import transition_conversation_turn
from app.services.llm.runtime_model import RuntimeLLMModel
from app.services.tool_seeder import seed_builtin_tools
from test_im_scene_command_integration import _seed_scene_runtime
from execution_provider_fixture import provider


@pytest.fixture(autouse=True)
async def isolated_engine(monkeypatch):
    monkeypatch.setenv("AGENT_EXECUTION_ISOLATION", "1")
    await engine.dispose()
    yield
    await engine.dispose()


async def seed(base_url, channel="web"):
    await seed_builtin_tools()
    aid, uid = await _seed_scene_runtime()
    async with async_session() as db:
        agent = await db.get(Agent, aid)
        model = LLMModel(tenant_id=agent.tenant_id, provider="openai", model="test-model",
                         api_key_encrypted="test-key", base_url=base_url, label="Test model",
                         request_timeout=5, context_window=100000, max_output_tokens=1000)
        db.add(model)
        await db.flush()
        agent.primary_model_id = model.id
        session = ChatSession(agent_id=aid, user_id=uid, source_channel=channel, title="Isolation")
        db.add(session)
        await db.flush()
        anchor = ChatMessage(agent_id=aid, user_id=uid, conversation_id=str(session.id),
                             role="user", content="Read the report.")
        db.add(anchor)
        await db.flush()
        await transition_conversation_turn(db, agent_id=aid, conversation_id=str(session.id),
                                           turn_anchor_id=anchor.id, status="running")
        for name in ("read_file", "search_files"):
            tool = await db.scalar(select(Tool).where(Tool.name == name))
            db.add(AgentTool(agent_id=aid, tool_id=tool.id, enabled=True))
        await db.commit()
        snapshot = RuntimeLLMModel.from_orm(model)
    from app.services.agent_runtime_workspace import standard_agent_runtime_workspace

    path = standard_agent_runtime_workspace(aid).local_path("workspace/report.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Observable report contents", encoding="utf-8")
    return aid, uid, session.id, anchor.id, snapshot


@pytest.mark.parametrize("channel", ["web", "dingtalk"])
async def test_streaming_tool_round_and_audit_are_unchanged(channel):
    from app.services.channel_llm import _call_agent_llm
    from app.services.llm import call_llm_with_failover

    tool_call = {"id": "read-report", "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": "workspace/report.txt"})}}
    async with provider([{"content": "Checking the report.", "tool_calls": [tool_call]},
                         {"content": "Report checked."}]) as (url, requests):
        aid, uid, sid, anchor, model = await seed(url, channel)
        chunks = []

        async def on_chunk(value):
            chunks.append(value)

        async with active_turn_boundary():
            if channel == "web":
                reply = await call_llm_with_failover(model, None, [{"role": "user", "content": "Read the report."}],
                    "Assistant", "Assistant", agent_id=aid, user_id=uid, session_id=str(sid),
                    turn_anchor_id=anchor, on_chunk=on_chunk, include_soul=False, include_memory=False)
            else:
                async with async_session() as db:
                    reply = await _call_agent_llm(db, aid, "Read the report.", session_id=str(sid),
                        user_id=uid, turn_anchor_id=anchor, on_chunk=on_chunk, broadcast_web=False,
                        include_soul=False, include_memory=False)
        assert reply == "Report checked."
        assert "Report checked." in "".join(chunks)
        assert len(requests) == 2
        tool_results = [m for m in requests[1]["messages"] if m["role"] == "tool"]
        assert len(tool_results) == 1
        assert "Observable report contents" in tool_results[0]["content"]
        async with async_session() as db:
            rows = (await db.scalars(select(ChatMessage).where(
                ChatMessage.agent_id == aid, ChatMessage.conversation_id == str(sid),
                ChatMessage.role == "tool_call"))).all()
        events = [json.loads(row.content) for row in rows]
        assert any(e["status"] == "done" and "Observable report contents" in e["result"] for e in events)


async def test_background_entry_uses_real_provider_and_own_database_session():
    from app.services.llm import call_agent_llm_with_tools

    async with provider([{"content": "Scheduled work complete."}]) as (url, requests):
        aid, uid, sid, _, _ = await seed(url, "trigger")
        async with active_turn_boundary(), async_session() as db:
            reply = await call_agent_llm_with_tools(db, aid, "Assistant", "Run scheduled work",
                session_id=str(sid), execution_user_id=uid, max_rounds=2)
            assert not db.in_transaction()
        assert reply == "Scheduled work complete."
        assert len(requests) == 1


async def test_nested_agents_share_session_lease_without_deadlock():
    import os
    from tests.execution_process_fixtures import conversation

    process_ids = []

    async def callback(pid):
        process_ids.append(pid)

    result = await conversation(str(uuid.uuid4()), str(uuid.uuid4()), callback,
                                peer_id=str(uuid.uuid4()))
    assert result == "complete"
    assert len(process_ids) == 2
    assert len(set(process_ids)) == 1
    assert os.getpid() not in process_ids


async def test_channel_file_callback_keeps_durable_receipt_and_single_send():
    from app.services import agent_tools
    from app.services.im_delivery import IMDeliveryPart, IMDeliveryResult
    from app.services.llm import call_llm_with_failover

    tool_call = {"id": "send-report", "type": "function", "function": {
        "name": "send_channel_file", "arguments": json.dumps({"file_path": "workspace/report.txt"})}}
    async with provider([{"tool_calls": [tool_call]}, {"content": "File delivered."}]) as (url, requests):
        aid, uid, sid, anchor, model = await seed(url)
        sends = []

        async def sender(path, message):
            sends.append(path.read_text())
            async with async_session() as db:
                rows = (await db.scalars(select(ChatMessage).where(
                    ChatMessage.agent_id == aid, ChatMessage.role == "tool_call"))).all()
                assert any(row.message_meta.get("delivery", {}).get("status") == "pending" for row in rows)
            part = IMDeliveryPart(transport="slack_file", provider_message_id="file-1",
                                  conversation_ref="test-conversation", recallable=False)
            await agent_tools.channel_file_part_recorder.get()(part)
            return IMDeliveryResult.sent("slack", part)

        token = agent_tools.channel_file_sender.set(sender)
        try:
            async with active_turn_boundary():
                result = await call_llm_with_failover(model, None, [{"role": "user", "content": "Send the report."}],
                    "Assistant", "Assistant", agent_id=aid, user_id=uid, session_id=str(sid),
                    turn_anchor_id=anchor, include_soul=False, include_memory=False)
        finally:
            agent_tools.channel_file_sender.reset(token)
        assert result == "File delivered."
        assert sends == ["Observable report contents"]
        async with async_session() as db:
            rows = (await db.scalars(select(ChatMessage).where(
                ChatMessage.agent_id == aid, ChatMessage.role == "tool_call"))).all()
        receipts = [row.message_meta["delivery"] for row in rows if row.message_meta.get("delivery")]
        assert len(receipts) == 1
        assert receipts[0]["status"] == "sent"
