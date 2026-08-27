"""Integration test: chat_history.load_messages_for_session honors
ChatCompaction markers — compacted rows are filtered out, the active
summary is injected as a synthetic _SyntheticSummaryMessage at the
front of the returned list.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

# Import the full model graph so FK references resolve at table-mapping time.
# SQLAlchemy needs this when an FK column points at a parent model that
# this test file doesn't otherwise touch (e.g. ChatMessage.user_id → users.id).
from app.models.user import Identity, User  # noqa: F401
from app.models.agent import Agent, AgentPermission, AgentTemplate  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.models.identity import IdentityProvider, SSOScanSession  # noqa: F401
from app.models.participant import Participant  # noqa: F401
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction
from app.database import async_session, engine
from app.services.chat_history import (
    _SyntheticSummaryMessage,
    load_history_for_llm,
    load_history_prefix_before_anchor,
    load_messages_for_session,
    load_recoverable_messages_for_turn,
)
from app.services.llm.compactor import select_compaction_span


pytestmark = pytest.mark.asyncio


async def test_compaction_span_preserves_recent_turns_by_message_boundary():
    """The hard floor keeps eight full turns even when config asks for one."""
    rows = []
    for _ in range(9):
        rows.extend(
            [
                SimpleNamespace(role="user"),
                SimpleNamespace(role="assistant"),
            ]
        )

    assert select_compaction_span(rows, keep_recent_turns=1) == (0, 1)


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    """Dispose the global async engine before each test.

    Each pytest-asyncio test gets a fresh event loop, but the module-level
    asyncpg connection pool keeps connections that were bound to the previous
    loop. Reusing one of those connections raises
    `cannot perform operation: another operation is in progress`.
    Disposing forces a fresh pool inside the current loop.
    """
    await engine.dispose()
    yield
    await engine.dispose()


# NOTE on running these:
# Each test passes when run individually
# (`pytest tests/test_chat_history_compaction.py::<one_test>`). When
# run as a suite, the second test onward can fail with
# `asyncpg.exceptions._base.InterfaceError: cannot perform operation:
# another operation is in progress` — that's a known interaction
# between pytest-asyncio's shared event loop and asyncpg's connection
# pool, NOT a bug in the code under test. CI should run these with
# `--forked` or a fresh-loop fixture; for local dev, run one at a
# time when validating, and rely on the dryrun integration in
# scripts/run_compaction_dryrun.py for end-to-end coverage.


# A throwaway agent UUID for the test rows. ChatMessage.agent_id has a
# real FK to agents.id; we use ON DELETE SET NULL semantics on the
# compaction tables so missing parents don't block — but the agent FK
# *will* block. Tests pick an existing agent UUID at runtime.
TEST_USER = uuid.uuid4()


async def _pick_existing_agent_id() -> uuid.UUID:
    """Return any active agent's UUID. Lets us satisfy the FK without
    fixturing a fresh agent (which would also need a user, tenant, …).
    """
    from sqlalchemy import select as _sa_select
    async with async_session() as db:
        r = await db.execute(_sa_select(Agent.id).limit(1))
        agent_id = r.scalar_one_or_none()
        if agent_id is None:
            tenant = Tenant(
                name="Compaction Test",
                slug=f"compaction-{uuid.uuid4().hex[:10]}",
            )
            db.add(tenant)
            await db.flush()
            identity = Identity(
                username=f"compaction_{uuid.uuid4().hex[:10]}",
                email=f"{uuid.uuid4().hex[:10]}@test.local",
                password_hash="x",
            )
            db.add(identity)
            await db.flush()
            user = User(
                identity_id=identity.id,
                display_name="Compaction User",
                role="member",
                is_active=True,
                tenant_id=tenant.id,
            )
            db.add(user)
            await db.flush()
            agent = Agent(
                name="Compaction Agent",
                creator_id=user.id,
                tenant_id=tenant.id,
            )
            db.add(agent)
            await db.commit()
            agent_id = agent.id
        return agent_id


async def _pick_existing_user_id(agent_id: uuid.UUID) -> uuid.UUID:
    """Use the selected agent's creator so tenant-edge triggers remain valid."""
    from sqlalchemy import select as _sa_select
    async with async_session() as db:
        r = await db.execute(_sa_select(Agent.creator_id).where(Agent.id == agent_id))
        user_id = r.scalar_one_or_none()
        if user_id is None:
            pytest.skip("No users in DB; cannot run compaction integration tests")
        return user_id


