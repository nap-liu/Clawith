"""Observe actual background admission and transaction boundaries over PostgreSQL."""

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.schedule import AgentSchedule
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.services.background_turns import run_background_turn
from app.services.scheduler import _claim_due_schedules_for_scope, prepare_schedule_turn
from app.services.trigger_daemon_invocation import _invoke_agent_for_triggers
from app.services.trigger_runtime.invoker import invoke_agent_for_triggers
from app.services.trigger_runtime.executions import build_execution_runtime_trigger
from app.services.trigger_runtime.queue import enqueue_trigger_execution
from app.services.workload_capacity import CapacityDimension, WorkloadKind, WorkloadOverloadedError
from execution_provider_fixture import provider
from test_background_turn_recovery import (
    admit_schedule, admit_task, isolate_background_runtime, runtime, wait_for_requests,  # noqa: F401
)
from test_trigger_durable_recovery import claim_own

pytestmark = pytest.mark.asyncio


async def assert_reads_released():
    async with async_session() as db:
        assert await db.scalar(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND state = 'idle in transaction' AND pid <> pg_backend_pid()"
        )) == 0


class CheckedCapacity:
    def __init__(self, overloaded=False):
        self.overloaded = overloaded
        self.calls = []

    @asynccontextmanager
    async def slot(self, kind, tenant):
        await assert_reads_released()
        self.calls.append((kind, tenant))
        if self.overloaded:
            raise WorkloadOverloadedError(kind=kind, tenant_id=str(tenant), timeout_seconds=.01,
                                          blocked_by=(CapacityDimension.CATEGORY,))
        yield


async def manual_schedule(aid, uid):
    schedule_id, _ = await admit_schedule(aid, uid)
    async with async_session() as db:
        anchor = await prepare_schedule_turn(db, schedule_id, aid, "Manual work", uid, soul=False, memory=False)
        await db.commit()
    return schedule_id, anchor


async def check_wait(kind, monkeypatch):
    gate, capacity = asyncio.Event(), CheckedCapacity()
    monkeypatch.setattr("app.services.turn_recovery.get_workload_capacity", lambda: capacity)
    async with provider([{"content": "Completed", "_wait_for": gate}]) as (url, requests):
        aid, uid = await runtime(url)
        _, anchor = await (admit_task(aid, uid) if kind == "task" else manual_schedule(aid, uid))
        running = asyncio.create_task(run_background_turn(anchor.id))
        try:
            await wait_for_requests(requests)
            await assert_reads_released()
            expected = WorkloadKind.BACKGROUND if kind == "task" else WorkloadKind.SCHEDULED
            assert capacity.calls[0][0] == expected
        finally:
            gate.set()
        assert await asyncio.wait_for(running, 20) == "Completed"


async def test_schedule_releases_read_session_before_context_and_llm(monkeypatch):
    await check_wait("schedule", monkeypatch)


async def test_task_releases_snapshots_before_context_and_llm(monkeypatch):
    await check_wait("task", monkeypatch)


async def test_schedule_capacity_timeout_is_reported_as_retryable(monkeypatch):
    capacity = CheckedCapacity(overloaded=True)
    monkeypatch.setattr("app.services.turn_recovery.get_workload_capacity", lambda: capacity)
    async with provider([{"content": "Retried successfully"}]) as (url, requests):
        aid, uid = await runtime(url)
        schedule_id, anchor = await manual_schedule(aid, uid)
        with pytest.raises(WorkloadOverloadedError):
            await run_background_turn(anchor.id)
        assert requests == []
        async with async_session() as db:
            assert (await db.get(AgentSchedule, schedule_id)).run_count == 0
            assert (await db.get(ChatMessage, anchor.id)).message_meta["turn_status"] == "running"
        capacity.overloaded = False
        assert await run_background_turn(anchor.id) == "Retried successfully"
        assert await run_background_turn(anchor.id) == "Retried successfully"
        assert len(requests) == 1
        async with async_session() as db:
            assert (await db.get(AgentSchedule, schedule_id)).run_count == 1


