import asyncio
import uuid

import pytest
from sqlalchemy import func, select

import app.models.chat_compaction  # noqa: F401 - registers ChatMessage FK target
import app.models.chat_session
import app.models.participant  # noqa: F401 - registers ChatSession FK target
import app.models.project  # noqa: F401 - registers ChatSession FK target
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import AuditLog, ChatMessage
from app.models.chat_session import ChatSession
from app.models.schedule import AgentSchedule
from app.models.task import Task, TaskLog
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.user import Identity, User
from app.services.background_manual_run import (
    BackgroundManualRunConflict,
    handle_run_background_resource,
    run_background_resource,
)
from app.services.trigger_runtime.executions import claim_pending_trigger_executions

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_cases():
    await engine.dispose()
    yield
    await engine.dispose()


async def _user(db, tenant_id, username):
    identity = Identity(
        username=username,
        email=f"{username}@example.com",
        password_hash="test",
    )
    db.add(identity)
    await db.flush()
    user = User(
        identity_id=identity.id,
        tenant_id=tenant_id,
        display_name=username,
        role="member",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


async def test_manual_run_uses_one_actor_aligned_path_for_all_resource_types(monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Manual Run {suffix}", slug=f"manual-run-{suffix}")
        db.add(tenant)
        await db.flush()
        creator = await _user(db, tenant.id, f"creator_{suffix}")
        actor = await _user(db, tenant.id, f"actor_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=creator.id,
            name=f"Manual Run Agent {suffix}",
            access_mode="company",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        task = Task(
            agent_id=agent.id,
            title="Manual task",
            type="todo",
            status="pending",
            priority="medium",
            created_by=creator.id,
            execution_user_id=creator.id,
        )
        schedule = AgentSchedule(
            agent_id=agent.id,
            name="Manual schedule",
            instruction="Run the schedule",
            cron_expr="0 9 * * *",
            is_enabled=False,
            run_count=0,
            created_by=creator.id,
            execution_user_id=creator.id,
        )
        trigger = AgentTrigger(
            agent_id=agent.id,
            created_by_user_id=creator.id,
            execution_user_id=creator.id,
            name="Manual trigger",
            type="cron",
            config={"expr": "0 9 * * *"},
            reason="Run the trigger",
            is_enabled=False,
        )
        db.add_all([task, schedule, trigger])
        await db.commit()

    calls = []

    async def _capture_task(*args):
        calls.append(args)

    async def _capture_schedule(*args):
        from app.services.scheduler import ScheduleExecutionOutcome

        calls.append(args)
        return ScheduleExecutionOutcome.SUCCEEDED

    monkeypatch.setattr("app.services.task_executor.execute_task", _capture_task)
    monkeypatch.setattr(
        "app.services.scheduler._execute_schedule",
        _capture_schedule,
    )

    async with async_session() as db:
        await run_background_resource(
            db,
            actor_user_id=actor.id,
            agent_id=agent.id,
            resource_type="task",
            resource="Manual task",
        )
    async with async_session() as db:
        await run_background_resource(
            db,
            actor_user_id=actor.id,
            agent_id=agent.id,
            resource_type="schedule",
            resource=str(schedule.id),
        )
    async with async_session() as db:
        trigger_result = await run_background_resource(
            db,
            actor_user_id=actor.id,
            agent_id=agent.id,
            resource_type="trigger",
            resource=str(trigger.id),
        )

    await asyncio.sleep(0.05)
    assert len(calls) == 2
    assert calls[0][-1] == actor.id
    assert calls[1][-1] == actor.id

    async with async_session() as db:
        assert (await db.get(Task, task.id)).execution_user_id == actor.id
        stored_schedule = await db.get(AgentSchedule, schedule.id)
        assert stored_schedule.execution_user_id == actor.id
        assert stored_schedule.run_count == 1
        assert stored_schedule.last_run_at is not None
        stored_trigger = await db.get(AgentTrigger, trigger.id)
        assert stored_trigger.execution_user_id == actor.id
        execution = await db.get(TriggerExecution, trigger_result.execution_id)
        assert execution.source == "manual"
        assert execution.status == "pending"
        assert execution.execution_user_id == actor.id
        assert (
            await db.scalar(
                select(func.count(AuditLog.id)).where(
                    AuditLog.action == "background_resource_manual_run",
                    AuditLog.agent_id == agent.id,
                )
            )
            == 3
        )

    claimed = await claim_pending_trigger_executions(sources=["manual"])
    claimed_execution, claimed_trigger = next(pair for pair in claimed if pair[0].id == trigger_result.execution_id)
    assert claimed_execution.execution_user_id == actor.id
    assert claimed_trigger.id == trigger.id


async def test_manual_schedule_overload_does_not_record_success(monkeypatch):
    """A capacity-deferred manual schedule remains absent from success counters."""

    from app.services.background_manual_run import _execute_and_track_schedule
    from app.services.scheduler import ScheduleExecutionOutcome

    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Manual Overload {suffix}", slug=f"manual-overload-{suffix}")
        db.add(tenant)
        await db.flush()
        actor = await _user(db, tenant.id, f"manual_overload_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=actor.id,
            name=f"Manual Overload Agent {suffix}",
            access_mode="private",
            status="running",
        )
        db.add(agent)
        await db.flush()
        schedule = AgentSchedule(
            agent_id=agent.id,
            name="Manual overloaded schedule",
            instruction="Run later",
            cron_expr="0 9 * * *",
            is_enabled=False,
            run_count=0,
            created_by=actor.id,
            execution_user_id=actor.id,
        )
        db.add(schedule)
        await db.commit()

    async def _deferred(*_args):
        return ScheduleExecutionOutcome.RETRYABLE

    monkeypatch.setattr("app.services.scheduler._execute_schedule", _deferred)
    await _execute_and_track_schedule(
        schedule.id,
        agent.id,
        schedule.instruction,
        actor.id,
    )

    async with async_session() as db:
        stored = await db.get(AgentSchedule, schedule.id)
        assert stored is not None
        assert stored.run_count == 0
        assert stored.last_run_at is None


async def test_task_capacity_overload_returns_task_to_retryable_state(monkeypatch):
    """Capacity pressure must not leave a task doing or mark it failed/done."""

    from contextlib import asynccontextmanager

    from app.services.active_turns import reset_active_turns_for_testing
    from app.services.task_executor import execute_task
    from app.services.workload_capacity import (
        CapacityDimension,
        WorkloadOverloadedError,
    )

    class _OverloadedCapacity:
        @asynccontextmanager
        async def slot(self, kind, tenant_key):
            raise WorkloadOverloadedError(
                kind=kind,
                tenant_id=str(tenant_key),
                timeout_seconds=0.01,
                blocked_by=(CapacityDimension.CATEGORY,),
            )
            yield  # pragma: no cover

    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Task Overload {suffix}", slug=f"task-overload-{suffix}")
        db.add(tenant)
        await db.flush()
        actor = await _user(db, tenant.id, f"task_overload_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=actor.id,
            name=f"Task Overload Agent {suffix}",
            access_mode="private",
            status="running",
        )
        db.add(agent)
        await db.flush()
        task = Task(
            agent_id=agent.id,
            title="Retry after overload",
            type="todo",
            status="pending",
            priority="medium",
            created_by=actor.id,
            execution_user_id=actor.id,
        )
        db.add(task)
        await db.commit()

    monkeypatch.setattr(
        "app.services.task_executor.get_workload_capacity",
        lambda: _OverloadedCapacity(),
    )
    await reset_active_turns_for_testing()
    try:
        await execute_task(task.id, agent.id, actor.id)
    finally:
        await reset_active_turns_for_testing()

    async with async_session() as db:
        stored = await db.get(Task, task.id)
        assert stored is not None
        assert stored.status == "pending"
        assert stored.completed_at is None
        logs = (
            (await db.execute(select(TaskLog).where(TaskLog.task_id == task.id).order_by(TaskLog.created_at)))
            .scalars()
            .all()
        )
        assert any("可重新执行" in log.content for log in logs)
        assert not any("执行出错" in log.content for log in logs)


async def test_manual_run_rejects_running_or_ambiguous_tasks(monkeypatch):
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Manual Guard {suffix}", slug=f"manual-guard-{suffix}")
        db.add(tenant)
        await db.flush()
        actor = await _user(db, tenant.id, f"guard_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=actor.id,
            name=f"Manual Guard Agent {suffix}",
            access_mode="private",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        doing = Task(
            agent_id=agent.id,
            title="Already running",
            type="todo",
            status="doing",
            priority="medium",
            created_by=actor.id,
            execution_user_id=actor.id,
        )
        duplicate_a = Task(
            agent_id=agent.id,
            title="Duplicate",
            type="todo",
            status="pending",
            priority="medium",
            created_by=actor.id,
            execution_user_id=actor.id,
        )
        duplicate_b = Task(
            agent_id=agent.id,
            title="Duplicate",
            type="todo",
            status="pending",
            priority="medium",
            created_by=actor.id,
            execution_user_id=actor.id,
        )
        db.add_all([doing, duplicate_a, duplicate_b])
        await db.commit()

    async def _unexpected(*_args):
        pytest.fail("executor must not start")

    monkeypatch.setattr("app.services.task_executor.execute_task", _unexpected)
    async with async_session() as db:
        with pytest.raises(BackgroundManualRunConflict, match="already running"):
            await run_background_resource(
                db,
                actor_user_id=actor.id,
                agent_id=agent.id,
                resource_type="task",
                resource=str(doing.id),
            )
    async with async_session() as db:
        with pytest.raises(BackgroundManualRunConflict, match="Multiple task"):
            await run_background_resource(
                db,
                actor_user_id=actor.id,
                agent_id=agent.id,
                resource_type="task",
                resource="Duplicate",
            )


@pytest.mark.parametrize("human_channel", ["web", "mcp"])
async def test_agent_tool_manual_run_requires_a_real_human_turn(
    monkeypatch,
    human_channel,
):
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Manual Tool {suffix}", slug=f"manual-tool-{suffix}")
        db.add(tenant)
        await db.flush()
        actor = await _user(db, tenant.id, f"manual_tool_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=actor.id,
            name=f"Manual Tool Agent {suffix}",
            access_mode="private",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        task = Task(
            agent_id=agent.id,
            title="Human-only manual task",
            type="todo",
            status="pending",
            priority="medium",
            created_by=actor.id,
            execution_user_id=actor.id,
        )
        human_session = ChatSession(
            agent_id=agent.id,
            user_id=actor.id,
            title="Human session",
            source_channel=human_channel,
        )
        background_session = ChatSession(
            agent_id=agent.id,
            title="Trigger session",
            source_channel="trigger",
            external_conv_id=f"trigger:{suffix}",
        )
        db.add_all([task, human_session, background_session])
        await db.flush()
        human_anchor = ChatMessage(
            agent_id=agent.id,
            user_id=actor.id,
            sender_user_id=actor.id,
            role="user",
            content="run it",
            conversation_id=str(human_session.id),
        )
        background_anchor = ChatMessage(
            agent_id=agent.id,
            user_id=actor.id,
            sender_user_id=actor.id,
            role="user",
            content="background event",
            conversation_id=str(background_session.id),
            message_meta={"trigger_execution_id": str(uuid.uuid4())},
        )
        db.add_all([human_anchor, background_anchor])
        await db.commit()

    calls = []

    async def _capture(*args):
        calls.append(args)

    monkeypatch.setattr("app.services.task_executor.execute_task", _capture)
    accepted = await handle_run_background_resource(
        agent.id,
        actor.id,
        str(human_session.id),
        human_anchor.id,
        {"resource_type": "task", "resource": str(task.id)},
    )
    denied = await handle_run_background_resource(
        agent.id,
        actor.id,
        str(background_session.id),
        background_anchor.id,
        {"resource_type": "task", "resource": str(task.id)},
    )
    await asyncio.sleep(0)

    assert accepted.startswith("✅")
    assert "human interactive session" in denied
    assert len(calls) == 1


async def test_task_executor_atomically_skips_duplicate_runs(monkeypatch):
    from app.services.active_turns import reset_active_turns_for_testing
    from app.services.task_executor import execute_task

    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"Task Claim {suffix}", slug=f"task-claim-{suffix}")
        db.add(tenant)
        await db.flush()
        actor = await _user(db, tenant.id, f"task_claim_{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=actor.id,
            name=f"Task Claim Agent {suffix}",
            access_mode="private",
            status="idle",
        )
        db.add(agent)
        await db.flush()
        task = Task(
            agent_id=agent.id,
            title="Claim once",
            type="todo",
            status="pending",
            priority="medium",
            created_by=actor.id,
            execution_user_id=actor.id,
        )
        db.add(task)
        await db.commit()

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def _context(*_args):
        return "static", "dynamic"

    async def _llm(**_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "done"

    async def _activity(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.agent_context.build_agent_context", _context)
    monkeypatch.setattr("app.services.llm.call_agent_llm_with_tools", _llm)
    monkeypatch.setattr("app.services.activity_logger.log_activity", _activity)

    await reset_active_turns_for_testing()
    worker = asyncio.create_task(execute_task(task.id, agent.id, actor.id))
    try:
        await entered.wait()
        await asyncio.wait_for(execute_task(task.id, agent.id, actor.id), timeout=1)
        assert calls == 1
    finally:
        release.set()
        await worker
        await reset_active_turns_for_testing()