async def _setup(rows_spec, marker_spec=None):
    """Insert messages + optional compaction marker. Returns
    (conversation_id, agent_id, inserted_messages_in_order, marker_or_None).

    rows_spec: list of (role, content, age_seconds)
    marker_spec: dict with keys: epoch, summary, from_idx, to_idx,
        flag_indices (list of indices in inserted_messages to flag),
        passed (bool, default True), superseded_by (None or another marker_spec)
    """
    agent_id = await _pick_existing_agent_id()
    user_id = await _pick_existing_user_id(agent_id)
    conv_id = f"compaction-test-{uuid.uuid4().hex[:8]}"

    async with async_session() as db:
        async with db.begin():
            inserted = []
            for role, content, age_s in rows_spec:
                msg = ChatMessage(
                    id=uuid.uuid4(),
                    agent_id=agent_id,
                    user_id=user_id,
                    role=role,
                    content=content,
                    conversation_id=conv_id,
                    created_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
                )
                db.add(msg)
                inserted.append(msg)
            await db.flush()

            marker = None
            if marker_spec:
                marker = ChatCompaction(
                    id=uuid.uuid4(),
                    session_id=conv_id,
                    agent_id=agent_id,
                    epoch=marker_spec["epoch"],
                    compacted_from_message_id=inserted[marker_spec["from_idx"]].id,
                    compacted_to_message_id=inserted[marker_spec["to_idx"]].id,
                    summary_text=marker_spec["summary"],
                    summary_tokens=len(marker_spec["summary"]) // 3,
                    trigger_prompt_tokens=100_000,
                    trigger_ratio=0.85,
                    summary_validation_passed=marker_spec.get("passed", True),
                )
                db.add(marker)
                await db.flush()
                # Flag specified rows
                for i in marker_spec.get("flag_indices", []):
                    inserted[i].compacted_into = marker.id

    return conv_id, agent_id, inserted, marker


async def _cleanup(conv_id: str):
    async with async_session() as db:
        async with db.begin():
            await db.execute(
                ChatMessage.__table__.delete().where(ChatMessage.conversation_id == conv_id)
            )
            await db.execute(
                ChatCompaction.__table__.delete().where(ChatCompaction.session_id == conv_id)
            )


async def test_no_compaction_returns_active_rows_in_order():
    conv_id, agent_id, inserted, _ = await _setup([
        ("user", "first", 300),
        ("assistant", "second", 200),
        ("user", "third", 100),
    ])
    try:
        async with async_session() as db:
            rows = await load_messages_for_session(
                db,
                agent_id=agent_id,
                conversation_id=conv_id,
                ctx_size=100,
            )
        assert [r.id for r in rows] == [m.id for m in inserted]
        assert all(not isinstance(r, _SyntheticSummaryMessage) for r in rows)
    finally:
        await _cleanup(conv_id)


async def test_row_limit_never_splits_or_drops_twelve_recent_tool_turns():
    import json as _json

    rows_spec = []
    age = 10_000
    for turn in range(12):
        rows_spec.append(("user", f"user-{turn}", age))
        age -= 1
        for tool in range(5):
            rows_spec.append(
                (
                    "tool_call",
                    _json.dumps(
                        {
                            "name": "test_tool",
                            "args": {"turn": turn, "tool": tool},
                            "status": "done",
                            "result": "ok",
                        }
                    ),
                    age,
                )
            )
            age -= 1
        rows_spec.append(("assistant", f"assistant-{turn}", age))
        age -= 1

    conv_id, agent_id, inserted, _ = await _setup(rows_spec)
    try:
        async with async_session() as db:
            rows = await load_messages_for_session(
                db,
                agent_id=agent_id,
                conversation_id=conv_id,
                ctx_size=3,
            )

        assert [row.id for row in rows] == [row.id for row in inserted]
        assert all(row.compacted_into is None for row in rows)
    finally:
        await _cleanup(conv_id)


