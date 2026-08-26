"""Project runtime failures must remain isolated from standard Agent work."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.models.chat_compaction
import app.models.chat_session
import app.models.participant
import app.models.project  # noqa: F401 - register project schema for integration cases
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.project import Project
from app.models.schedule import AgentSchedule
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.user import Identity, User

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_cases():
    await engine.dispose()
    yield
    await engine.dispose()


class _OpenCapacity:
    @asynccontextmanager
    async def slot(self, *_args, **_kwargs):
        yield


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeSession:
    def __init__(self, *, get_value=None, execute_value=None):
        self.get_value = get_value
        self.execute_value = execute_value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args):
        return self.get_value

    async def execute(self, _statement):
        return _ScalarResult(self.execute_value)


class _SessionFactory:
    def __init__(self, sessions):
        self.sessions = iter(sessions)

    def __call__(self):
        return next(self.sessions)


class _RecordingCapacity:
    def __init__(self):
        self.entered = False

    @asynccontextmanager
    async def slot(self, *_args, **_kwargs):
        self.entered = True
        yield


async def _seed_standard_agent() -> tuple[User, Agent]:
    suffix = uuid.uuid4().hex[:12]
    async with async_session() as db:
        tenant = Tenant(
            name=f"Fault Isolation {suffix}",
            slug=f"fault-isolation-{suffix}",
        )
        identity = Identity(
            username=f"fault_{suffix}",
            email=f"fault_{suffix}@example.com",
            password_hash="test",
        )
        db.add_all([tenant, identity])
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name=f"Fault {suffix}",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            tenant_id=tenant.id,
            creator_id=user.id,
            name=f"Standard Agent {suffix}",
            access_mode="private",
            status="running",
            scope="standard",
        )
        db.add(agent)
        await db.commit()
        return user, agent


async def _seed_paused_project_agent() -> tuple[User, Agent, Project]:
    user, _standard_agent = await _seed_standard_agent()
    async with async_session() as db:
        project = Project(
            tenant_id=user.tenant_id,
            owner_user_id=user.id,
            execution_user_id=user.id,
            name=f"Paused Project {uuid.uuid4().hex[:8]}",
            status="paused",
        )
        db.add(project)
        await db.flush()
        agent_id = uuid.uuid4()
        agent = Agent(
            id=agent_id,
            tenant_id=user.tenant_id,
            creator_id=user.id,
            name=f"Paused Project Agent {uuid.uuid4().hex[:8]}",
            access_mode="private",
            status="running",
            scope="project",
            project_id=project.id,
            agent_dir=f".agents/{agent_id}",
        )
        db.add(agent)
        await db.commit()
        return user, agent, project


async def test_standard_schedule_executes_when_project_service_fails(monkeypatch):
    from app.services.scheduler import ScheduleExecutionOutcome, _execute_schedule

    user, agent = await _seed_standard_agent()
    broken_project_check = AsyncMock(side_effect=RuntimeError("projects relation unavailable"))
    monkeypatch.setattr(
        "app.services.project_service.project_runtime_allows_agent",
        broken_project_check,
    )
    monkeypatch.setattr(
        "app.services.scheduler.get_workload_capacity",
        lambda: _OpenCapacity(),
    )
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("static", "dynamic")),
    )
    monkeypatch.setattr(
        "app.services.llm.call_agent_llm_with_tools",
        AsyncMock(return_value="scheduled result"),
    )
    activity = AsyncMock()
    monkeypatch.setattr("app.services.activity_logger.log_activity", activity)

    outcome = await _execute_schedule(
        uuid.uuid4(),
        agent.id,
        "prepare report",
        user.id,
    )

    assert outcome is ScheduleExecutionOutcome.SUCCEEDED
    broken_project_check.assert_not_awaited()
    activity.assert_awaited_once()


async def test_standard_task_executes_when_project_service_fails(monkeypatch):
    from app.services.task_executor import execute_task

    user, agent = await _seed_standard_agent()
    async with async_session() as db:
        task = Task(
            agent_id=agent.id,
            title="Keep standard task available",
            description="Project checks are unavailable",
            type="todo",
            status="pending",
            priority="medium",
            created_by=user.id,
            execution_user_id=user.id,
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    broken_project_check = AsyncMock(side_effect=RuntimeError("projects relation unavailable"))
    monkeypatch.setattr(
        "app.services.project_service.project_runtime_allows_agent",
        broken_project_check,
    )
    monkeypatch.setattr(
        "app.services.task_executor.get_workload_capacity",
        lambda: _OpenCapacity(),
    )
    monkeypatch.setattr(
        "app.services.active_turns.ensure_active_turn",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("static", "dynamic")),
    )
    monkeypatch.setattr(
        "app.services.llm.call_agent_llm_with_tools",
        AsyncMock(return_value="task result"),
    )
    monkeypatch.setattr("app.services.activity_logger.log_activity", AsyncMock())

    await execute_task(task_id, agent.id, user.id)

    async with async_session() as db:
        stored = await db.get(Task, task_id)
        assert stored is not None
        assert stored.status == "done"
    broken_project_check.assert_not_awaited()


async def test_standard_manual_run_ignores_broken_project_service(monkeypatch):
    from app.services.background_manual_run import run_background_resource

    user, agent = await _seed_standard_agent()
    async with async_session() as db:
        task = Task(
            agent_id=agent.id,
            title="Manual standard task",
            type="todo",
            status="pending",
            priority="medium",
            created_by=user.id,
            execution_user_id=user.id,
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    broken_project_check = AsyncMock(side_effect=RuntimeError("projects relation unavailable"))
    monkeypatch.setattr(
        "app.services.project_service.project_runtime_allows_agent",
        broken_project_check,
    )
    executed = asyncio.Event()

    async def _capture_task(*_args):
        executed.set()

    monkeypatch.setattr("app.services.task_executor.execute_task", _capture_task)
    async with async_session() as db:
        result = await run_background_resource(
            db,
            actor_user_id=user.id,
            agent_id=agent.id,
            resource_type="task",
            resource=str(task_id),
        )

    await asyncio.wait_for(executed.wait(), timeout=1)
    assert result.resource_id == task_id
    broken_project_check.assert_not_awaited()


async def test_standard_schedule_claim_survives_project_query_failure(monkeypatch):
    import app.services.scheduler as scheduler

    user, agent = await _seed_standard_agent()
    now = datetime.now(UTC)
    async with async_session() as db:
        schedule = AgentSchedule(
            agent_id=agent.id,
            name="Due standard schedule",
            instruction="run",
            cron_expr="*/5 * * * *",
            is_enabled=True,
            next_run_at=now - timedelta(minutes=1),
            run_count=0,
            created_by=user.id,
            execution_user_id=user.id,
        )
        db.add(schedule)
        await db.commit()
        schedule_id = schedule.id

    original = scheduler._claim_due_schedules_for_scope

    async def _claim_with_failed_project(*args, project_agents: bool, **kwargs):
        if project_agents:
            raise RuntimeError("projects relation unavailable")
        return await original(*args, project_agents=False, **kwargs)

    monkeypatch.setattr(scheduler, "_claim_due_schedules_for_scope", _claim_with_failed_project)
    claimed = await scheduler._claim_due_schedules(now)

    assert schedule_id in {item.id for item in claimed}


async def test_standard_trigger_claim_survives_project_query_failure(monkeypatch):
    import app.services.trigger_runtime.executions as executions

    user, agent = await _seed_standard_agent()
    async with async_session() as db:
        trigger = AgentTrigger(
            agent_id=agent.id,
            created_by_user_id=user.id,
            execution_user_id=user.id,
            name=f"manual-{uuid.uuid4().hex[:8]}",
            type="cron",
            config={"expr": "0 9 * * *"},
            reason="fault isolation",
            is_enabled=True,
        )
        db.add(trigger)
        await db.flush()
        execution = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=agent.id,
            execution_user_id=user.id,
            source="manual",
            status="pending",
            idempotency_key=f"fault:{uuid.uuid4()}",
            scheduled_at=datetime.now(UTC),
        )
        db.add(execution)
        await db.commit()
        execution_id = execution.id

    original = executions._claim_pending_trigger_executions_for_scope

    async def _claim_with_failed_project(*, project_agents: bool, **kwargs):
        if project_agents:
            raise RuntimeError("projects relation unavailable")
        return await original(project_agents=False, **kwargs)

    monkeypatch.setattr(
        executions,
        "_claim_pending_trigger_executions_for_scope",
        _claim_with_failed_project,
    )
    claimed = await executions.claim_pending_trigger_executions(sources=["manual"])

    assert execution_id in {execution.id for execution, _trigger in claimed}


async def test_standard_trigger_catalog_survives_project_query_failure(monkeypatch):
    import app.services.trigger_daemon as trigger_daemon

    user, agent = await _seed_standard_agent()
    async with async_session() as db:
        trigger = AgentTrigger(
            agent_id=agent.id,
            created_by_user_id=user.id,
            execution_user_id=user.id,
            name=f"catalog-{uuid.uuid4().hex[:8]}",
            type="cron",
            config={"expr": "0 9 * * *"},
            reason="fault isolation",
            is_enabled=True,
        )
        db.add(trigger)
        await db.commit()
        trigger_id = trigger.id

    original = trigger_daemon._load_enabled_triggers_for_scope

    async def _load_with_failed_project(*, project_agents: bool):
        if project_agents:
            raise RuntimeError("projects relation unavailable")
        return await original(project_agents=False)

    monkeypatch.setattr(
        trigger_daemon,
        "_load_enabled_triggers_for_scope",
        _load_with_failed_project,
    )
    loaded = await trigger_daemon._load_enabled_triggers()

    assert trigger_id in {trigger.id for trigger in loaded}


async def test_project_runtime_boundary_still_blocks_project_agents(monkeypatch):
    from app.services.project_runtime_boundary import project_agent_runtime_allows

    project_check = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "app.services.project_service.project_runtime_allows_agent",
        project_check,
    )
    project_agent = SimpleNamespace(
        id=uuid.uuid4(),
        scope="project",
        project_id=uuid.uuid4(),
    )

    assert not await project_agent_runtime_allows(SimpleNamespace(), project_agent)
    project_check.assert_awaited_once()


async def test_standard_trigger_invocation_survives_broken_project_check(monkeypatch):
    from app.services.trigger_daemon import _invoke_agent_for_triggers

    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    origin_session_id = uuid.uuid4()
    matched_message_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    standard_agent = SimpleNamespace(
        id=agent_id,
        scope="standard",
        tenant_id=uuid.uuid4(),
        creator_id=owner_id,
        is_expired=False,
    )
    trigger = SimpleNamespace(
        name="standard-trigger",
        type="on_message",
        config={
            "_origin_session_id": str(origin_session_id),
            "_matched_message_id": str(matched_message_id),
            "_execution_id": str(execution_id),
        },
        execution_user_id=owner_id,
    )
    broken_project_check = AsyncMock(side_effect=RuntimeError("project runtime unavailable"))
    resume = AsyncMock()
    finalize = AsyncMock()
    monkeypatch.setattr(
        "app.services.project_service.project_runtime_allows_agent",
        broken_project_check,
    )
    monkeypatch.setattr(
        "app.services.trigger_daemon.async_session",
        _SessionFactory([_FakeSession(get_value=standard_agent)]),
    )
    monkeypatch.setattr(
        "app.core.okr_feature.partition_retired_okr_triggers",
        lambda triggers: ([], triggers),
    )
    monkeypatch.setattr(
        "app.services.execution_identity.resolve_execution_user_id",
        AsyncMock(return_value=owner_id),
    )
    monkeypatch.setattr(
        "app.services.trigger_daemon._link_invocation_executions",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.trigger_daemon._resume_origin_session_for_on_message",
        resume,
    )
    monkeypatch.setattr(
        "app.services.trigger_daemon._finalize_invocation_executions",
        finalize,
    )

    await _invoke_agent_for_triggers(agent_id, [trigger])

    resume.assert_awaited_once()
    finalize.assert_awaited_once()
    assert finalize.await_args.args[0] == [execution_id]
    assert finalize.await_args.args[3] is None
    broken_project_check.assert_not_awaited()


async def test_paused_project_trigger_is_requeued_before_capacity(monkeypatch):
    from app.services.trigger_runtime.invoker import invoke_agent_for_triggers

    agent_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    project_agent = SimpleNamespace(
        id=agent_id,
        scope="project",
        project_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        creator_id=uuid.uuid4(),
    )
    trigger = SimpleNamespace(
        name="project-trigger",
        type="cron",
        config={"_execution_id": str(execution_id)},
    )
    capacity = _RecordingCapacity()
    project_check = AsyncMock(return_value=False)
    requeue = AsyncMock()
    monkeypatch.setattr(
        "app.services.project_service.project_runtime_allows_agent",
        project_check,
    )
    monkeypatch.setattr(
        "app.services.trigger_runtime.invoker.async_session",
        _SessionFactory([_FakeSession(get_value=project_agent)]),
    )
    monkeypatch.setattr(
        "app.services.trigger_runtime.invoker.get_workload_capacity",
        lambda: capacity,
    )
    monkeypatch.setattr(
        "app.core.okr_feature.partition_retired_okr_triggers",
        lambda triggers: ([], triggers),
    )
    monkeypatch.setattr(
        "app.services.trigger_runtime.invoker.requeue_trigger_executions",
        requeue,
    )

    await invoke_agent_for_triggers(agent_id, [trigger])

    project_check.assert_awaited_once()
    requeue.assert_awaited_once_with(
        [execution_id],
        "Project runtime is paused",
    )
    assert not capacity.entered


async def test_paused_project_agent_is_blocked_across_background_paths(monkeypatch):
    import app.services.scheduler as scheduler
    import app.services.trigger_daemon as trigger_daemon
    import app.services.trigger_runtime.executions as executions
    from app.services.background_manual_run import (
        BackgroundManualRunConflict,
        run_background_resource,
    )

    user, agent, _project = await _seed_paused_project_agent()
    now = datetime.now(UTC)
    async with async_session() as db:
        task = Task(
            agent_id=agent.id,
            title="Paused task",
            type="todo",
            status="pending",
            priority="medium",
            created_by=user.id,
            execution_user_id=user.id,
        )
        schedule = AgentSchedule(
            agent_id=agent.id,
            name="Paused schedule",
            instruction="must not run",
            cron_expr="*/5 * * * *",
            is_enabled=True,
            next_run_at=now - timedelta(minutes=1),
            run_count=0,
            created_by=user.id,
            execution_user_id=user.id,
        )
        trigger = AgentTrigger(
            agent_id=agent.id,
            created_by_user_id=user.id,
            execution_user_id=user.id,
            name=f"paused-{uuid.uuid4().hex[:8]}",
            type="cron",
            config={"expr": "0 9 * * *"},
            reason="paused project",
            is_enabled=True,
        )
        db.add_all([task, schedule, trigger])
        await db.flush()
        execution = TriggerExecution(
            trigger_id=trigger.id,
            agent_id=agent.id,
            execution_user_id=user.id,
            source="manual",
            status="pending",
            idempotency_key=f"paused:{uuid.uuid4()}",
            scheduled_at=now,
        )
        db.add(execution)
        await db.commit()
        task_id = task.id
        schedule_id = schedule.id
        trigger_id = trigger.id
        execution_id = execution.id

    build_context = AsyncMock()
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        build_context,
    )
    outcome = await scheduler._execute_schedule(
        schedule_id,
        agent.id,
        "must not run",
        user.id,
    )
    assert outcome is scheduler.ScheduleExecutionOutcome.RETRYABLE
    build_context.assert_not_awaited()

    async with async_session() as db:
        with pytest.raises(BackgroundManualRunConflict, match="paused"):
            await run_background_resource(
                db,
                actor_user_id=user.id,
                agent_id=agent.id,
                resource_type="task",
                resource=str(task_id),
            )

    claimed_schedules = await scheduler._claim_due_schedules(now)
    assert schedule_id not in {item.id for item in claimed_schedules}

    claimed_executions = await executions.claim_pending_trigger_executions(
        sources=["manual"],
    )
    assert execution_id not in {claimed.id for claimed, _trigger in claimed_executions}

    loaded_triggers = await trigger_daemon._load_enabled_triggers()
    assert trigger_id not in {loaded.id for loaded in loaded_triggers}
