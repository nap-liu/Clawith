"""Durable merge coverage for concurrent native Gateway messages."""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.api import gateway as gateway_api
from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.gateway_message import GatewayMessage
from gateway_workload_capacity_support import (
    _dispose_engine_between_cases,
    _native_background_args,
    _seed_native_pair,
)

@pytest.mark.asyncio
async def test_concurrent_native_gateway_messages_merge_into_one_durable_turn(
    monkeypatch,
) -> None:
    _api_key, target_id, _tenant_id, source_id = await _seed_native_pair()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    provider_calls = 0
    injected: list[dict] = []

    async def fake_call_llm(**kwargs):
        nonlocal provider_calls
        provider_calls += 1
        first_started.set()
        await release_first.wait()
        injected.extend(await kwargs["before_round"](0))
        return "one merged gateway reply"

    monkeypatch.setattr("app.services.llm.call_llm", fake_call_llm)
    first_args = await _native_background_args(
        source_id,
        target_id,
        content="first gateway message",
        source_event_id=f"gateway-first-{uuid.uuid4()}",
    )
    second_args = await _native_background_args(
        source_id,
        target_id,
        content="second gateway message",
        source_event_id=f"gateway-second-{uuid.uuid4()}",
    )

    first = asyncio.create_task(gateway_api._send_to_agent_background(*first_args))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second = asyncio.create_task(gateway_api._send_to_agent_background(*second_args))
    await asyncio.wait_for(second, timeout=1)
    release_first.set()
    await asyncio.wait_for(first, timeout=1)

    assert provider_calls == 1
    assert len(injected) == 1
    assert "second gateway message" in injected[0]["content"]
    async with async_session() as db:
        session = await db.scalar(
            select(ChatSession).where(
                ChatSession.source_channel == "agent",
                ChatSession.agent_id.in_([source_id, target_id]),
                ChatSession.peer_agent_id.in_([source_id, target_id]),
            )
        )
        assert session is not None
        rows = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(session.id))
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )
        gateway_replies = list(
            (
                await db.execute(
                    select(GatewayMessage).where(
                        GatewayMessage.agent_id == source_id,
                        GatewayMessage.sender_agent_id == target_id,
                    )
                )
            ).scalars()
        )
    assert len([row for row in rows if row.role == "user"]) == 2
    assert rows[1].message_meta["turn_inbox_state"] == "delivered"
    assert [row.content for row in rows if row.role == "assistant"] == [
        "one merged gateway reply"
    ]
    assert [row.content for row in gateway_replies] == ["one merged gateway reply"]