async def test_recoverable_loader_never_row_slices_complete_turns():
    rows_spec = []
    age = 10_000
    for turn in range(9):
        rows_spec.extend(
            [
                ("user", f"user-{turn}", age),
                ("tool_call", '{"status":"done"}', age - 1),
                ("assistant", f"assistant-{turn}", age - 2),
            ]
        )
        age -= 3
    rows_spec.append(("user", "interrupted-current", age))

    conv_id, agent_id, inserted, _ = await _setup(rows_spec)
    try:
        async with async_session() as db:
            rows = await load_recoverable_messages_for_turn(
                db,
                agent_id=agent_id,
                conversation_id=conv_id,
                turn_anchor_id=inserted[-1].id,
                ctx_size=3,
            )

        assert [row.id for row in rows] == [row.id for row in inserted]
    finally:
        await _cleanup(conv_id)


async def test_flagged_rows_are_skipped_summary_injected():
    summary = (
        "## Summary of earlier conversation\n\n"
        "### Key facts\n- the older two messages were here\n"
    )
    conv_id, agent_id, inserted, marker = await _setup(
        [
            ("user", "older 1", 600),
            ("assistant", "older 2", 500),
            ("user", "recent 1", 200),
            ("assistant", "recent 2", 100),
        ],
        marker_spec={
            "epoch": 1,
            "summary": summary,
            "from_idx": 0,
            "to_idx": 1,
            "flag_indices": [0, 1],
            "passed": True,
        },
    )
    try:
        async with async_session() as db:
            rows = await load_messages_for_session(
                db,
                agent_id=agent_id,
                conversation_id=conv_id,
                ctx_size=100,
            )
        assert isinstance(rows[0], _SyntheticSummaryMessage)
        assert "<conversation-summary" in rows[0].content
        assert 'epoch="1"' in rows[0].content
        assert "the older two messages were here" in rows[0].content
        assert [r.id for r in rows[1:]] == [inserted[2].id, inserted[3].id]
    finally:
        await _cleanup(conv_id)


async def test_exact_anchor_prefix_keeps_summary_and_excludes_later_inputs():
    summary = (
        "## Summary of earlier conversation\n\n"
        "### Decisions\n- preserve the durable project context\n"
    )
    conv_id, agent_id, inserted, _ = await _setup(
        [
            ("user", "compacted question", 600),
            ("assistant", "compacted answer", 500),
            ("user", "exact turn anchor", 200),
            ("user", "later queued input", 100),
        ],
        marker_spec={
            "epoch": 1,
            "summary": summary,
            "from_idx": 0,
            "to_idx": 1,
            "flag_indices": [0, 1],
            "passed": True,
        },
    )
    try:
        async with async_session() as db:
            prefix = await load_history_prefix_before_anchor(
                db,
                agent_id=agent_id,
                conversation_id=conv_id,
                turn_anchor_id=inserted[2].id,
                ctx_size=100,
            )
            compacted_anchor = await load_history_prefix_before_anchor(
                db,
                agent_id=agent_id,
                conversation_id=conv_id,
                turn_anchor_id=inserted[0].id,
                ctx_size=100,
            )

        assert prefix is not None
        assert [message["role"] for message in prefix] == ["user"]
        assert "<conversation-summary" in prefix[0]["content"]
        assert "preserve the durable project context" in prefix[0]["content"]
        assert "exact turn anchor" not in prefix[0]["content"]
        assert "later queued input" not in prefix[0]["content"]
        assert compacted_anchor is None
    finally:
        await _cleanup(conv_id)


async def test_failed_validation_marker_is_ignored():
    conv_id, agent_id, inserted, _ = await _setup(
        [
            ("user", "msg 1", 200),
            ("assistant", "msg 2", 100),
        ],
        marker_spec={
            "epoch": 1,
            "summary": "x" * 50,
            "from_idx": 0,
            "to_idx": 1,
            "flag_indices": [],  # Failed validation → didn't flag rows
            "passed": False,
        },
    )
    try:
        async with async_session() as db:
            rows = await load_messages_for_session(
                db,
                agent_id=agent_id,
                conversation_id=conv_id,
                ctx_size=100,
            )
        assert all(not isinstance(r, _SyntheticSummaryMessage) for r in rows)
        assert [r.id for r in rows] == [m.id for m in inserted]
    finally:
        await _cleanup(conv_id)


