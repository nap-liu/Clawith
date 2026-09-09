"""Responses state survives short transactions and canonical history loading."""

import uuid

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.services.chat_history import (
    build_llm_messages_from_rows,
    persist_assistant_reply_row,
    persist_pending_confirmation_row,
    persist_tool_call_row,
)
from app.services.llm.caller_streaming_support import _assemble_api_messages_for_call_llm
from app.services.llm.client_openai_responses import OpenAIResponsesClient
from app.services.llm.client_shared import LLMMessage
from app.services.llm.responses_history import checkpoint_response
from tests.test_chat_history import _fk_bypass_session, _isolate_async_engine_between_tests  # noqa: F401
from tests.test_responses_stream import OUTPUT, response_data

pytestmark = pytest.mark.asyncio


async def test_tool_round_survives_database_reload_and_assembly():
    agent, user, conv = uuid.uuid4(), uuid.uuid4(), str(uuid.uuid4())
    client = OpenAIResponsesClient("test", model="model-a")
    response = client._parse_response_data(response_data())
    async with _fk_bypass_session() as db:
        await persist_tool_call_row(db, agent_id=agent, user_id=user, conversation_id=conv, evt={
            "name": "read_file", "call_id": "call-1", "args": {"path": "a"},
            "status": "done", "result": "file text", "round_id": "round-1", "round_tool_index": 0,
            "assistant_content": response.content, "responses_snapshot": response.responses_snapshot,
        })
        await db.commit()
    async with async_session() as db:
        rows = (await db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conv))).all()
        messages = build_llm_messages_from_rows(rows)
    api_messages = await _assemble_api_messages_for_call_llm(
        messages, agent_id=agent, supports_vision=False, static_prompt="Instructions", dynamic_prompt="",
    )
    items = client._messages_to_input(api_messages)
    assert items[1:4] == OUTPUT
    assert items[4]["type"] == "function_call_output"
    assert len(items) == 5


async def test_terminal_checkpoint_moves_once_and_is_not_replayed_from_anchor():
    agent, user, anchor = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conv = f"responses-{uuid.uuid4()}"
    client = OpenAIResponsesClient("test", model="model-a")
    native = [{**OUTPUT[1], "phase": "final_answer"}]
    response = client._parse_response_data(response_data(native))
    async with _fk_bypass_session() as db:
        db.add(ChatMessage(id=anchor, agent_id=agent, user_id=user, conversation_id=conv,
                           role="user", content="question", message_meta={"turn_status": "running"}))
        await db.commit()
    await checkpoint_response(_fk_bypass_session, agent_id=agent, conversation_id=conv,
                              anchor_id=anchor, response=response)
    async with _fk_bypass_session() as db:
        source = await db.get(ChatMessage, anchor)
        assert "responses_snapshot" not in build_llm_messages_from_rows([source])[0]
        assert source.message_meta["pending_responses_snapshot"]["output"] == native
        await persist_assistant_reply_row(db, agent_id=agent, user_id=user,
                                          conversation_id=conv, content=response.content, turn_anchor_id=anchor)
        await db.commit()
    async with async_session() as db:
        source = await db.get(ChatMessage, anchor)
        assert "pending_responses_snapshot" not in source.message_meta
        row = await db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == conv,
                                                       ChatMessage.role == "assistant"))
        assert row.message_meta["responses_snapshot"]["output"] == native
        messages = build_llm_messages_from_rows([row])
    fresh_client = OpenAIResponsesClient("test", model="model-a")
    assert fresh_client._messages_to_input([LLMMessage(**messages[0])]) == native


async def test_confirmation_preserves_original_call_id_on_resume():
    client = OpenAIResponsesClient("test", model="model-a")
    output = [{**OUTPUT[2], "name": "request_confirmation", "arguments": "{}"}]
    response = client._parse_response_data(response_data(output))
    async with _fk_bypass_session() as db:
        row_id = await persist_pending_confirmation_row(
            db, agent_id=uuid.uuid4(), user_id=uuid.uuid4(), conversation_id=str(uuid.uuid4()),
            name="request_confirmation", args={}, call_id="call-1", responses_snapshot=response.responses_snapshot,
        )
        await db.commit()
    async with async_session() as db:
        row = await db.get(ChatMessage, row_id)
        messages = build_llm_messages_from_rows([row])
    assert messages[0]["tool_calls"][0]["id"] == "call-1"
    items = client._messages_to_input([LLMMessage(**messages[0]), LLMMessage("tool", "confirmed", tool_call_id="call-1")])
    assert items[0] == output[0]
    assert items[1]["call_id"] == "call-1"


