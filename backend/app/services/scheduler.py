"""Lightweight asyncio scheduler for agent cron jobs.

Runs as a background task inside the FastAPI process.
Every 30 seconds, checks for schedules whose next_run_at <= now
and executes them by calling the LLM with the schedule's instruction.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from croniter import croniter
from loguru import logger
from sqlalchemy import select

from app.services.project_runtime_boundary import (
    is_project_agent,
    project_agent_runtime_allows,
)
from app.services.workload_capacity import (
    WorkloadKind,
    WorkloadOverloadedError,
    get_workload_capacity,
)


class ScheduleExecutionOutcome(StrEnum):
    """Result of one schedule attempt, including admission deferral."""

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    RETRYABLE = "retryable"


@dataclass(frozen=True, slots=True)
class _DueSchedule:
    """Immutable dispatch data returned after the claim transaction closes."""

    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    instruction: str
    execution_user_id: uuid.UUID | None
    model_id: uuid.UUID | None
    temperature: float | None
    reasoning_effort: str | None
    soul: bool
    memory: bool
    occurrence_at: datetime
    claimed_at: datetime
    previous_last_run_at: datetime | None
    previous_run_count: int
    next_run_at: datetime | None


def compute_next_run(cron_expr: str, after: datetime | None = None) -> datetime | None:
    """Compute the next run time from a cron expression."""
    try:
        base = after or datetime.now(UTC)
        cron = croniter(cron_expr, base)
        return cron.get_next(datetime).replace(tzinfo=UTC)
    except Exception as e:  # noqa: BLE001 - invalid cron input is non-fatal
        logger.error(f"Invalid cron expression '{cron_expr}': {e}")
        return None


async def _execute_schedule(
    schedule_id: uuid.UUID,
    agent_id: uuid.UUID,
    instruction: str,
    execution_user_id: uuid.UUID | None = None,
    model_id: uuid.UUID | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    soul: bool = True,
    memory: bool = True,
) -> ScheduleExecutionOutcome:
    """Execute a single schedule by calling the LLM with the instruction."""
    try:
        from app.database import async_session
        from app.models.agent import Agent

        # Load only the immutable prompt inputs in a short read session.  The
        # storage/context builder and provider call may take minutes and must
        # never inherit this transaction or its checked-out connection.
        async with async_session() as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent:
                logger.warning(f"Schedule {schedule_id}: agent {agent_id} not found")
                return ScheduleExecutionOutcome.SKIPPED

            if agent.status != "running":
                logger.info(f"Schedule {schedule_id}: agent {agent.name} not running, skipping")
                return ScheduleExecutionOutcome.SKIPPED

            from app.core.permissions import is_agent_expired

            if is_agent_expired(agent):
                logger.info(f"Schedule {schedule_id}: agent {agent.name} has expired, skipping")
                return ScheduleExecutionOutcome.SKIPPED

            project_scoped_agent = is_project_agent(agent)
            if project_scoped_agent:
                try:
                    project_running = await project_agent_runtime_allows(db, agent)
                except Exception as exc:  # noqa: BLE001 - retry project-only failure
                    logger.warning(
                        "Schedule {}: project runtime check failed, deferring: {}",
                        schedule_id,
                        exc,
                    )
                    return ScheduleExecutionOutcome.RETRYABLE
                if not project_running:
                    logger.info(f"Schedule {schedule_id}: project runtime is paused, deferring")
                    return ScheduleExecutionOutcome.RETRYABLE

            agent_name = agent.name
            role_description = agent.role_description or ""
            tenant_key = (
                getattr(agent, "company_id", None) or getattr(agent, "tenant_id", None) or execution_user_id or agent_id
            )

        from app.services.agent_context import build_agent_context
        from app.services.llm import call_agent_llm_with_tools

        # Admission can queue for a bounded period, so it must happen only
        # after the agent read session has returned its connection.
        async with get_workload_capacity().slot(WorkloadKind.SCHEDULED, tenant_key):
            if project_scoped_agent:
                async with async_session() as db:
                    current_agent = await db.get(Agent, agent_id)
                    try:
                        project_running = current_agent is not None and await project_agent_runtime_allows(
                            db,
                            current_agent,
                        )
                    except Exception as exc:  # noqa: BLE001 - retry project-only failure
                        logger.warning(
                            "Schedule {}: project runtime recheck failed, deferring: {}",
                            schedule_id,
                            exc,
                        )
                        return ScheduleExecutionOutcome.RETRYABLE
                    if not project_running:
                        logger.info(f"Schedule {schedule_id}: project paused while waiting for capacity, deferring")
                        return ScheduleExecutionOutcome.RETRYABLE
            context_options = {}
            if not soul:
                context_options["include_soul"] = False
            if not memory:
                context_options["include_memory"] = False
            static_prompt, dynamic_prompt = await build_agent_context(
                agent_id,
                agent_name,
                role_description,
                **context_options,
            )
            system_prompt = f"{static_prompt}\n\n{dynamic_prompt}"

            user_prompt = f"[自动调度任务] {instruction}"

            # A newly-created AsyncSession has no checked-out connection.  The
            # caller snapshots model configuration and commits its read phase
            # before provider I/O; tool implementations use short sessions.
            async with async_session() as db:
                reply = await call_agent_llm_with_tools(
                    db=db,
                    agent_id=agent_id,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    max_rounds=50,
                    session_id=str(schedule_id),
                    execution_user_id=execution_user_id,
                    turn_type="schedule",
                    model_override_id=model_id,
                    temperature_override=temperature,
                    reasoning_effort_override=reasoning_effort,
                )

            from app.services.llm.failure_outcome import llm_failure_code

            failure_code = llm_failure_code(reply)

            from app.services.activity_logger import log_activity

            await log_activity(
                agent_id,
                "schedule_run",
                f"定时任务执行: {instruction[:60]}",
                detail={
                    "schedule_id": str(schedule_id),
                    "instruction": instruction,
                    "reply": reply[:500],
                    "status": "failed" if failure_code else "completed",
                    **({"error_code": failure_code} if failure_code else {}),
                },
            )

            logger.info(f"Schedule {schedule_id} executed for agent {agent_name}: {reply[:80]}")
            return (
                ScheduleExecutionOutcome.FAILED
                if failure_code
                else ScheduleExecutionOutcome.SUCCEEDED
            )

    except WorkloadOverloadedError as e:
        logger.warning(f"Schedule {schedule_id} deferred by workload capacity: {e}")
        return ScheduleExecutionOutcome.RETRYABLE
    except Exception as e:  # noqa: BLE001 - one schedule must not stop the daemon
        logger.exception(f"Schedule {schedule_id} execution error: {e}")
        return ScheduleExecutionOutcome.FAILED


async def _claim_due_schedules_for_scope(
    now: datetime,
    *,
    project_agents: bool,
) -> tuple[_DueSchedule, ...]:
    """Claim one Agent scope without coupling standard work to projects."""

    from app.database import async_session
    from app.models.agent import Agent
    from app.models.schedule import AgentSchedule

    async with async_session() as db:
        statement = (
            select(AgentSchedule)
            .join(Agent, Agent.id == AgentSchedule.agent_id)
            .where(
                AgentSchedule.is_enabled.is_(True),
                AgentSchedule.next_run_at <= now,
            )
        )
        if project_agents:
            from app.models.project import Project

            statement = statement.join(Project, Project.id == Agent.project_id).where(
                Agent.scope == "project",
                Project.status == "running",
            )
        else:
            statement = statement.where(Agent.scope != "project")

        statement = statement.order_by(
            AgentSchedule.next_run_at,
            AgentSchedule.id,
        ).with_for_update(skip_locked=True, of=AgentSchedule)
        result = await db.execute(statement)
        due_schedules = result.scalars().all()
        claimed: list[_DueSchedule] = []
        for schedule in due_schedules:
            occurrence_at = schedule.next_run_at
            previous_last_run_at = schedule.last_run_at
            previous_run_count = schedule.run_count or 0
            next_run = compute_next_run(schedule.cron_expr, now)
            schedule.last_run_at = now
            schedule.next_run_at = next_run
            schedule.run_count = (schedule.run_count or 0) + 1
            claimed.append(
                _DueSchedule(
                    id=schedule.id,
                    agent_id=schedule.agent_id,
                    name=schedule.name,
                    instruction=schedule.instruction,
                    execution_user_id=schedule.execution_user_id,
                    model_id=getattr(schedule, "model_id", None),
                    temperature=getattr(schedule, "temperature", None),
                    reasoning_effort=getattr(schedule, "reasoning_effort", None),
                    soul=getattr(schedule, "soul", True),
                    memory=getattr(schedule, "memory", True),
                    occurrence_at=occurrence_at,
                    claimed_at=now,
                    previous_last_run_at=previous_last_run_at,
                    previous_run_count=previous_run_count,
                    next_run_at=next_run,
                )
            )
        await db.commit()

    return tuple(claimed)


async def _claim_due_schedules(now: datetime) -> tuple[_DueSchedule, ...]:
    """Claim standard work even when the optional project runtime is broken."""

    standard = await _claim_due_schedules_for_scope(now, project_agents=False)
    try:
        project = await _claim_due_schedules_for_scope(now, project_agents=True)
    except Exception as exc:  # noqa: BLE001 - isolate optional project runtime
        logger.error("Project schedule claim failed; standard schedules remain available: {}", exc)
        project = ()
    return tuple(
        sorted(
            (*standard, *project),
            key=lambda schedule: (schedule.occurrence_at, str(schedule.id)),
        )
    )


async def _release_schedule_occurrence(schedule: _DueSchedule) -> bool:
    """Return an admission-deferred occurrence to the durable due queue."""

    from app.database import async_session
    from app.models.schedule import AgentSchedule

    async with async_session() as db:
        stored = await db.scalar(select(AgentSchedule).where(AgentSchedule.id == schedule.id).with_for_update())
        if stored is None:
            return False
        if stored.last_run_at != schedule.claimed_at or stored.next_run_at != schedule.next_run_at:
            return False
        stored.last_run_at = schedule.previous_last_run_at
        stored.next_run_at = schedule.occurrence_at
        stored.run_count = schedule.previous_run_count
        await db.commit()
        return True


async def _execute_claimed_schedule(schedule: _DueSchedule) -> None:
    """Execute a claimed occurrence and requeue it after admission timeout."""

    outcome = await _execute_schedule(
        schedule.id,
        schedule.agent_id,
        schedule.instruction,
        schedule.execution_user_id,
        schedule.model_id,
        schedule.temperature,
        schedule.reasoning_effort,
        schedule.soul,
        schedule.memory,
    )
    if outcome is ScheduleExecutionOutcome.RETRYABLE:
        released = await _release_schedule_occurrence(schedule)
        if released:
            logger.info(f"Schedule '{schedule.name}' occurrence returned to the due queue")
        else:
            logger.warning(f"Schedule '{schedule.name}' changed after claim; deferred occurrence was not restored")


async def _tick():
    """One scheduler tick: claim due schedules, then dispatch without a DB lease."""
    from app.services.audit_logger import write_audit_log

    now = datetime.now(UTC)

    try:
        due_schedules = await _claim_due_schedules(now)

        if due_schedules:
            await write_audit_log(
                "schedule_tick",
                {"due_count": len(due_schedules)},
            )

        for schedule in due_schedules:
            await write_audit_log(
                "schedule_fire",
                {
                    "schedule_id": str(schedule.id),
                    "name": schedule.name,
                    "instruction": schedule.instruction[:100],
                    "next_run": str(schedule.next_run_at),
                },
                agent_id=schedule.agent_id,
            )

            asyncio.create_task(_execute_claimed_schedule(schedule))
            logger.info(f"Triggered schedule '{schedule.name}' (next: {schedule.next_run_at})")

    except Exception as e:  # noqa: BLE001 - the scheduler loop must remain alive
        logger.exception(f"Scheduler tick error: {e}")
        await write_audit_log("schedule_error", {"error": str(e)[:300]})


async def start_scheduler():
    """Start the background scheduler loop. Call from FastAPI startup."""
    logger.info("🕐 Agent scheduler started (30s interval)")
    while True:
        await _tick()
        await asyncio.sleep(30)
