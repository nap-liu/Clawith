"""Cron admission: each claimed occurrence is one recoverable shared turn."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from croniter import croniter
from loguru import logger
from sqlalchemy import select

from app.core.permissions import is_agent_expired
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.schedule import AgentSchedule
from app.services.background_task_admission import create_background_turn, resource_execution_settings
from app.services.llm.failure_outcome import render_message
from app.services.project_runtime_boundary import is_project_agent, project_agent_runtime_allows


class ScheduleExecutionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"
    RETRYABLE = "retryable"


@dataclass(frozen=True, slots=True)
class _DueSchedule:
    id: uuid.UUID
    agent_id: uuid.UUID
    name: str
    instruction: str
    occurrence_at: datetime
    next_run_at: datetime | None
    anchor_id: uuid.UUID


def compute_next_run(cron_expr: str, after: datetime | None = None) -> datetime | None:
    try:
        return croniter(cron_expr, after or datetime.now(UTC)).get_next(datetime).replace(tzinfo=UTC)
    except Exception as exc:
        logger.error("Invalid cron expression '{}': {}", cron_expr, exc)
        return None


async def _schedule_agent_available(db, agent: Agent | None) -> bool:
    if agent is None or agent.status != "running" or is_agent_expired(agent):
        return False
    return not is_project_agent(agent) or await project_agent_runtime_allows(db, agent)


async def prepare_schedule_turn(
    db,
    schedule_id: uuid.UUID,
    agent_id: uuid.UUID,
    instruction: str,
    execution_user_id: uuid.UUID | None = None,
    model_id: uuid.UUID | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    soul: bool = True,
    memory: bool = True,
) -> ChatMessage | None:
    """Persist a manual occurrence in the caller's transaction."""
    agent = await db.get(Agent, agent_id)
    if not await _schedule_agent_available(db, agent):
        return None
    return await create_background_turn(
        db, agent=agent, kind="schedule", reference_id=uuid.uuid4(),
        user_prompt=render_message("background.scheduleInput").format(instruction=instruction),
        execution_user_id=execution_user_id,
        settings={
            "model_override_id": str(model_id) if model_id else None,
            "temperature_override": temperature,
            "reasoning_effort_override": reasoning_effort,
            "include_soul": soul,
            "include_memory": memory,
            "max_tool_rounds_override": 50,
        },
        completion={"schedule_id": str(schedule_id), "instruction": instruction, "manual": True},
    )


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
    """Accept a manual occurrence; its finalizer owns successful-run counters."""
    from app.services.background_turns import run_background_turn

    try:
        async with async_session() as db:
            anchor = await prepare_schedule_turn(
                db, schedule_id, agent_id, instruction, execution_user_id,
                model_id, temperature, reasoning_effort, soul, memory,
            )
            if anchor is None:
                return ScheduleExecutionOutcome.SKIPPED
            anchor_id = anchor.id
            await db.commit()
        await run_background_turn(anchor_id)
        async with async_session() as db:
            stored = await db.get(ChatMessage, anchor_id)
            status = (stored.message_meta or {}).get("turn_status") if stored else None
        if status == "completed":
            return ScheduleExecutionOutcome.SUCCEEDED
        if status in {"failed", "cancelled"}:
            return ScheduleExecutionOutcome.FAILED
        return ScheduleExecutionOutcome.RETRYABLE
    except Exception:
        logger.exception("Schedule {} dispatch interrupted; accepted turns remain recoverable", schedule_id)
        return ScheduleExecutionOutcome.RETRYABLE


async def _claim_due_schedules_for_scope(
    now: datetime, *, project_agents: bool,
) -> tuple[_DueSchedule, ...]:
    """Commit the occurrence before advancing its schedule in the same transaction."""
    async with async_session() as db:
        statement = select(AgentSchedule).join(Agent, Agent.id == AgentSchedule.agent_id).where(
            AgentSchedule.is_enabled.is_(True), AgentSchedule.next_run_at <= now,
        )
        if project_agents:
            from app.models.project import Project

            statement = statement.join(Project, Project.id == Agent.project_id).where(
                Agent.scope == "project", Project.status == "running",
            )
        else:
            statement = statement.where(Agent.scope != "project")
        statement = statement.order_by(AgentSchedule.next_run_at, AgentSchedule.id).with_for_update(
            skip_locked=True, of=AgentSchedule,
        )
        schedules = (await db.scalars(statement)).all()
        claimed = []
        for schedule in schedules:
            agent = await db.get(Agent, schedule.agent_id)
            if not await _schedule_agent_available(db, agent):
                continue
            occurrence_at = schedule.next_run_at
            occurrence_id = uuid.uuid5(schedule.id, occurrence_at.astimezone(UTC).isoformat())
            next_run = compute_next_run(schedule.cron_expr, now)
            anchor = await create_background_turn(
                db, agent=agent, kind="schedule", reference_id=occurrence_id,
                user_prompt=render_message("background.scheduleInput").format(instruction=schedule.instruction),
                execution_user_id=schedule.execution_user_id,
                title=schedule.name, settings=resource_execution_settings(schedule),
                completion={
                    "schedule_id": str(schedule.id), "instruction": schedule.instruction,
                    "occurrence_at": occurrence_at.isoformat(), "manual": False,
                },
            )
            schedule.last_run_at = now
            schedule.next_run_at = next_run
            schedule.run_count = (schedule.run_count or 0) + 1
            claimed.append(_DueSchedule(
                id=schedule.id, agent_id=schedule.agent_id, name=schedule.name,
                instruction=schedule.instruction, occurrence_at=occurrence_at,
                next_run_at=next_run, anchor_id=anchor.id,
            ))
        await db.commit()
        return tuple(claimed)


async def _claim_due_schedules(now: datetime) -> tuple[_DueSchedule, ...]:
    standard = await _claim_due_schedules_for_scope(now, project_agents=False)
    try:
        project = await _claim_due_schedules_for_scope(now, project_agents=True)
    except Exception as exc:
        logger.error("Project schedule claim failed; standard schedules remain available: {}", exc)
        project = ()
    return tuple(sorted((*standard, *project), key=lambda item: (item.occurrence_at, str(item.id))))


async def _execute_claimed_schedule(schedule: _DueSchedule) -> None:
    from app.services.background_turns import run_background_turn

    try:
        await run_background_turn(schedule.anchor_id)
    except Exception:
        logger.exception("Schedule occurrence {} interrupted; the same turn will recover", schedule.anchor_id)


async def _tick():
    from app.services.audit_logger import write_audit_log

    try:
        schedules = await _claim_due_schedules(datetime.now(UTC))
        if schedules:
            await write_audit_log("schedule_tick", {"due_count": len(schedules)})
        for schedule in schedules:
            await write_audit_log("schedule_fire", {
                "schedule_id": str(schedule.id), "name": schedule.name,
                "instruction": schedule.instruction[:100], "next_run": str(schedule.next_run_at),
                "turn_anchor_id": str(schedule.anchor_id),
            }, agent_id=schedule.agent_id)
            asyncio.create_task(_execute_claimed_schedule(schedule))
    except Exception as exc:
        logger.exception("Scheduler tick error: {}", exc)
        await write_audit_log("schedule_error", {"error": str(exc)[:300]})


async def start_scheduler():
    logger.info("Agent scheduler started (30s interval)")
    while True:
        await _tick()
        await asyncio.sleep(30)
