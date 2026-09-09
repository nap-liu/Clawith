"""Canonical manual execution path for durable background resources."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import get_agent_access_level_for_user_id, is_agent_expired
from app.models.agent import Agent
from app.models.audit import AuditLog
from app.models.schedule import AgentSchedule
from app.models.task import Task
from app.models.trigger import AgentTrigger
from app.services.execution_identity import align_background_execution_user
from app.services.project_runtime_boundary import lock_and_check_project_agent_runtime
from app.services.trigger_runtime.queue import enqueue_trigger_execution


class BackgroundManualRunError(RuntimeError):
    """The requested background resource cannot be manually executed."""


class BackgroundManualRunConflict(BackgroundManualRunError):
    """The resource already has an incompatible active execution."""


@dataclass(frozen=True)
class BackgroundManualRunResult:
    resource_type: str
    resource_id: uuid.UUID
    resource_name: str
    execution_id: uuid.UUID | None = None


_RESOURCE_MODELS = {
    "trigger": (AgentTrigger, AgentTrigger.name),
    "task": (Task, Task.title),
    "schedule": (AgentSchedule, AgentSchedule.name),
}


async def _resolve_resource(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    resource_type: str,
    resource: str,
):
    definition = _RESOURCE_MODELS.get(resource_type)
    if definition is None:
        raise BackgroundManualRunError("resource_type must be trigger, task, or schedule")
    model, name_column = definition
    resource_id = None
    try:
        resource_id = uuid.UUID(resource)
    except (TypeError, ValueError, AttributeError):
        pass

    query = select(model).where(model.agent_id == agent_id)
    if resource_id is not None:
        query = query.where(model.id == resource_id)
    else:
        canonical_name = resource.strip()
        if not canonical_name:
            raise BackgroundManualRunError("resource is required")
        query = query.where(func.lower(name_column) == canonical_name.lower())

    matches = (await db.execute(query.with_for_update())).scalars().all()
    if not matches:
        raise BackgroundManualRunError(f"{resource_type} not found")
    if len(matches) > 1:
        choices = ", ".join(f"{item.id}:{getattr(item, name_column.key)}" for item in matches[:10])
        raise BackgroundManualRunConflict(
            f"Multiple {resource_type} resources have that name; use an exact UUID: {choices}"
        )
    return matches[0]


async def run_background_resource(
    db: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    agent_id: uuid.UUID,
    resource_type: str,
    resource: str,
) -> BackgroundManualRunResult:
    """Queue one resource run using the current actor as its durable identity."""
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise BackgroundManualRunError("未找到数字员工")
    if is_agent_expired(agent):
        raise BackgroundManualRunError("数字员工已过期")
    if await get_agent_access_level_for_user_id(db, actor_user_id, agent) is None:
        raise BackgroundManualRunError("The current user cannot access this Agent")

    # Serialize manual admission with the owner pause switch. No resource
    # identity, audit row, durable trigger occurrence, or asyncio task is
    # created unless the authoritative project runtime accepts this work.
    if getattr(agent, "scope", "standard") == "project":
        try:
            project_running = await lock_and_check_project_agent_runtime(db, agent)
        except Exception as exc:
            raise BackgroundManualRunConflict(
                "Project runtime is unavailable; try again after the project recovers"
            ) from exc
        if not project_running:
            raise BackgroundManualRunConflict(
                "Project runtime is paused; resume the project before starting background work"
            )

    item = await _resolve_resource(
        db,
        agent_id=agent_id,
        resource_type=resource_type,
        resource=resource,
    )
    if resource_type == "task" and item.status == "doing":
        raise BackgroundManualRunConflict("Task is already running")
    await align_background_execution_user(
        db,
        agent_id=agent_id,
        resource_type=resource_type,
        resource_id=item.id,
        execution_user_id=actor_user_id,
    )

    now = datetime.now(UTC)
    execution_id = None
    anchor_id = None
    if resource_type == "trigger":
        execution, created = await enqueue_trigger_execution(
            db,
            trigger=item,
            source="manual",
            idempotency_key=f"manual:{uuid.uuid4()}",
            payload_obj={"_manual_run": True},
            scheduled_at=now,
            commit=False,
        )
        if not created or execution is None:
            raise BackgroundManualRunConflict("Trigger run could not be queued")
        execution_id = execution.id
    elif resource_type == "task" and item.type != "supervision":
        from app.services.task_executor import prepare_task_turn

        # Finish identity assignment before loading workspace context. Acceptance
        # is returned only after the resulting run and audit commit below.
        await db.commit()
        anchor = await prepare_task_turn(db, item.id, agent_id, actor_user_id)
        if anchor is None:
            from app.services.llm.failure_outcome import render_message

            raise BackgroundManualRunConflict(render_message("background.unavailable"))
        anchor_id = anchor.id
        execution_id = uuid.UUID(anchor.message_meta["background_execution"]["reference_id"])
    elif resource_type == "schedule":
        from app.services.scheduler import prepare_schedule_turn

        anchor = await prepare_schedule_turn(
            db, item.id, agent_id, item.instruction, actor_user_id,
            item.model_id, item.temperature, item.reasoning_effort, item.soul, item.memory,
        )
        if anchor is None:
            from app.services.llm.failure_outcome import render_message

            raise BackgroundManualRunConflict(render_message("background.unavailable"))
        anchor_id = anchor.id
        execution_id = uuid.UUID(anchor.message_meta["background_execution"]["reference_id"])

    name_column = _RESOURCE_MODELS[resource_type][1]
    resource_name = getattr(item, name_column.key)
    db.add(
        AuditLog(
            user_id=actor_user_id,
            agent_id=agent_id,
            action="background_resource_manual_run",
            details={
                "resource_type": resource_type,
                "resource_id": str(item.id),
                "execution_id": str(execution_id) if execution_id else None,
            },
        )
    )
    await db.commit()

    from app.services.agent_execution.bridge import dispatch_background

    if anchor_id is not None:
        await dispatch_background("app.services.background_turns:run_background_turn", anchor_id)
    elif resource_type == "task":
        await dispatch_background("app.services.task_executor:execute_task", item.id, agent_id, actor_user_id)

    return BackgroundManualRunResult(
        resource_type=resource_type,
        resource_id=item.id,
        resource_name=resource_name,
        execution_id=execution_id,
    )


async def _execute_and_track_schedule(
    schedule_id: uuid.UUID,
    agent_id: uuid.UUID,
    instruction: str,
    execution_user_id: uuid.UUID,
    model_id: uuid.UUID | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    soul: bool = True,
    memory: bool = True,
) -> None:
    """Dispatch one manual occurrence; its durable finalizer owns counters."""
    from app.services.scheduler import _execute_schedule

    if model_id is None and temperature is None and reasoning_effort is None and soul and memory:
        await _execute_schedule(
            schedule_id,
            agent_id,
            instruction,
            execution_user_id,
        )
    else:
        await _execute_schedule(
            schedule_id,
            agent_id,
            instruction,
            execution_user_id,
            model_id,
            temperature,
            reasoning_effort,
            soul,
            memory,
        )


async def handle_run_background_resource(
    agent_id,
    user_id,
    session_id: str,
    turn_anchor_id,
    arguments: dict,
) -> str:
    """Agent tool boundary for the canonical manual execution service."""
    from app.database import async_session
    from app.services.execution_identity import is_human_interactive_turn
    from app.services.session_query import _as_uuid

    aid = _as_uuid(agent_id)
    actor_id = _as_uuid(user_id)
    session_uuid = _as_uuid(session_id)
    anchor_uuid = _as_uuid(turn_anchor_id)
    resource_type = str(arguments.get("resource_type") or "").strip()
    resource = str(arguments.get("resource") or "").strip()
    if None in {aid, actor_id, session_uuid, anchor_uuid}:
        return "❌ agent/session/execution user must use exact canonical UUIDs"
    if not resource:
        return "❌ resource is required"

    async with async_session() as db:
        if not await is_human_interactive_turn(
            db,
            agent_id=aid,
            actor_user_id=actor_id,
            session_id=session_uuid,
            turn_anchor_id=anchor_uuid,
        ):
            return "❌ This tool may only run in a human interactive session for this Agent"
        try:
            result = await run_background_resource(
                db,
                actor_user_id=actor_id,
                agent_id=aid,
                resource_type=resource_type,
                resource=resource,
            )
        except BackgroundManualRunError as exc:
            await db.rollback()
            return f"❌ {exc}"

    suffix = f", execution_id={result.execution_id}" if result.execution_id else ""
    return (
        "✅ Background execution queued. "
        f"resource={result.resource_type}:{result.resource_id}, "
        f"name={result.resource_name}{suffix}."
    )