async def test_superseded_marker_is_ignored_only_active_used():
    summary_v1 = "## Summary v1\n\n### Key facts\n- old summary\n"
    summary_v2 = "## Summary v2 (chained)\n\n### Key facts\n- v1 plus mid range\n"

    # 6 messages, 2 epochs of compaction
    rows_spec = [
        ("user", "oldest", 900),
        ("assistant", "old", 800),
        ("user", "mid u", 600),
        ("assistant", "mid a", 500),
        ("user", "recent u", 200),
        ("assistant", "recent a", 100),
    ]
    agent_id = await _pick_existing_agent_id()
    user_id = await _pick_existing_user_id(agent_id)
    conv_id = f"compaction-test-{uuid.uuid4().hex[:8]}"

    try:
        async with async_session() as db:
            async with db.begin():
                inserted = []
                for role, content, age_s in rows_spec:
                    msg = ChatMessage(
                        id=uuid.uuid4(),
                        agent_id=agent_id,
                        user_id=user_id,
                        role=role,
                        content=content,
                        conversation_id=conv_id,
                        created_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
                    )
                    db.add(msg)
                    inserted.append(msg)
                await db.flush()

                comp_v1 = ChatCompaction(
                    id=uuid.uuid4(),
                    session_id=conv_id,
                    agent_id=agent_id,
                    epoch=1,
                    compacted_from_message_id=inserted[0].id,
                    compacted_to_message_id=inserted[1].id,
                    summary_text=summary_v1,
                    summary_tokens=len(summary_v1) // 3,
                    trigger_prompt_tokens=100_000,
                    trigger_ratio=0.85,
                    summary_validation_passed=True,
                )
                comp_v2 = ChatCompaction(
                    id=uuid.uuid4(),
                    session_id=conv_id,
                    agent_id=agent_id,
                    epoch=2,
                    compacted_from_message_id=inserted[0].id,
                    compacted_to_message_id=inserted[3].id,
                    summary_text=summary_v2,
                    summary_tokens=len(summary_v2) // 3,
                    trigger_prompt_tokens=100_000,
                    trigger_ratio=0.85,
                    summary_validation_passed=True,
                )
                db.add(comp_v1)
                db.add(comp_v2)
                await db.flush()

                comp_v1.superseded_by = comp_v2.id
                for i in (0, 1, 2, 3):
                    inserted[i].compacted_into = comp_v2.id

        async with async_session() as db:
            loaded = await load_messages_for_session(
                db,
                agent_id=agent_id,
                conversation_id=conv_id,
                ctx_size=100,
            )

        assert isinstance(loaded[0], _SyntheticSummaryMessage)
        assert 'epoch="2"' in loaded[0].content
        assert "v1 plus mid range" in loaded[0].content
        assert 'epoch="1"' not in loaded[0].content
        assert [r.id for r in loaded[1:]] == [inserted[4].id, inserted[5].id]
    finally:
        await _cleanup(conv_id)


async def test_load_history_for_llm_compaction_and_tool_call_coexist():
    """The IM-channel loader must combine compaction with tool-call expansion:
    older messages fold into an injected summary, while a surviving (non-folded)
    tool_call row is still expanded into the assistant(tool_calls)+tool(result)
    pair. This guards the regression point that compaction stays correct for all
    channels after the tool_call-history change."""
    import json as _json

    summary = "## Summary\n\n### Key facts\n- earlier sales discussion\n"
    tc_content = _json.dumps({"name": "get_weather", "args": {"city": "SH"}, "status": "done", "result": "sunny"})
    conv_id, agent_id, inserted, marker = await _setup(
        [
            ("user", "older question", 600),
            ("assistant", "older answer", 500),
            ("tool_call", tc_content, 200),
            ("assistant", "今天上海晴", 100),
        ],
        marker_spec={
            "epoch": 1,
            "summary": summary,
            "from_idx": 0,
            "to_idx": 1,
            "flag_indices": [0, 1],
            "passed": True,
        },
    )
    try:
        async with async_session() as db:
            history = await load_history_for_llm(db, agent_id=agent_id, conversation_id=conv_id, ctx_size=100)

        roles = [m["role"] for m in history]
        # injected summary (user) + expanded tool pair + final assistant reply
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert "tool_call" not in roles  # surviving tool_call expanded, not raw
        assert "<conversation-summary" in history[0]["content"]
        assert history[1]["tool_calls"][0]["function"]["name"] == "get_weather"
        assert history[2]["content"] == "sunny"
        assert history[3]["content"] == "今天上海晴"
    finally:
        await _cleanup(conv_id)


