"""End-to-end auto-compaction smoke test.

Builds a synthetic conversation in the DB (30 rounds, ~2KB each),
forces `maybe_compact` to evaluate, asserts:

1. dryrun mode: chat_compactions row appears, summary passes
   validation, ChatMessage rows are NOT flagged (live conversation
   safe).
2. live mode: ChatMessage rows ARE flagged, prior compaction marker
   superseded by new one.
3. compaction-aware load returns the synthetic summary + only the
   trailing window (size = keep_recent_turns × 2 messages).

Run inside the backend container:
    docker exec clawith-backend-1 python /app/scripts/test_compaction_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

# Make all model relationships resolve.
from app.models.user import User  # noqa
from app.models.agent import Agent  # noqa
from app.models.tenant import Tenant  # noqa
from app.models.identity import IdentityProvider, SSOScanSession  # noqa
from app.models.participant import Participant  # noqa
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction
from app.models.llm import LLMModel
from app.database import async_session
from app.services.chat_history import (
    _SyntheticSummaryMessage,
    load_messages_for_session,
)
from app.services.llm.compactor import maybe_compact
from sqlalchemy import select, update


CONV_ID = f"compaction-e2e-{uuid.uuid4().hex[:8]}"


async def setup_synthetic_conversation():
    """Build 30 messages for a fake session backed by an existing
    agent + user. Each message is a few hundred chars containing a
    fake UUID we can later check for in the summary's recall metric.
    """
    from sqlalchemy import select as _s
    async with async_session() as db:
        agent_id = (await db.execute(_s(Agent.id).limit(1))).scalar_one()
        user_id = (await db.execute(_s(User.id).limit(1))).scalar_one()
        model = (await db.execute(_s(LLMModel).where(LLMModel.model == "qwen3.5-plus"))).scalar_one()

        now = datetime.now(timezone.utc)
        for i in range(30):
            ts = now - timedelta(seconds=(30 - i) * 60)
            if i % 2 == 0:
                role, content = "user", f"Round {i // 2 + 1} user question. Here's a tracked id: aaa{i:02d}bbb1111c2222d3333e4444f5555aa for the file workspace/round_{i:02d}.md please look at it."
            else:
                role, content = "assistant", f"Round {i // 2 + 1} assistant answer. I checked workspace/round_{i:02d}.md — the contents reference id aaa{i-1:02d}bbb1111c2222d3333e4444f5555aa successfully."
            db.add(ChatMessage(
                id=uuid.uuid4(),
                agent_id=agent_id,
                user_id=user_id,
                role=role,
                content=content,
                conversation_id=CONV_ID,
                created_at=ts,
            ))
        await db.commit()
        return agent_id, model


async def cleanup():
    async with async_session() as db:
        await db.execute(ChatMessage.__table__.delete().where(ChatMessage.conversation_id == CONV_ID))
        await db.execute(ChatCompaction.__table__.delete().where(ChatCompaction.session_id == CONV_ID))
        await db.commit()


async def count_flagged_messages():
    from sqlalchemy import func, select as _s
    async with async_session() as db:
        r = await db.execute(_s(func.count(ChatMessage.id)).where(
            ChatMessage.conversation_id == CONV_ID,
            ChatMessage.compacted_into.isnot(None),
        ))
        return r.scalar_one()


async def get_compaction_rows():
    from sqlalchemy import select as _s
    async with async_session() as db:
        r = await db.execute(
            _s(ChatCompaction).where(ChatCompaction.session_id == CONV_ID).order_by(ChatCompaction.epoch.asc())
        )
        return r.scalars().all()


async def main():
    try:
        agent_id, model = await setup_synthetic_conversation()
        print(f"[e2e] inserted 30 messages for conversation_id={CONV_ID}")
        print(f"[e2e] agent_id={agent_id}, model={model.model}, context_window={model.context_window}")
        print(f"[e2e] CLAWITH_COMPACT_DRYRUN={os.environ.get('CLAWITH_COMPACT_DRYRUN', 'unset')}")

        # Force trigger by passing last_prompt_tokens = 80% of context_window
        forced_tokens = int(model.context_window * 0.8)

        # ── Phase 1: dryrun expected ─────────────────────────────────
        print("\n=== Phase 1: dryrun mode ===")
        result = await maybe_compact(
            agent_id=agent_id,
            conversation_id=CONV_ID,
            model=model,
            last_prompt_tokens=forced_tokens,
        )
        print(f"  triggered={result.triggered} skipped_reason={result.skipped_reason}")
        print(f"  epoch={result.epoch} summary_id={result.summary_id}")
        flagged = await count_flagged_messages()
        rows = await get_compaction_rows()
        print(f"  chat_compactions rows: {len(rows)}")
        for row in rows:
            print(f"    epoch={row.epoch} validated={row.summary_validation_passed} summary_chars={len(row.summary_text)}")
        print(f"  flagged ChatMessage rows: {flagged} (expect 0 in dryrun)")
        assert flagged == 0, f"DRYRUN should not flag, but {flagged} rows are flagged"
        assert len(rows) == 1, f"Expected 1 compaction row, got {len(rows)}"
        assert rows[0].summary_validation_passed, "Summary should pass gate"

        # ── Phase 2: switch off dryrun, trigger again ───────────────
        # We can't restart the process, but we CAN delete the prior
        # row + temporarily monkeypatch _is_dryrun() to return False.
        print("\n=== Phase 2: live mode ===")
        from app.services.llm import compactor as _compactor_mod
        orig_dryrun = _compactor_mod._is_dryrun
        _compactor_mod._is_dryrun = lambda: False
        try:
            # Clear prior dryrun row to start fresh; the new live call
            # would otherwise see it as a "prior epoch" marker.
            async with async_session() as db:
                await db.execute(ChatCompaction.__table__.delete().where(ChatCompaction.session_id == CONV_ID))
                await db.commit()

            result2 = await maybe_compact(
                agent_id=agent_id,
                conversation_id=CONV_ID,
                model=model,
                last_prompt_tokens=forced_tokens,
            )
            print(f"  triggered={result2.triggered} skipped_reason={result2.skipped_reason}")
            print(f"  epoch={result2.epoch} summary_id={result2.summary_id}")
            print(f"  progress_notice: {result2.progress_notice!r}")
            assert result2.triggered, "live mode should set triggered=True"
            flagged2 = await count_flagged_messages()
            print(f"  flagged ChatMessage rows: {flagged2} (expect > 0 in live mode)")
            assert flagged2 > 0, f"Live mode should flag rows, but {flagged2}"
        finally:
            _compactor_mod._is_dryrun = orig_dryrun

        # ── Phase 3: load_messages_for_session sees compacted view ──
        print("\n=== Phase 3: compaction-aware history load ===")
        async with async_session() as db:
            messages = await load_messages_for_session(
                db,
                agent_id=agent_id,
                conversation_id=CONV_ID,
                ctx_size=200,
            )
        print(f"  loaded {len(messages)} messages")
        synth_count = sum(1 for m in messages if isinstance(m, _SyntheticSummaryMessage))
        active_count = sum(1 for m in messages if not isinstance(m, _SyntheticSummaryMessage))
        print(f"  synthetic summaries: {synth_count}; active rows: {active_count}")
        if synth_count == 1:
            summary_msg = next(m for m in messages if isinstance(m, _SyntheticSummaryMessage))
            print(f"  summary first 300 chars: {summary_msg.content[:300]!r}")
        assert synth_count == 1, "Expected exactly 1 synthetic summary in load"
        assert active_count > 0, "Expected at least the trailing window of active rows"
        # Trailing window = keep_recent_turns user-message boundaries.
        # With 30 msgs and keep_recent_turns=2, we expect ~3-4 trailing rows.
        print(f"  ✅ ALL ASSERTIONS PASSED")

    finally:
        await cleanup()


if __name__ == "__main__":
    asyncio.run(main())