async def test_compaction_keeps_native_suffix_without_resurrecting_archived_items(monkeypatch):
    from unittest.mock import AsyncMock

    import app.services.llm.compactor as compactor
    from app.services.chat_history import load_history_for_llm
    from tests.test_chat_history_compaction import _cleanup, _precompact_model, _setup

    rows_spec = []
    for turn in range(9):
        rows_spec.extend([
            ("user", "old bulk " * 800 if turn == 0 else f"question-{turn}", 100 - turn * 2),
            ("assistant", f"answer-{turn}", 99 - turn * 2),
        ])
    rows_spec.append(("user", "current", 1))
    conv, agent, inserted, _ = await _setup(rows_spec)
    client = OpenAIResponsesClient("test", model="model-a")
    try:
        async with async_session() as db:
            for turn in range(9):
                row = await db.get(ChatMessage, inserted[turn * 2 + 1].id)
                item = {**OUTPUT[1], "id": f"message-{turn}", "phase": "final_answer",
                        "content": [{"type": "output_text", "text": f"answer-{turn}", "annotations": []}]}
                row.message_meta = {"responses_snapshot": client._parse_response_data(
                    response_data([item])).responses_snapshot}
            await db.commit()
        monkeypatch.setattr(compactor, "_summarize_via_llm", AsyncMock(return_value=(
            "## Work summary\n\n### Decisions\n" + "Older conversations were summarized safely. " * 8,
            {"completion_tokens": 100},
        )))
        result = await compactor.maybe_compact(
            agent_id=agent, conversation_id=conv, model=_precompact_model(context_window=100, keep=3),
            last_prompt_tokens=10000, current_anchor_id=inserted[-1].id,
        )
        assert result.triggered
        async with async_session() as db:
            history = await load_history_for_llm(db, agent_id=agent, conversation_id=conv, ctx_size=100)
        messages = await _assemble_api_messages_for_call_llm(
            history, agent_id=agent, supports_vision=False, static_prompt="Instructions", dynamic_prompt="",
        )
        items = client._messages_to_input(messages)
        native_ids = [item["id"] for item in items if item.get("phase") == "final_answer"]
        assert native_ids == ["message-6", "message-7", "message-8"]
    finally:
        await _cleanup(conv)


async def test_chat_completion_after_recovery_clears_stale_responses_checkpoint():
    from app.services.llm.client_shared import LLMResponse

    agent, user, anchor = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conv = f"responses-recovery-{uuid.uuid4()}"
    client = OpenAIResponsesClient("test", model="model-a")
    previous = client._parse_response_data(response_data([OUTPUT[1]]))
    async with _fk_bypass_session() as db:
        db.add(ChatMessage(id=anchor, agent_id=agent, user_id=user, conversation_id=conv,
                           role="user", content="question", message_meta={"turn_status": "running"}))
        await db.commit()
    await checkpoint_response(_fk_bypass_session, agent_id=agent, conversation_id=conv,
                              anchor_id=anchor, response=previous)
    # A new process/model attempt completes on Chat, without native Responses state.
    current = LLMResponse(content="Fresh Chat answer", model="chat-model")
    await checkpoint_response(_fk_bypass_session, agent_id=agent, conversation_id=conv,
                              anchor_id=anchor, response=current)
    async with _fk_bypass_session() as db:
        source = await db.get(ChatMessage, anchor)
        assert "pending_responses_snapshot" not in source.message_meta
        final_id = await persist_assistant_reply_row(
            db, agent_id=agent, user_id=user, conversation_id=conv,
            content=current.content, turn_anchor_id=anchor,
        )
        await db.commit()
    async with async_session() as db:
        final = await db.get(ChatMessage, final_id)
        assert final.content == "Fresh Chat answer"
        assert "responses_snapshot" not in final.message_meta
        history = build_llm_messages_from_rows([final])
    # Switching back to the original Responses model must use the actual answer.
    assert client._messages_to_input([LLMMessage(**history[0])]) == [
        {"role": "assistant", "content": "Fresh Chat answer"},
    ]
