"""Losing a real Redis execution lease interrupts rather than fails a turn."""

import asyncio
import hashlib

from app.core.events import get_redis
from app.database import async_session
from app.models.audit import ChatMessage
from app.services.active_turns import active_turn_boundary
from app.services.background_turns import run_background_turn
from app.services.channel_llm import _call_agent_llm
from app.services.turn_interruption import TurnInterrupted
from execution_provider_fixture import provider
from test_unified_turn_process_recovery import background_anchor, isolated_engine as isolated_engine

import pytest


async def test_real_redis_owner_loss_preserves_turn_for_resume():
    gate = asyncio.Event()
    async with provider([{"content": "old owner", "_wait_for": gate},
                         {"content": "New owner completed."}]) as (url, requests):
        aid, sid, anchor_id = await background_anchor(url)
        async with async_session() as db:
            user_id = (await db.get(ChatMessage, anchor_id)).user_id

        async def execute():
            async with active_turn_boundary(), async_session() as db:
                return await _call_agent_llm(db, aid, "Read the report.", session_id=str(sid),
                    user_id=user_id, turn_anchor_id=anchor_id, include_soul=False, include_memory=False)

        task = asyncio.create_task(execute())
        async with asyncio.timeout(25):
            while not requests:
                if task.done():
                    await task
                await asyncio.sleep(.02)
        redis = await get_redis()
        key = "clawith:conversation-execution:" + hashlib.sha256(str(sid).encode()).hexdigest()
        assert await redis.exists(key)
        # Emulate a lease being reclaimed after an owner stopped renewing. The
        # original process must fence itself; it cannot publish the old result.
        await redis.set(key, "fault-drill-new-owner", ex=30)
        try:
            with pytest.raises(TurnInterrupted):
                async with asyncio.timeout(20):
                    await task
            assert await redis.get(key) in {b"fault-drill-new-owner", "fault-drill-new-owner"}
            async with async_session() as db:
                anchor = await db.get(ChatMessage, anchor_id)
                assert anchor.message_meta["turn_status"] == "running"
        finally:
            await redis.delete(key)
            gate.set()
        assert await run_background_turn(anchor_id) == "New owner completed."
        assert len(requests) == 2
        async with async_session() as db:
            anchor = await db.get(ChatMessage, anchor_id)
            assert anchor.message_meta["turn_status"] == "completed"
