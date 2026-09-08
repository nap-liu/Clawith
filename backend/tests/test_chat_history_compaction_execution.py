"""Compaction execution and preflight integration tests."""

from __future__ import annotations

import uuid

import pytest

from app.database import async_session
from app.models.audit import ChatMessage
from app.services.chat_history import load_history_for_llm
from app.services.session_token_usage import (
    SESSION_CONTEXT_META_KEY,
    persist_round_context_usage,
)
from tests.test_chat_history_compaction import (
    _cleanup,
    _isolate_async_engine_between_tests,  # noqa: F401 - register the autouse fixture
    _precompact_model,
    _setup,
)

pytestmark = pytest.mark.asyncio


async def test_compaction_never_marks_recent_three_or_current_turn(monkeypatch):
    from sqlalchemy import select as _select
    from unittest.mock import AsyncMock

    import app.services.llm.compactor as compactor

    rows_spec = []
    age = 20_000
    for turn in range(9):
        body = ("old bulk " * 800) if turn == 0 else f"turn-{turn}"
        rows_spec.append(("user", body, age))
        age -= 1
        rows_spec.append(("assistant", f"answer-{turn}", age))
        age -= 1
    rows_spec.append(("user", "current", age))

    conv_id, agent_id, inserted, _ = await _setup(rows_spec)
    current_anchor = inserted[-1].id
    await persist_round_context_usage(
        agent_id=agent_id,
        session_id=conv_id,
        turn_anchor_id=current_anchor,
        input_tokens=10_000,
        output_tokens=10,
        provider="qwen",
        model="qwen-test",
    )
    summary = (
        "## Work summary\n\n"
        "### Decisions\n"
        + ("The oldest completed turn was condensed safely. " * 8)
    )
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(summary, {"completion_tokens": 100})),
    )

    try:
        result = await compactor.maybe_compact(
            agent_id=agent_id,
            conversation_id=conv_id,
            model=_precompact_model(context_window=100, keep=2),
            last_prompt_tokens=10_000,
            current_anchor_id=current_anchor,
        )
        assert result.triggered is True

        async with async_session() as db:
            rows = (
                await db.execute(
                    _select(ChatMessage)
                    .where(ChatMessage.conversation_id == conv_id)
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            ).scalars().all()

        marker_id = rows[0].compacted_into
        assert marker_id is not None
        assert all(row.compacted_into == marker_id for row in rows[:12])
        assert all(row.compacted_into is None for row in rows[12:])
        assert [row.content for row in rows[12:]] == [row.content for row in inserted[12:]]
        current = next(row for row in rows if row.id == current_anchor)
        observation = current.message_meta[SESSION_CONTEXT_META_KEY]
        assert observation["last_input_tokens"] == 0
        assert observation["consumed_by_compaction_epoch"] == 1
    finally:
        await _cleanup(conv_id)


async def test_large_tool_heavy_history_replay_compacts_and_keeps_three_raw_turns(monkeypatch):
    """Production-shaped replay: many tool loops shrink to one stable summary.

    The current turn and the latest three completed user turns remain byte-for-
    byte raw, regardless of how many tool calls each protected turn contains.
    """
    import json as _json
    from sqlalchemy import select as _select
    from unittest.mock import AsyncMock

    import app.services.llm.compactor as compactor

    rows_spec = []
    age = 100_000
    for turn in range(30):
        rows_spec.append(("user", f"目标-{turn}: " + "历史上下文" * 400, age))
        age -= 1
        for tool_index in range(5):
            rows_spec.append((
                "tool_call",
                _json.dumps({
                    "name": "read_file",
                    "args": {"path": f"workspace/{turn}-{tool_index}.md"},
                    "status": "done",
                    "result": "bounded tool result",
                }),
                age,
            ))
            age -= 1
        rows_spec.append(("assistant", f"完成阶段-{turn}", age))
        age -= 1
    rows_spec.append(("user", "当前目标：继续推进，不得丢失", age))

    conv_id, agent_id, inserted, _ = await _setup(rows_spec)
    current_anchor = inserted[-1].id
    valid_summary = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and progress\n"
        "- Continue the active objective using the protected recent turns.\n\n"
        "### Key facts\n"
        "- Earlier completed stages and tool evidence were compacted.\n"
        + ("- Historical progress remains available through this summary.\n" * 5)
        + "\n### Open items\n"
        "- Continue from the current uncompacted user request.\n"
    )
    summarize = AsyncMock(return_value=(valid_summary, {"completion_tokens": 120}))
    monkeypatch.setattr(compactor, "_summarize_via_llm", summarize)

    try:
        result = await compactor.maybe_compact(
            agent_id=agent_id,
            conversation_id=conv_id,
            model=_precompact_model(context_window=20_000, keep=3),
            last_prompt_tokens=50_000,
            current_anchor_id=current_anchor,
        )
        assert result.triggered is True
        # The identifier-heavy model draft exceeds the unified conservative
        # budget, so one bounded repair is attempted before lossless fallback.
        assert summarize.await_count == 2

        async with async_session() as db:
            rows = (
                await db.execute(
                    _select(ChatMessage)
                    .where(ChatMessage.conversation_id == conv_id)
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            ).scalars().all()
            history = await load_history_for_llm(
                db,
                agent_id=agent_id,
                conversation_id=conv_id,
                ctx_size=1000,
            )

        compacted_count = 27 * 7
        marker_id = rows[0].compacted_into
        assert marker_id is not None
        assert all(row.compacted_into == marker_id for row in rows[:compacted_count])
        assert all(row.compacted_into is None for row in rows[compacted_count:])
        assert [row.content for row in rows[compacted_count:]] == [
            row.content for row in inserted[compacted_count:]
        ]
        assert "<conversation-summary" in history[0]["content"]
        # The generic history loader intentionally omits an incomplete current
        # user tail; the caller reattaches its durable anchor separately. The
        # database row itself must remain raw and unmarked for that reassembly.
        assert rows[-1].content == "当前目标：继续推进，不得丢失"
        assert rows[-1].compacted_into is None
        assert history[-1]["content"] == "完成阶段-29"
    finally:
        await _cleanup(conv_id)


async def test_consumed_onmessage_event_does_not_block_compaction(monkeypatch):
    from sqlalchemy import select as _select
    from unittest.mock import AsyncMock

    import app.services.llm.compactor as compactor

    rows_spec = [("user", "consumed event", 30_000)]
    age = 20_000
    for turn in range(9):
        body = ("old bulk " * 800) if turn == 0 else f"turn-{turn}"
        rows_spec.extend(
            [
                ("user", body, age),
                ("assistant", f"answer-{turn}", age - 1),
            ]
        )
        age -= 2
    rows_spec.append(("user", "current", age))

    conv_id, agent_id, inserted, _ = await _setup(rows_spec)
    current_anchor = inserted[-1].id
    async with async_session() as db:
        consumed = await db.get(ChatMessage, inserted[0].id)
        consumed.message_meta = {"consumed_by_onmessage": True}
        await db.commit()

    summary = (
        "## Work summary\n\n"
        "### Decisions\n"
        + ("The oldest completed turn was condensed safely. " * 8)
    )
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(summary, {"completion_tokens": 100})),
    )

    try:
        result = await compactor.maybe_compact(
            agent_id=agent_id,
            conversation_id=conv_id,
            model=_precompact_model(context_window=100, keep=8),
            last_prompt_tokens=10_000,
            current_anchor_id=current_anchor,
        )
        assert result.triggered is True

        async with async_session() as db:
            rows = (
                await db.execute(
                    _select(ChatMessage)
                    .where(ChatMessage.conversation_id == conv_id)
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            ).scalars().all()

        assert rows[0].compacted_into is None
        assert rows[1].compacted_into is not None
        assert rows[2].compacted_into == rows[1].compacted_into
        assert all(row.compacted_into is None for row in rows[3:])
    finally:
        await _cleanup(conv_id)


async def test_concurrent_compaction_rechecks_persisted_state_after_lock(monkeypatch):
    """A waiter reloads when the lock holder already changed compaction state."""
    from unittest.mock import AsyncMock

    import app.services.llm.compactor as compactor

    class _HeldLock:
        def locked(self):
            return True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    marker_id = uuid.uuid4()
    monkeypatch.setattr(compactor, "_get_session_lock", AsyncMock(return_value=_HeldLock()))
    monkeypatch.setattr(
        compactor,
        "_load_compaction_state",
        AsyncMock(side_effect=[(12, None), (5, marker_id)]),
    )
    do_compact = AsyncMock()
    monkeypatch.setattr(compactor, "_do_compact", do_compact)

    result = await compactor.maybe_compact(
        agent_id=uuid.uuid4(),
        conversation_id=str(uuid.uuid4()),
        model=_precompact_model(context_window=100),
        last_prompt_tokens=500,
    )

    assert result.triggered is True
    assert result.required is True
    assert result.skipped_reason == "completed_by_concurrent_compaction"
    do_compact.assert_not_awaited()
