"""Consumed backlog is current model input, even when it arrived before the root."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import uuid

import pytest

from app.database import async_session, engine
from app.models.audit import ChatMessage
from app.services.chat_history import (
    load_messages_for_session, load_recoverable_messages_for_turn,
    persist_intermediate_assistant_reply,
)
from app.services.llm.compactor_runtime import _load_active_rows
from app.services.llm.turn_partition import partition_turns
from app.services.turn_inbox import drain_turn_inbox
from test_turn_inbox_receipt_anchor import _seed_running_im_turn


@pytest.fixture(autouse=True)
async def _dispose_connections():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_without_time", [False, True])
async def test_early_backlog_replays_after_root_and_cannot_be_compacted(legacy_without_time):
    agent_id, user_id, session_id, root_id, generation = await _seed_running_im_turn()
    async with async_session() as db:
        root = await db.get(ChatMessage, root_id)
        arrival = root.created_at - timedelta(minutes=5)
        backlog = ChatMessage(
            agent_id=agent_id, user_id=user_id, conversation_id=str(session_id),
            role="user", content="old pending correction", created_at=arrival,
            message_meta={"turn_inbox_state": "pending", "turn_inbox_mode": "next_turn"},
        )
        db.add(backlog)
        await db.commit()
        backlog_id = backlog.id

    # Keep a reader alive across the drain to verify fresh compaction reads.
    async with async_session() as reader:
        cached = await reader.get(ChatMessage, backlog_id)
        before = await _load_active_rows(reader, agent_id=agent_id, conversation_id=str(session_id))
        assert backlog_id not in [row.id for row in before]
        await reader.commit()
        intermediate_ids = []

        async def persist_before_injection(*, created_at):
            intermediate_ids.append(await persist_intermediate_assistant_reply(
                async_session, agent_id=agent_id, user_id=user_id,
                conversation_id=str(session_id), turn_anchor_id=root_id,
                content="work before correction", created_at=created_at,
            ))

        assert await drain_turn_inbox(
            session_id=str(session_id), execution_agent_id=agent_id,
            execution_user_id=user_id, active_turn_anchor_id=root_id,
            before_injection=persist_before_injection,
        ) == [{"role": "user", "content": "old pending correction"}]
        if legacy_without_time:
            async with async_session() as db:
                row = await db.get(ChatMessage, backlog_id)
                row.message_meta = {k: v for k, v in row.message_meta.items()
                                    if k != "turn_inbox_consumed_at"}
                await db.commit()
        after = await _load_active_rows(reader, agent_id=agent_id, conversation_id=str(session_id))
        assert cached.message_meta["turn_inbox_state"] == "delivered"
        assert cached.created_at == arrival
        assert cached.message_meta["turn_inbox_generation"] == generation
        partition = partition_turns(
            after, current_anchor_id=str(root_id), keep_recent_turns=0,
            minimum_protected_turns=0,
        )
        assert partition.compactable_rows == []
        assert set(row.id for row in partition.current.rows) == {root_id, backlog_id, *intermediate_ids}
        expected = ([root_id, backlog_id, *intermediate_ids] if legacy_without_time
                    else [root_id, *intermediate_ids, backlog_id])
        assert [row.id for row in after] == expected
        for loader in (load_messages_for_session, load_recoverable_messages_for_turn):
            kwargs = {"turn_anchor_id": root_id} if loader is load_recoverable_messages_for_turn else {}
            rows = await loader(reader, agent_id=agent_id, conversation_id=str(session_id), ctx_size=1, **kwargs)
            assert [row.id for row in rows] == expected


@pytest.mark.asyncio
async def test_inbox_consumption_keeps_prior_subagent_input_before_it():
    from app.services.message_context_order import order_messages_for_context

    now = datetime.now(UTC)
    root = SimpleNamespace(id=uuid.uuid4(), role="user", created_at=now, message_meta={})
    subagent = SimpleNamespace(
        id=uuid.uuid4(), role="user", created_at=now + timedelta(seconds=2),
        message_meta={"subagent_turn_anchor_id": str(root.id)},
    )
    inbox = SimpleNamespace(
        id=uuid.uuid4(), role="user", created_at=now + timedelta(seconds=1),
        message_meta={
            "turn_inbox_state": "delivered", "turn_inbox_anchor_id": str(root.id),
            "turn_inbox_consumed_at": (now + timedelta(seconds=3)).isoformat(),
        },
    )
    projected = order_messages_for_context([root, inbox, subagent])
    assert projected == [root, subagent, inbox]
    assert order_messages_for_context(projected) == projected
    partition = partition_turns(projected, current_anchor_id=str(root.id))
    assert list(partition.current.rows) == projected
