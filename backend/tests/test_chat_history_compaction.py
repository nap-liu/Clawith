"""Integration test: chat_history.load_messages_for_session honors
ChatCompaction markers — compacted rows are filtered out, the active
summary is injected as a synthetic _SyntheticSummaryMessage at the
front of the returned list.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

# Import the full model graph so FK references resolve at table-mapping time.
# SQLAlchemy needs this when an FK column points at a parent model that
# this test file doesn't otherwise touch (e.g. ChatMessage.user_id → users.id).
from app.models.user import User  # noqa: F401
from app.models.agent import Agent, AgentPermission, AgentTemplate  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.models.identity import IdentityProvider, SSOScanSession  # noqa: F401
from app.models.participant import Participant  # noqa: F401
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction
from app.database import async_session
from app.services.chat_history import (
    _SyntheticSummaryMessage,
    load_messages_for_session,
)


pytestmark = pytest.mark.asyncio


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
            pytest.skip("No agents in DB; cannot run compaction integration tests")
        return agent_id


async def _pick_existing_user_id() -> uuid.UUID:
    """Same idea for chat_messages.user_id."""
    from sqlalchemy import select as _sa_select
    async with async_session() as db:
        r = await db.execute(_sa_select(User.id).limit(1))
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
    user_id = await _pick_existing_user_id()
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
    user_id = await _pick_existing_user_id()
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