async def test_due_schedule_claim_commits_before_returning_dispatch_data():
    async with provider([{"content": "Unused"}]) as (url, requests):
        aid, uid = await runtime(url)
        schedule_id, now = await admit_schedule(aid, uid)
        own = next(item for item in await _claim_due_schedules_for_scope(now, project_agents=False)
                   if item.id == schedule_id)
        async with async_session() as db:
            anchor = await db.get(ChatMessage, own.anchor_id)
            assert anchor.message_meta["turn_status"] == "running"
            assert (await db.get(AgentSchedule, schedule_id)).next_run_at > now
        assert requests == []
        assert not [item for item in await _claim_due_schedules_for_scope(now, project_agents=False)
                    if item.id == schedule_id]


async def claimed_trigger(aid, uid, source):
    async with async_session() as db:
        trigger = AgentTrigger(agent_id=aid, execution_user_id=uid, name="Boundary event", type=source,
                               config={"minutes": 1} if source == "interval" else {},
                               reason="Handle event", soul=False, memory=False)
        db.add(trigger)
        await db.flush()
        execution, created = await enqueue_trigger_execution(
            db, trigger=trigger, source=source, idempotency_key=str(uuid.uuid4()), commit=False)
        assert created
        await db.commit()
        execution_id = execution.id
    claims = await claim_own({execution_id}, [source])
    return execution_id, build_execution_runtime_trigger(claims[0][1], claims[0][0])


async def trigger_boundary(monkeypatch, invoker, source="interval", overloaded=False):
    capacity, gate = CheckedCapacity(overloaded), asyncio.Event()
    monkeypatch.setattr("app.services.turn_recovery.get_workload_capacity", lambda: capacity)
    async with provider([{"content": "Event completed", "_wait_for": gate}]) as (url, requests):
        aid, uid = await runtime(url)
        execution_id, trigger = await claimed_trigger(aid, uid, source)
        if overloaded:
            await invoker(aid, [trigger])
            assert requests == []
            async with async_session() as db:
                stored = await db.get(TriggerExecution, execution_id)
                assert stored.status == "pending" and stored.conversation_id is not None
                original_session = stored.conversation_id
            capacity.overloaded = False
        running = asyncio.create_task(invoker(aid, [trigger]))
        try:
            await wait_for_requests(requests)
            await assert_reads_released()
            assert capacity.calls and all(kind == WorkloadKind.SCHEDULED for kind, _ in capacity.calls)
        finally:
            gate.set()
        await asyncio.wait_for(running, 20)
        await invoker(aid, [trigger])
        assert len(requests) == 1
        async with async_session() as db:
            stored = await db.get(TriggerExecution, execution_id)
            assert stored.status == "completed" and stored.execution_user_id == uid
            if overloaded:
                assert stored.conversation_id == original_session
            anchors = list(await db.scalars(select(ChatMessage).where(
                ChatMessage.conversation_id == str(stored.conversation_id), ChatMessage.role == "user")))
            assert len(anchors) == 1


async def test_trigger_runtime_waits_for_capacity_after_identity_session_closes(monkeypatch):
    await trigger_boundary(monkeypatch, invoke_agent_for_triggers)


async def test_trigger_daemon_waits_for_capacity_after_identity_session_closes(monkeypatch):
    await trigger_boundary(monkeypatch, _invoke_agent_for_triggers)


async def test_on_message_trigger_delegates_one_scheduled_admission(monkeypatch):
    await trigger_boundary(monkeypatch, invoke_agent_for_triggers, source="on_message")


async def test_trigger_runtime_capacity_timeout_requeues_execution(monkeypatch):
    await trigger_boundary(monkeypatch, invoke_agent_for_triggers, overloaded=True)


async def test_trigger_capacity_timeout_requeues_durable_execution(monkeypatch):
    await trigger_boundary(monkeypatch, _invoke_agent_for_triggers, overloaded=True)
