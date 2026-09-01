"""Durability and interrupted-tail integration tests for chat history."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.services.chat_history import load_history_for_llm
from tests.test_chat_history import (
    _fk_bypass_session,
    _insert_messages_bypass_fk,
    _isolate_async_engine_between_tests,
)

pytestmark = pytest.mark.asyncio


async def test_assistant_reply_stamped_after_tool_calls():
    """Headline-fix regression guard: persist_assistant_reply uses its OWN
    session, so a reply written after the tool loop gets a created_at no earlier
    than the turn's tool_call rows — it sorts AFTER them instead of being folded
    into the web 'ran N tools' analysis card. Reverting the reply to the
    channel's long-lived request transaction (PostgreSQL now() = txn-start time)
    would stamp an earlier created_at and fail this. Compares timestamps
    directly, so it is independent of any sort tiebreak and never flaky (>=)."""
    from sqlalchemy import select as _select

    from app.services.chat_history import persist_assistant_reply, persist_tool_call

    agent_id = uuid.uuid4()
    conv_id = f"test_order_{uuid.uuid4().hex[:8]}"
    await persist_tool_call(
        _fk_bypass_session,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        conversation_id=conv_id,
        evt={"name": "get_weather", "args": {"city": "SH"}, "status": "done", "result": "sunny"},
    )
    await persist_assistant_reply(
        _fk_bypass_session,
        agent_id=agent_id,
        user_id=uuid.uuid4(),
        conversation_id=conv_id,
        content="今天上海晴",
    )
    async with async_session() as db:
        rows = (
            await db.execute(_select(ChatMessage).where(ChatMessage.conversation_id == conv_id))
        ).scalars().all()
    by_role = {r.role: r for r in rows}
    assert set(by_role) == {"tool_call", "assistant"}
    assert by_role["assistant"].created_at >= by_role["tool_call"].created_at


async def test_load_history_for_llm_drops_cancelled_turn_tail():
    """A /stop can leave the interrupted turn as user + tool_call rows with no
    final assistant reply. That tail must not be replayed before the next user
    message, otherwise strict providers can reject the role sequence."""
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conv_id = f"test_cancelled_tail_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk(
        [
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "user",
                "content": "上一轮问题",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=40),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "assistant",
                "content": "上一轮回复",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=30),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "user",
                "content": "被 /stop 中断的问题",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=20),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "tool_call",
                "content": json.dumps(
                    {
                        "name": "web_search",
                        "args": {"q": "x"},
                        "status": "done",
                        "result": "partial",
                    },
                    ensure_ascii=False,
                ),
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=10),
            },
        ]
    )

    async with async_session() as db:
        history = await load_history_for_llm(
            db,
            agent_id=agent_id,
            conversation_id=conv_id,
            ctx_size=50,
        )

    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "上一轮问题"),
        ("assistant", "上一轮回复"),
    ]

    async with async_session() as db:
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
                .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            )
        ).scalars().all()
    assert [(row.role, row.content) for row in rows] == [
        ("user", "上一轮问题"),
        ("assistant", "上一轮回复"),
        ("user", "被 /stop 中断的问题"),
        (
            "tool_call",
            json.dumps(
                {
                    "name": "web_search",
                    "args": {"q": "x"},
                    "status": "done",
                    "result": "partial",
                },
                ensure_ascii=False,
            ),
        ),
    ]


async def test_load_history_for_llm_keeps_resolved_confirmation_turn_for_resume():
    """A resolved confirmation is not a cancelled tool tail.

    Clicking the card completes its normalized tool result and immediately resumes
    the same turn, so the LLM must receive the original user message plus the
    assistant/tool pair even though no final assistant prose exists yet.
    """
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conv_id = f"test_confirmation_resume_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    await _insert_messages_bypass_fk(
        [
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "user",
                "content": "请执行需要确认的操作",
                "conversation_id": conv_id,
                "created_at": now - timedelta(seconds=10),
            },
            {
                "id": uuid.uuid4(),
                "agent_id": agent_id,
                "user_id": user_id,
                "role": "tool_call",
                "content": json.dumps(
                    {
                        "name": "request_confirmation",
                        "args": {"title": "确认操作", "summary": "继续执行"},
                        "status": "done",
                        "result": "用户点击了「确认」",
                    },
                    ensure_ascii=False,
                ),
                "conversation_id": conv_id,
                "created_at": now,
            },
        ]
    )

    async with async_session() as db:
        history = await load_history_for_llm(
            db,
            agent_id=agent_id,
            conversation_id=conv_id,
            ctx_size=50,
        )

    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "tool",
    ]
    assert history[0]["content"] == "请执行需要确认的操作"
    assert history[1]["tool_calls"][0]["function"]["name"] == "request_confirmation"
    assert history[2]["content"] == "用户点击了「确认」"
