"""Durable Trigger ownership across workers, process loss and receipt recovery."""

import asyncio
import json
import os
import signal
import sys
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

import app.models.registry  # noqa: F401
from app.database import async_session, engine
from app.models.audit import AuditLog, ChatMessage
from app.models.chat_session import ChatSession
from app.models.tool import Tool
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.services.active_turns import active_turn_boundary
from app.services.background_turns import run_background_turn
from app.services.chat_history import persist_assistant_reply_row
from app.services.trigger_daemon_invocation import _invoke_agent_for_triggers
from app.services.trigger_daemon_loop import wake_agent_with_context
from app.services.trigger_runtime.dispatch import enqueue_due_trigger
from app.services.trigger_runtime.executions import (
    build_execution_runtime_trigger,
    claim_pending_trigger_executions,
)
from app.services.trigger_runtime.queue import enqueue_trigger_execution
from app.services.turn_interruption import TurnInterrupted
from execution_provider_fixture import provider
from test_agent_execution_integration import seed


@pytest.fixture(autouse=True)
async def isolated_engine(monkeypatch):
    monkeypatch.setenv("AGENT_EXECUTION_ISOLATION", "1")
    await engine.dispose()
    async with async_session() as db:
        seeded = await db.scalar(select(Tool.id).where(Tool.name == "read_file"))
    if seeded is None:
        from app.services.tool_seeder import seed_builtin_tools

        await seed_builtin_tools()
    yield
    await engine.dispose()


async def runtime_for(execution_id):
    async with async_session() as db:
        execution = await db.get(TriggerExecution, execution_id)
        trigger = await db.get(AgentTrigger, execution.trigger_id)
        return build_execution_runtime_trigger(trigger, execution)


async def claim_own(execution_ids, sources):
    rows = await claim_pending_trigger_executions(sources=sources)
    return [(row, trigger) for row, trigger in rows if row.id in execution_ids]


async def test_concurrent_workers_claim_independent_timer_occurrences_once():
    async with provider([{"content": "Timer work completed."}]) as (url, requests):
        agent_id, user_id, _, _, _ = await seed(url, "trigger")
        ids = set()
        now = datetime.now(UTC).replace(microsecond=0)
        async with async_session() as db:
            for source, config in (
                ("cron", {"expr": "* * * * *"}),
                ("interval", {"minutes": 1}),
                ("once", {"at": now.isoformat()}),
            ):
                trigger = AgentTrigger(agent_id=agent_id, execution_user_id=user_id,
                    name=f"timer-{source}", type=source, config=config, reason="Execute this occurrence",
                    soul=False, memory=False)
                db.add(trigger)
                await db.flush()
                execution, created = await enqueue_trigger_execution(db, trigger=trigger, source=source,
                    idempotency_key=f"{source}:{now.isoformat()}", commit=False,
                    payload_obj={"_scheduled_for": now.isoformat(), "_scheduled_timezone": "UTC"})
                assert created
                ids.add(execution.id)
            await db.commit()
        async def worker_claim():
            process = await asyncio.create_subprocess_exec(
                sys.executable, __file__, "--claim", json.dumps([str(value) for value in ids]),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            )
            output, errors = await process.communicate()
            assert process.returncode == 0, errors.decode()
            return json.loads(output.decode().strip().splitlines()[-1])

        claims = await asyncio.gather(*(worker_claim() for _ in range(4)))
        claimed_ids = [value for batch in claims for value in batch]
        assert len(claimed_ids) == len(set(claimed_ids)) == 3
        assert set(claimed_ids) == {str(value) for value in ids}
        runtimes = [await runtime_for(uuid.UUID(value)) for value in claimed_ids]
        async with active_turn_boundary():
            await asyncio.gather(*(_invoke_agent_for_triggers(agent_id, [runtime]) for runtime in runtimes))
        assert len(requests) == 3
        async with async_session() as db:
            completed = list(await db.scalars(select(TriggerExecution).where(TriggerExecution.id.in_(ids))))
            assert {row.status for row in completed} == {"completed"}
            assert len({row.conversation_id for row in completed}) == 3
            triggers = list(await db.scalars(select(AgentTrigger).where(AgentTrigger.agent_id == agent_id)))
            assert all(trigger.fire_count == 1 for trigger in triggers)
            assert next(trigger for trigger in triggers if trigger.type == "once").is_enabled is False
        # A stale worker handle can reconcile terminal state but cannot run it again.
        async with active_turn_boundary():
            await asyncio.gather(*(_invoke_agent_for_triggers(agent_id, [runtime]) for runtime in runtimes))
        assert len(requests) == 3


