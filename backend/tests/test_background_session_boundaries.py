"""Regression tests for short-lived database sessions in background turns."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, patch

from app.services.workload_capacity import (
    CapacityDimension,
    WorkloadKind,
    WorkloadOverloadedError,
)


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[object]:
        return list(self._value)  # type: ignore[arg-type]


class _FakeSession:
    """Small AsyncSession stand-in that exposes transaction/close state."""

    def __init__(
        self,
        *,
        execute_value: object | None = None,
        scalar_value: object | None = None,
    ) -> None:
        self.execute_value = execute_value
        self.scalar_value = scalar_value
        self.transaction_open = False
        self.closed = False
        self.commits = 0
        self.added: list[object] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.transaction_open = False
        self.closed = True

    async def execute(self, _statement: object) -> _ScalarResult:
        self.transaction_open = True
        return _ScalarResult(self.execute_value)

    async def scalar(self, _statement: object) -> object | None:
        self.transaction_open = True
        return self.scalar_value

    async def get(self, _model: object, _identity: object) -> object | None:
        self.transaction_open = True
        return self.execute_value

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()

    async def commit(self) -> None:
        self.commits += 1
        self.transaction_open = False

    def in_transaction(self) -> bool:
        return self.transaction_open


class _SessionFactory:
    def __init__(self, sessions: list[_FakeSession]) -> None:
        self.sessions = sessions
        self.index = 0

    def __call__(self) -> _FakeSession:
        session = self.sessions[self.index]
        self.index += 1
        return session


class _RecordingCapacity:
    def __init__(self, assert_released) -> None:
        self.assert_released = assert_released
        self.calls: list[tuple[WorkloadKind, object]] = []

    @asynccontextmanager
    async def slot(self, kind: WorkloadKind, tenant_key: object):
        self.assert_released()
        self.calls.append((kind, tenant_key))
        yield


class _OverloadedCapacity:
    @asynccontextmanager
    async def slot(self, kind: WorkloadKind, tenant_key: object):
        raise WorkloadOverloadedError(
            kind=kind,
            tenant_id=str(tenant_key),
            timeout_seconds=1,
            blocked_by=(CapacityDimension.CATEGORY,),
        )
        yield  # pragma: no cover - required for an async context manager


async def test_schedule_releases_read_session_before_context_and_llm() -> None:
    """A slow scheduled model turn starts without the agent read transaction."""

    from app.services.scheduler import _execute_schedule

    schedule_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    model_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Schedule Agent",
        role_description="Run scheduled work",
        status="running",
        tenant_id=tenant_id,
    )
    read_session = _FakeSession(execute_value=agent)
    llm_session = _FakeSession()
    sessions = [read_session, llm_session]
    context_checked = False
    llm_checked = False
    capacity = _RecordingCapacity(lambda: assert_sessions_released([read_session]))

    async def build_context(*_args: object, **options: object) -> tuple[str, str]:
        nonlocal context_checked
        assert read_session.closed
        assert not read_session.in_transaction()
        assert options == {"include_soul": False, "include_memory": False}
        context_checked = True
        return "static", "dynamic"

    async def call_llm(**kwargs: object) -> str:
        nonlocal llm_checked
        db = kwargs["db"]
        assert db is llm_session
        assert not llm_session.in_transaction()
        assert read_session.closed
        assert kwargs["model_override_id"] == model_id
        assert kwargs["temperature_override"] == 1.1
        assert kwargs["reasoning_effort_override"] == "high"
        llm_checked = True
        return "scheduled result"

    with (
        patch("app.database.async_session", _SessionFactory(sessions)),
        patch("app.core.permissions.is_agent_expired", return_value=False),
        patch(
            "app.services.scheduler.get_workload_capacity",
            return_value=capacity,
        ),
        patch(
            "app.services.agent_context.build_agent_context",
            side_effect=build_context,
        ),
        patch("app.services.llm.call_agent_llm_with_tools", side_effect=call_llm),
        patch(
            "app.services.activity_logger.log_activity",
            new=AsyncMock(),
        ) as activity_log,
    ):
        await _execute_schedule(
            schedule_id,
            agent_id,
            "prepare report",
            owner_id,
            model_id=model_id,
            temperature=1.1,
            reasoning_effort="high",
            soul=False,
            memory=False,
        )

    assert context_checked
    assert llm_checked
    assert llm_session.closed
    assert capacity.calls == [(WorkloadKind.SCHEDULED, tenant_id)]
    activity_log.assert_awaited_once()


async def test_schedule_capacity_timeout_is_reported_as_retryable() -> None:
    """Scheduled admission overload is distinguishable from terminal failure."""

    from app.services.scheduler import ScheduleExecutionOutcome, _execute_schedule

    schedule_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Deferred Schedule Agent",
        role_description="Run scheduled work",
        status="running",
        tenant_id=tenant_id,
    )
    read_session = _FakeSession(execute_value=agent)

    with (
        patch("app.database.async_session", _SessionFactory([read_session])),
        patch("app.core.permissions.is_agent_expired", return_value=False),
        patch(
            "app.services.scheduler.get_workload_capacity",
            return_value=_OverloadedCapacity(),
        ),
        patch(
            "app.services.agent_context.build_agent_context",
            new=AsyncMock(),
        ) as build_context,
    ):
        outcome = await _execute_schedule(
            schedule_id,
            agent_id,
            "prepare report",
            owner_id,
        )

    assert read_session.closed
    assert outcome is ScheduleExecutionOutcome.RETRYABLE
    build_context.assert_not_awaited()


async def test_task_releases_snapshots_before_context_and_llm() -> None:
    """A task model turn does not inherit task or agent query transactions."""

    from app.services.task_executor import execute_task

    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    model_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        execution_user_id=owner_id,
        created_by=owner_id,
        title="Capacity validation",
        description="Run without pinning the database pool",
        type="todo",
        status="pending",
        completed_at=None,
        model_id=model_id,
        temperature=1.4,
        reasoning_effort="none",
        soul=False,
        memory=False,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Task Agent",
        role_description="Execute background work",
        tenant_id=tenant_id,
    )
    task_transition = _FakeSession(execute_value=task)
    run_snapshot = _FakeSession(scalar_value=owner_id)
    agent_read = _FakeSession(execute_value=agent)
    llm_session = _FakeSession()
    result_persist = _FakeSession(execute_value=task)
    sessions = [
        task_transition,
        run_snapshot,
        agent_read,
        llm_session,
        result_persist,
    ]
    context_checked = False
    llm_checked = False
    capacity = _RecordingCapacity(lambda: assert_sessions_released([task_transition, run_snapshot, agent_read]))

    async def build_context(*_args: object, **options: object) -> tuple[str, str]:
        nonlocal context_checked
        assert task_transition.closed
        assert run_snapshot.closed
        assert agent_read.closed
        assert options == {"include_soul": False, "include_memory": False}
        context_checked = True
        return "static", "dynamic"

    async def call_llm(**kwargs: object) -> str:
        nonlocal llm_checked
        assert kwargs["db"] is llm_session
        assert not llm_session.in_transaction()
        assert all(session.closed for session in sessions[:3])
        assert kwargs["model_override_id"] == model_id
        assert kwargs["temperature_override"] == 1.4
        assert kwargs["reasoning_effort_override"] == "none"
        llm_checked = True
        return "task result"

    with (
        patch(
            "app.services.task_executor.async_session",
            _SessionFactory(sessions),
        ),
        patch(
            "app.services.active_turns.ensure_active_turn",
            new=AsyncMock(),
        ),
        patch(
            "app.services.task_executor.get_workload_capacity",
            return_value=capacity,
        ),
        patch(
            "app.services.agent_context.build_agent_context",
            side_effect=build_context,
        ),
        patch("app.services.llm.call_agent_llm_with_tools", side_effect=call_llm),
        patch(
            "app.services.activity_logger.log_activity",
            new=AsyncMock(),
        ) as activity_log,
    ):
        await execute_task(task_id, agent_id, owner_id)

    assert context_checked
    assert llm_checked
    assert llm_session.closed
    assert result_persist.closed
    assert task.status == "done"
    assert capacity.calls == [(WorkloadKind.BACKGROUND, tenant_id)]
    activity_log.assert_awaited_once()


async def test_due_schedule_claim_commits_before_returning_dispatch_data() -> None:
    """The scheduler claim returns detached values after releasing row locks."""

    from app.services.scheduler import _claim_due_schedules

    now = datetime.now(UTC)
    schedule = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="Daily brief",
        instruction="prepare brief",
        execution_user_id=uuid.uuid4(),
        cron_expr="0 9 * * *",
        last_run_at=None,
        next_run_at=now,
        run_count=0,
    )
    claim_session = _FakeSession(execute_value=[schedule])

    with patch("app.database.async_session", _SessionFactory([claim_session])):
        claimed = await _claim_due_schedules(now)

    assert claim_session.commits == 1
    assert claim_session.closed
    assert not claim_session.in_transaction()
    assert len(claimed) == 1
    assert claimed[0].id == schedule.id
    assert claimed[0].instruction == "prepare brief"
    assert claimed[0].occurrence_at == now
    assert claimed[0].previous_last_run_at is None
    assert claimed[0].previous_run_count == 0
    assert schedule.last_run_at == now
    assert schedule.run_count == 1


async def test_trigger_runtime_waits_for_capacity_after_identity_session_closes() -> None:
    """The extracted trigger invoker uses the scheduled lane without a DB lease."""

    from app.services.trigger_runtime.invoker import invoke_agent_for_triggers

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=owner_id,
    )
    trigger = SimpleNamespace(name="daily", type="cron", config={})
    identity_session = _FakeSession(execute_value=agent)
    unavailable_session = _FakeSession(execute_value=None)
    capacity = _RecordingCapacity(lambda: assert_sessions_released([identity_session]))

    with (
        patch(
            "app.services.trigger_runtime.invoker.async_session",
            _SessionFactory([identity_session, unavailable_session]),
        ),
        patch(
            "app.services.trigger_runtime.invoker.get_workload_capacity",
            return_value=capacity,
        ),
        patch(
            "app.core.okr_feature.partition_retired_okr_triggers",
            return_value=([], [trigger]),
        ),
    ):
        await invoke_agent_for_triggers(agent_id, [trigger])

    assert capacity.calls == [(WorkloadKind.SCHEDULED, tenant_id)]
    assert unavailable_session.closed


async def test_trigger_daemon_waits_for_capacity_after_identity_session_closes() -> None:
    """The durable trigger daemon queues outside its execution-identity session."""

    from app.services.trigger_daemon import _invoke_agent_for_triggers

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=owner_id,
    )
    trigger = SimpleNamespace(
        name="daily",
        type="cron",
        config={},
        execution_user_id=owner_id,
    )
    identity_session = _FakeSession(execute_value=agent)
    unavailable_session = _FakeSession(execute_value=None)
    capacity = _RecordingCapacity(lambda: assert_sessions_released([identity_session]))

    with (
        patch(
            "app.services.trigger_daemon.async_session",
            _SessionFactory([identity_session, unavailable_session]),
        ),
        patch(
            "app.services.trigger_daemon.get_workload_capacity",
            return_value=capacity,
        ),
        patch(
            "app.core.okr_feature.partition_retired_okr_triggers",
            return_value=([], [trigger]),
        ),
        patch(
            "app.services.execution_identity.resolve_execution_user_id",
            new=AsyncMock(return_value=owner_id),
        ),
    ):
        await _invoke_agent_for_triggers(agent_id, [trigger])

    assert capacity.calls == [(WorkloadKind.SCHEDULED, tenant_id)]
    assert unavailable_session.closed


async def test_on_message_trigger_delegates_one_scheduled_admission() -> None:
    """An on-message turn is admitted by its session wrapper exactly once."""

    from app.services.trigger_daemon import _invoke_agent_for_triggers

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    origin_session_id = uuid.uuid4()
    matched_message_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=owner_id,
    )
    trigger = SimpleNamespace(
        name="message",
        type="on_message",
        config={
            "_execution_id": str(execution_id),
            "_origin_session_id": str(origin_session_id),
            "_matched_message_id": str(matched_message_id),
        },
        execution_user_id=owner_id,
    )
    identity_session = _FakeSession(execute_value=agent)
    capacity = _RecordingCapacity(lambda: assert_sessions_released([identity_session]))

    with (
        patch(
            "app.services.trigger_daemon.async_session",
            _SessionFactory([identity_session]),
        ),
        patch(
            "app.services.trigger_daemon.get_workload_capacity",
            return_value=capacity,
        ),
        patch(
            "app.core.okr_feature.partition_retired_okr_triggers",
            return_value=([], [trigger]),
        ),
        patch(
            "app.services.execution_identity.resolve_execution_user_id",
            new=AsyncMock(return_value=owner_id),
        ),
        patch(
            "app.services.trigger_daemon._link_invocation_executions",
            new=AsyncMock(),
        ),
        patch(
            "app.services.trigger_daemon._resume_origin_session_for_on_message",
            new=AsyncMock(),
        ) as resume,
        patch(
            "app.services.trigger_daemon._finalize_invocation_executions",
            new=AsyncMock(),
        ),
    ):
        await _invoke_agent_for_triggers(agent_id, [trigger])

    assert capacity.calls == []
    resume.assert_awaited_once_with(
        agent_id,
        trigger,
        tenant_id=tenant_id,
    )


async def test_trigger_runtime_capacity_timeout_requeues_execution() -> None:
    """The extracted invoker keeps capacity timeouts retryable."""

    from app.services.trigger_runtime.invoker import invoke_agent_for_triggers

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=owner_id,
    )
    trigger = SimpleNamespace(
        name="daily",
        type="cron",
        config={"_execution_id": str(execution_id)},
    )
    identity_session = _FakeSession(execute_value=agent)

    with (
        patch(
            "app.services.trigger_runtime.invoker.async_session",
            _SessionFactory([identity_session]),
        ),
        patch(
            "app.services.trigger_runtime.invoker.get_workload_capacity",
            return_value=_OverloadedCapacity(),
        ),
        patch(
            "app.core.okr_feature.partition_retired_okr_triggers",
            return_value=([], [trigger]),
        ),
        patch(
            "app.services.trigger_runtime.invoker.requeue_trigger_executions",
            new=AsyncMock(),
        ) as requeue,
        patch(
            "app.services.trigger_runtime.invoker.mark_trigger_executions_failed",
            new=AsyncMock(),
        ) as fail,
    ):
        await invoke_agent_for_triggers(agent_id, [trigger])

    assert identity_session.closed
    requeue.assert_awaited_once()
    assert requeue.await_args.args[0] == [execution_id]
    fail.assert_not_awaited()


async def test_trigger_capacity_timeout_requeues_durable_execution() -> None:
    """Admission timeout keeps a leased trigger execution retryable."""

    from app.services.trigger_daemon import _invoke_agent_for_triggers

    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=owner_id,
    )
    trigger = SimpleNamespace(
        name="daily",
        type="cron",
        config={"_execution_id": str(execution_id)},
        execution_user_id=owner_id,
    )
    identity_session = _FakeSession(execute_value=agent)

    with (
        patch(
            "app.services.trigger_daemon.async_session",
            _SessionFactory([identity_session]),
        ),
        patch(
            "app.services.trigger_daemon.get_workload_capacity",
            return_value=_OverloadedCapacity(),
        ),
        patch(
            "app.core.okr_feature.partition_retired_okr_triggers",
            return_value=([], [trigger]),
        ),
        patch(
            "app.services.execution_identity.resolve_execution_user_id",
            new=AsyncMock(return_value=owner_id),
        ),
        patch(
            "app.services.trigger_daemon._finalize_invocation_executions",
            new=AsyncMock(),
        ) as finalize,
    ):
        await _invoke_agent_for_triggers(agent_id, [trigger])

    assert identity_session.closed
    finalize.assert_awaited_once()
    assert finalize.await_args.args[0] == [execution_id]
    assert finalize.await_args.args[3] is not None
    assert finalize.await_args.args[4] is True


def assert_sessions_released(sessions: list[_FakeSession]) -> None:
    """Assert capacity admission never observes an open snapshot transaction."""

    assert all(session.closed for session in sessions)
    assert not any(session.in_transaction() for session in sessions)
