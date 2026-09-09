"""Real isolated callers retain native Responses items through tools and reload."""

from dataclasses import replace

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.services.active_turns import active_turn_boundary
from app.services.chat_history import build_llm_messages_from_rows, persist_assistant_reply_row
from app.services.llm import call_llm_with_failover
from execution_provider_fixture import provider
from test_agent_execution_integration import isolated_engine, seed  # noqa: F401
from test_responses_stream import OUTPUT


@pytest.mark.asyncio
async def test_isolated_responses_tool_round_and_restarted_history():
    native = [OUTPUT[0], OUTPUT[1], {**OUTPUT[2], 'arguments': '{"path":"workspace/report.txt"}'}]
    final = [{**OUTPUT[1], "id": "final-1", "phase": "final_answer",
              "content": [{"type": "output_text", "text": "Report checked.", "annotations": []}]}]
    async with provider([{"_responses_output": native}, {"_responses_output": final},
                         {"_responses_output": final}]) as (url, requests):
        aid, uid, sid, anchor, selected = await seed(url)
        model = replace(selected, api_protocol="openai_responses")
        chunks = []

        async def on_chunk(text):
            chunks.append(text)

        async with active_turn_boundary():
            reply = await call_llm_with_failover(
                model, None, [{"role": "user", "content": "Read the report."}], "Assistant", "Assistant",
                agent_id=aid, user_id=uid, session_id=str(sid), turn_anchor_id=anchor,
                on_chunk=on_chunk, include_soul=False, include_memory=False,
            )
        assert reply == "Report checked."
        assert "Report checked." in "".join(chunks)
        second_items = requests[1]["input"]
        assert next(item for item in second_items if item.get("type") == "reasoning") == native[0]
        assert sum(item.get("type") == "function_call" for item in second_items) == 1
        assert any(item.get("type") == "function_call_output" and
                   "Observable report contents" in item["output"] for item in second_items)
        async with async_session() as db:
            await persist_assistant_reply_row(db, agent_id=aid, user_id=uid, conversation_id=str(sid),
                                              content=reply, turn_anchor_id=anchor)
            await db.commit()
        async with async_session() as db:
            rows = (await db.scalars(select(ChatMessage).where(
                ChatMessage.conversation_id == str(sid)).order_by(ChatMessage.created_at, ChatMessage.id))).all()
            history = build_llm_messages_from_rows(rows)
            final_row = next(row for row in rows if row.role == "assistant")
            assert final_row.message_meta["responses_snapshot"]["output"] == final
        # Each invocation creates a fresh isolated process. Only durable history
        # carries the prior reasoning and function pair into this next request.
        async with active_turn_boundary():
            await call_llm_with_failover(
                model, None, [*history, {"role": "user", "content": "Confirm the result."}],
                "Assistant", "Assistant", agent_id=aid, user_id=uid, session_id=str(sid),
                include_soul=False, include_memory=False,
            )
        replay = requests[2]["input"]
        assert next(item for item in replay if item.get("type") == "reasoning") == native[0]
        assert sum(item.get("type") == "function_call" for item in replay) == 1
        assert sum(item.get("type") == "function_call_output" for item in replay) == 1
        assert final[0] in replay