async def test_webhook_two_process_losses_preserve_batch_and_sent_receipt_tail(monkeypatch):
    gates = [asyncio.Event(), asyncio.Event()]
    child_pids = []
    spawn = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        if "app.services.agent_execution.worker" in args:
            child_pids.append(process.pid)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    tool = {"id": "durable-webhook-read", "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": "workspace/report.txt"})}}
    responses = [{"tool_calls": [tool]}, {"content": "lost once", "_wait_for": gates[0]},
                 {"content": "lost twice", "_wait_for": gates[1]}, {"content": "Webhook finished."}]
    async with provider(responses) as (url, requests):
        agent_id, user_id, origin_id, _, _ = await seed(url)
        async with async_session() as db:
            trigger = AgentTrigger(agent_id=agent_id, execution_user_id=user_id, type="webhook",
                name="two-interruptions", reason="Read the report for this event", soul=False, memory=False,
                config={"webhook_mode": "queue", "_webhook_queue": ["first", "second"],
                        "_origin_session_id": str(origin_id), "_origin_source_channel": "web"})
            db.add(trigger)
            await db.commit()
            trigger_id = trigger.id
        await enqueue_due_trigger(trigger, datetime.now(UTC))
        async with async_session() as db:
            execution_id = await db.scalar(select(TriggerExecution.id).where(TriggerExecution.trigger_id == trigger_id))
        original_conversation = None
        try:
            for interruption in range(2):
                claims = await claim_own({execution_id}, ["webhook"])
                assert len(claims) == 1
                runtime = build_execution_runtime_trigger(claims[0][1], claims[0][0])
                async with active_turn_boundary():
                    task = asyncio.create_task(_invoke_agent_for_triggers(agent_id, [runtime]))
                    async with asyncio.timeout(25):
                        while len(requests) < interruption + 2:
                            await asyncio.sleep(.02)
                    os.kill(child_pids[-1], signal.SIGKILL)
                    with pytest.raises(TurnInterrupted):
                        await task
                async with async_session() as db:
                    execution = await db.get(TriggerExecution, execution_id)
                    trigger = await db.get(AgentTrigger, trigger_id)
                    assert execution.status == "pending"
                    assert trigger.config["_webhook_queue"] == ["first", "second"]
                    assert trigger.config["_webhook_active"] is True
                    original_conversation = original_conversation or execution.conversation_id
                    assert execution.conversation_id == original_conversation
                gates[interruption].set()
            # Crash after the actual shared receipt writer commits a successful
            # send, before the background completion marker can be committed.
            from app.services import im_delivery

            register = im_delivery.register_delivery
            receipt_commits = []

            async def exit_after_receipt(message_id, result):
                persisted = await register(message_id, result)
                if result.status == "sent":
                    receipt_commits.append(message_id)
                    raise TurnInterrupted("process lost after receipt commit")
                return persisted

            await claim_own({execution_id}, ["webhook"])
            runtime = await runtime_for(execution_id)
            with monkeypatch.context() as patch:
                patch.setattr(im_delivery, "register_delivery", exit_after_receipt)
                async with active_turn_boundary():
                    with pytest.raises(TurnInterrupted):
                        await _invoke_agent_for_triggers(agent_id, [runtime])
            assert len(receipt_commits) == 1
            async with async_session() as db:
                execution = await db.get(TriggerExecution, execution_id)
                assert execution.status == "completed"
                anchor = await db.scalar(select(ChatMessage).where(
                    ChatMessage.conversation_id == str(original_conversation), ChatMessage.role == "user"))
                assert anchor.message_meta["background_execution"]["delivered"] is False
                receipt = await db.get(ChatMessage, receipt_commits[0])
                assert receipt.message_meta["delivery"]["status"] == "sent"
            # Two late dispatchers both observe the original terminal turn.
            async with active_turn_boundary():
                await asyncio.gather(_invoke_agent_for_triggers(agent_id, [runtime]),
                                     _invoke_agent_for_triggers(agent_id, [runtime]))
            assert len(requests) == 4
            assert len(set(child_pids)) == 3
            async with async_session() as db:
                anchor = await db.get(ChatMessage, anchor.id)
                trigger = await db.get(AgentTrigger, trigger_id)
                rows = list(await db.scalars(select(ChatMessage).where(
                    ChatMessage.conversation_id == str(original_conversation))))
                audits = await db.scalar(select(func.count()).select_from(AuditLog).where(
                    AuditLog.agent_id == agent_id, AuditLog.action == "trigger_fired"))
                assert trigger.config["_webhook_queue"] == ["second"]
                assert trigger.fire_count == audits == 1
                assert anchor.message_meta["background_execution"]["delivered"] is True
                assert len([row for row in rows if row.role == "assistant"]) == 1
                done = [json.loads(row.content) for row in rows if row.role == "tool_call"
                        and json.loads(row.content).get("status") == "done"]
                assert len(done) == 1
                assert "Observable report contents" in done[0]["result"]
            for request in requests[2:]:
                assert len([message for message in request["messages"] if message["role"] == "tool"]) == 1
        finally:
            for gate in gates:
                gate.set()


