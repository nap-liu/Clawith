"""Real child-process loss resumes the durable tool result and completes once."""

import asyncio
import json
import os
import signal

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.active_turns import active_turn_boundary
from app.services.background_turns import initialize_background_turn, run_background_turn
from app.services.turn_interruption import TurnInterrupted
from execution_provider_fixture import provider
from test_agent_execution_integration import seed


@pytest.fixture(autouse=True)
async def isolated_engine(monkeypatch):
    monkeypatch.setenv("AGENT_EXECUTION_ISOLATION", "1")
    await engine.dispose()
    yield
    await engine.dispose()


async def background_anchor(url):
    aid, uid, sid, anchor_id, _ = await seed(url, "trigger")
    async with async_session() as db:
        session = await db.get(ChatSession, sid)
        anchor = await db.get(ChatMessage, anchor_id)
        await initialize_background_turn(db, session=session, anchor=anchor,
            kind="background", reference_id=anchor_id,
            settings={"include_soul": False, "include_memory": False})
        await db.commit()
    return aid, sid, anchor_id


async def test_child_loss_reuses_completed_tool_and_finalizes_once(monkeypatch):
    gate = asyncio.Event()
    child_pids = []
    spawn = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if "app.services.agent_execution.worker" in args:
            child_pids.append(process.pid)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    tool = {"id": "durable-report-read", "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": "workspace/report.txt"})}}
    responses = [{"tool_calls": [tool]}, {"content": "Interrupted reply", "_wait_for": gate},
                 {"content": "Recovered work complete."}]
    async with provider(responses) as (url, requests):
        aid, sid, anchor_id = await background_anchor(url)
        async with active_turn_boundary():
            task = asyncio.create_task(run_background_turn(anchor_id))
            async with asyncio.timeout(25):
                while len(requests) < 2:
                    await asyncio.sleep(.02)
            os.kill(child_pids[-1], signal.SIGKILL)
            with pytest.raises(TurnInterrupted):
                await task
        async with async_session() as db:
            anchor = await db.get(ChatMessage, anchor_id)
            assert anchor.message_meta["turn_status"] == "running"
        gate.set()
        from app.services.turn_recovery_dispatch import (
            cancel_recovery_dispatch, dispatch_recovery_candidates,
        )

        tasks = await dispatch_recovery_candidates()
        own_task = next(task for task in tasks if task.get_name() == f"turn_recovery:{anchor_id}")
        try:
            async with asyncio.timeout(25):
                assert (await own_task).resumed == 1
        finally:
            # The shared scanner can also find unfinished fixtures belonging to
            # other tests. Their duration must not become this turn's deadline.
            await cancel_recovery_dispatch()
        assert await run_background_turn(anchor_id) == "Recovered work complete."
        assert len(requests) == 3
        tool_history = [row for row in requests[2]["messages"] if row["role"] == "tool"]
        assert len(tool_history) == 1
        assert "Observable report contents" in tool_history[0]["content"]
        async with async_session() as db:
            rows = list(await db.scalars(select(ChatMessage).where(
                ChatMessage.conversation_id == str(sid))))
            anchor = await db.get(ChatMessage, anchor_id)
        terminal = [row for row in rows if row.role == "assistant"
                    and (row.message_meta or {}).get("turn_status") == "completed"]
        completed_tools = [json.loads(row.content) for row in rows if row.role == "tool_call"
                           and json.loads(row.content).get("status") == "done"]
        assert len(terminal) == len(completed_tools) == 1
        assert anchor.message_meta["turn_status"] == "completed"
        assert anchor.message_meta["background_execution"]["delivered"] is True
        assert len(set(child_pids)) >= 2


async def test_missing_model_finishes_failed_without_recovery_loop():
    from app.services.llm.failure_outcome import LLMFailure
    from app.services.turn_recovery_scanner import _load_recoverable_anchors

    async with provider([{"content": "unused"}]) as (url, requests):
        aid, _, anchor_id = await background_anchor(url)
        async with async_session() as db:
            agent = await db.get(Agent, aid)
            agent.primary_model_id = None
            agent.fallback_model_id = None
            await db.commit()
        result = await run_background_turn(anchor_id)
        assert isinstance(result, LLMFailure)
        assert result.code == "model_unavailable"
        assert requests == []
        async with async_session() as db:
            anchor = await db.get(ChatMessage, anchor_id)
            assert anchor.message_meta["turn_status"] == "failed"
            candidates = await _load_recoverable_anchors(db, include_legacy=False)
            assert anchor_id not in {row.id for row in candidates}