def _precompact_model(context_window, ratio=0.85, keep=8, summary_max=2000):
    """SimpleNamespace duck-typing the LLMModel surface the compactor reads."""
    from types import SimpleNamespace

    return SimpleNamespace(
        context_window=context_window,
        compact_trigger_ratio=ratio,
        keep_recent_turns=keep,
        compact_summary_max_tokens=summary_max,
    )


async def _markers_for(conv_id: str):
    from sqlalchemy import select as _select

    async with async_session() as db:
        return (await db.execute(_select(ChatCompaction).where(ChatCompaction.session_id == conv_id))).scalars().all()


async def test_precompact_noop_below_threshold():
    """Pre-flight is a cheap no-op when the prompt is well under the window:
    returns False and writes NO compaction marker (no summary LLM call)."""
    from app.services.llm.compactor import maybe_precompact_prompt

    conv_id, agent_id, _, _ = await _setup([("user", "hi", 100), ("assistant", "hello", 50)])
    try:
        result = await maybe_precompact_prompt(
            agent_id=agent_id,
            conversation_id=conv_id,
            model=_precompact_model(context_window=131072),
            prompt_messages=[{"role": "user", "content": "short"}],
        )
        assert result.triggered is False
        assert result.required is False
        assert await _markers_for(conv_id) == []
    finally:
        await _cleanup(conv_id)


async def test_precompact_noop_when_history_too_small():
    """Even when the estimate crosses the pre-flight ratio, compaction is a
    no-op when there isn't enough older history to fold — no summary LLM call,
    no marker (guards against thrashing tiny conversations)."""
    from app.services.llm.compactor import maybe_precompact_prompt

    conv_id, agent_id, _, _ = await _setup([("user", "hi", 100), ("assistant", "hello", 50)])
    try:
        result = await maybe_precompact_prompt(
            agent_id=agent_id,
            conversation_id=conv_id,
            model=_precompact_model(context_window=100),  # tiny window → estimate >> 95%
            prompt_messages=[{"role": "user", "content": "x" * 4000}],
        )
        assert result.triggered is False  # select_compaction_span returns None (too few rows)
        assert result.required is True
        assert result.skipped_reason == "span_too_small_to_be_worth_compacting"
        assert await _markers_for(conv_id) == []
    finally:
        await _cleanup(conv_id)


async def test_precompact_noop_without_conversation_id():
    """No conversation_id → pre-flight returns an explicit no-op result."""
    from app.services.llm.compactor import maybe_precompact_prompt

    result = await maybe_precompact_prompt(
        agent_id=uuid.uuid4(),
        conversation_id="",
        model=_precompact_model(context_window=100),
        prompt_messages=[{"role": "user", "content": "x" * 4000}],
    )
    assert result.triggered is False
    assert result.required is False


async def test_compaction_never_marks_recent_eight_or_current_turn(monkeypatch):
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
            pre_flight_estimate=10_000,
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

        assert rows[0].compacted_into is not None
        assert rows[1].compacted_into == rows[0].compacted_into
        assert all(row.compacted_into is None for row in rows[2:])
        assert [row.content for row in rows[2:]] == [row.content for row in inserted[2:]]
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
            pre_flight_estimate=10_000,
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
        pre_flight_estimate=500,
    )

    assert result.triggered is True
    assert result.required is True
    assert result.skipped_reason == "completed_by_concurrent_compaction"
    do_compact.assert_not_awaited()


async def test_preflight_threshold_reserves_configured_output_tokens():
    """Preflight must fire before the final dispatch guard's input ceiling."""
    from app.services.llm.compactor import prompt_exceeds_preflight_limit

    model = _precompact_model(context_window=1_000)
    model.provider = "custom"
    model.model = "test"
    model.max_output_tokens = 250

    assert prompt_exceeds_preflight_limit(
        model=model,
        prompt_messages=[{"role": "user", "content": "数" * 1_800}],
    )