async def test_on_message_and_async_wake_use_actual_shared_executor():
    async with provider([{"content": "Subscription handled."}, {"content": "Wake handled."}]) as (url, requests):
        agent_id, user_id, origin_id, origin_anchor_id, _ = await seed(url)
        async with async_session() as db:
            await persist_assistant_reply_row(db, agent_id=agent_id, user_id=user_id,
                conversation_id=str(origin_id), content="Subscription armed.", turn_anchor_id=origin_anchor_id)
            watched = ChatSession(agent_id=agent_id, user_id=user_id, source_channel="web")
            db.add(watched)
            await db.flush()
            matched = ChatMessage(agent_id=agent_id,user_id=user_id,conversation_id=str(watched.id),
                                  role="user",content="The requested report is ready.")
            db.add(matched)
            await db.flush()
            trigger = AgentTrigger(agent_id=agent_id,execution_user_id=user_id,name="reply-subscription",
                type="on_message",reason="Handle this reply",soul=False,memory=False,config={})
            db.add(trigger)
            await db.flush()
            execution, _ = await enqueue_trigger_execution(db,trigger=trigger,source="on_message",
                idempotency_key=str(matched.id),commit=False,payload_obj={
                    "_origin_session_id":str(origin_id), "_origin_source_channel":"web",
                    "_origin_turn_anchor_id":str(origin_anchor_id), "_matched_message_id":str(matched.id),
                    "_watch_session_id":str(watched.id)})
            await db.commit()
            execution_id = execution.id
        claims = await claim_own({execution_id},["on_message"])
        runtime = build_execution_runtime_trigger(claims[0][1],claims[0][0])
        async with active_turn_boundary():
            await _invoke_agent_for_triggers(agent_id,[runtime])
        async with async_session() as db:
            execution = await db.get(TriggerExecution,execution_id)
            assert execution.status == "completed" and execution.conversation_id == origin_id
            anchor = await db.scalar(select(ChatMessage).where(ChatMessage.conversation_id == str(origin_id),
                ChatMessage.message_meta["kind"].as_string() == "on_message_event"))
            assert anchor.message_meta["background_execution"]["delivered"] is True
        await wake_agent_with_context(agent_id,"Handle a standalone notification",skip_dedup=True)
        async with asyncio.timeout(25):
            while True:
                async with async_session() as db:
                    wake = await db.scalar(select(ChatMessage).where(ChatMessage.agent_id == agent_id,
                        ChatMessage.role == "user", ChatMessage.conversation_id != str(origin_id),
                        ChatMessage.message_meta["background_execution"]["kind"].as_string() == "trigger"))
                    if wake is not None and (wake.message_meta or {}).get("turn_status") == "completed":
                        break
                await asyncio.sleep(.02)
        assert await run_background_turn(wake.id) == "Wake handled."
        assert len(requests) == 2


async def _claim_worker():
    ids = {uuid.UUID(value) for value in json.loads(sys.argv[2])}
    claims = await claim_own(ids, ["cron", "interval", "once"])
    print(json.dumps([str(row.id) for row, _ in claims]))
    await engine.dispose()


if __name__ == "__main__" and sys.argv[1:2] == ["--claim"]:
    asyncio.run(_claim_worker())
