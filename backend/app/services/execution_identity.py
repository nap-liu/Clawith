"""Execution-user validation shared by unattended background runners."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import get_agent_access_level_for_user_id, is_platform_admin_user
from app.models.agent import Agent
from app.models.audit import AuditLog, ChatMessage
from app.models.chat_session import ChatSession
from app.models.schedule import AgentSchedule
from app.models.task import Task, TaskLog
from app.models.trigger import AgentTrigger
from app.models.user import User


class ExecutionIdentityError(RuntimeError):
    """The configured user can no longer execute work for the agent."""


class ExecutionIdentityConflict(ExecutionIdentityError):
    """The execution user changed after the caller last read it."""


class ExecutionIdentityPermissionError(ExecutionIdentityError):
    """The caller cannot administer the resource's execution identity."""


async def is_human_interactive_turn(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_anchor_id: uuid.UUID,
) -> bool:
    """Return whether an Agent tool call belongs to a real human turn."""
    from app.services.session_query import HUMAN_CHANNELS

    session = await db.get(ChatSession, session_id)
    anchor = await db.get(ChatMessage, turn_anchor_id)
    anchor_meta = (
        anchor.message_meta if anchor and isinstance(anchor.message_meta, dict) else {}
    )
    return bool(
        session is not None
        and session.source_channel in HUMAN_CHANNELS
        and session.agent_id == agent_id
        and anchor is not None
        and anchor.conversation_id == str(session.id)
        and anchor.role == "user"
        and anchor.sender_user_id == actor_user_id
        and anchor_meta.get("kind") != "on_message_event"
        and not anchor_meta.get("trigger_execution_id")
    )


@dataclass(frozen=True)
class ExecutionIdentityChange:
    resource_type: str
    resource_id: uuid.UUID
    before: uuid.UUID | None
    after: uuid.UUID
    frozen_execution_count: int = 0


async def resolve_execution_user_id(
    db: AsyncSession,
    agent: Agent,
    configured_user_id: uuid.UUID | None,
    *,
    legacy_user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Resolve an explicit principal, with a legacy fallback for rolling upgrades."""
    user_id = configured_user_id or legacy_user_id
    if user_id is None:
        raise ExecutionIdentityError("Background execution has no configured user")
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise ExecutionIdentityError("Background execution user is missing or inactive")
    if user.tenant_id != agent.tenant_id:
        raise ExecutionIdentityError("Background execution user is outside the agent tenant")
    if await get_agent_access_level_for_user_id(db, user.id, agent) is None:
        raise ExecutionIdentityError("Background execution user no longer has agent access")
    return user.id


async def require_assignable_execution_user(
    db: AsyncSession,
    agent: Agent,
    user_id: uuid.UUID,
) -> uuid.UUID:
    """Validate an administrator-selected execution user."""
    return await resolve_execution_user_id(db, agent, user_id)


def _origin_uuid(trigger: AgentTrigger) -> uuid.UUID | None:
    """Recover only the on_message origin that affected legacy execution."""
    config = trigger.config if isinstance(trigger.config, dict) else {}
    if trigger.type != "on_message" or not config.get("_origin_session_id"):
        return None
    try:
        return uuid.UUID(str(config.get("_origin_user_id")))
    except (TypeError, ValueError, AttributeError):
        return None


async def legacy_trigger_execution_user_id(
    db: AsyncSession,
    trigger: AgentTrigger,
    agent: Agent,
) -> uuid.UUID:
    """Return the principal used before explicit execution identity existed."""
    origin_id = _origin_uuid(trigger)
    if origin_id is not None:
        origin = await db.get(User, origin_id)
        if origin and origin.tenant_id == agent.tenant_id:
            return origin.id
    return agent.creator_id


_RESOURCE_MODELS = {
    "trigger": AgentTrigger,
    "task": Task,
    "schedule": AgentSchedule,
}

_ORIGIN_UUID_RE = (
    "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    "[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


async def align_background_execution_user(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    expected_execution_user_id: uuid.UUID | None = None,
    expected_provided: bool = False,
) -> ExecutionIdentityChange:
    """Align a resource to its last actor while freezing already-started work.

    Callers must authorize the resource mutation itself. This function owns the
    concurrency-sensitive identity transition shared by REST, MCP, and Agent
    tools: lock the resource, validate the new principal, snapshot legacy queued
    work to the old effective principal, then update future execution identity.
    """
    model = _RESOURCE_MODELS.get(resource_type)
    if model is None:
        raise ExecutionIdentityError("resource_type must be trigger, task, or schedule")

    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise ExecutionIdentityError("Agent not found")
    resource = (
        await db.execute(
            select(model)
            .where(model.id == resource_id, model.agent_id == agent_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if resource is None:
        raise ExecutionIdentityError(f"{resource_type} not found")

    before = resource.execution_user_id
    if expected_provided and before != expected_execution_user_id:
        raise ExecutionIdentityConflict(
            "Execution user changed since it was read; refresh and retry with the current value"
        )
    after = await require_assignable_execution_user(db, agent, execution_user_id)

    frozen_count = 0
    if resource_type == "trigger":
        old_effective_user_id = before or await legacy_trigger_execution_user_id(
            db, resource, agent
        )
        # Rolling-upgrade rows may not have captured an execution identity yet.
        # Freeze them before changing the identity used by future work.
        frozen = await db.execute(
            text(
                f"""
                UPDATE trigger_executions AS execution
                   SET execution_user_id = COALESCE(
                       CASE
                           WHEN trigger.type = 'on_message'
                            AND COALESCE(execution.payload->>'_origin_session_id', '') <> ''
                            AND COALESCE(execution.payload->>'_origin_user_id', '')
                                ~* '{_ORIGIN_UUID_RE}'
                            AND EXISTS (
                                SELECT 1 FROM users
                                 WHERE users.id = (execution.payload->>'_origin_user_id')::uuid
                                   AND users.tenant_id = :tenant_id
                            )
                           THEN (execution.payload->>'_origin_user_id')::uuid
                           ELSE NULL
                       END,
                       :old_effective_user_id
                   )
                  FROM agent_triggers AS trigger
                 WHERE execution.trigger_id = trigger.id
                   AND execution.trigger_id = :trigger_id
                   AND execution.status IN ('pending', 'processing')
                   AND execution.execution_user_id IS NULL
                """
            ),
            {
                "tenant_id": agent.tenant_id,
                "old_effective_user_id": old_effective_user_id,
                "trigger_id": resource.id,
            },
        )
        frozen_count = int(frozen.rowcount or 0)
    elif resource_type == "task" and resource.status == "doing":
        # TaskLog is the durable run snapshot. Preserve a legacy active run that
        # predates the snapshot column while updating only future task runs.
        active_run_id = await db.scalar(
            select(TaskLog.id)
            .where(
                TaskLog.task_id == resource.id,
                TaskLog.content == "🤖 开始执行任务...",
                TaskLog.execution_user_id.is_(None),
            )
            .order_by(TaskLog.created_at.desc(), TaskLog.id.desc())
            .limit(1)
        )
        if active_run_id is not None:
            run = await db.get(TaskLog, active_run_id)
            # Match task_executor's legacy source of truth exactly: a Task with
            # no explicit execution user ran as its own creator, regardless of
            # whether it is todo or supervision work.
            run.execution_user_id = before or resource.created_by
            frozen_count = 1

    resource.execution_user_id = after
    await db.flush()
    return ExecutionIdentityChange(
        resource_type=resource_type,
        resource_id=resource.id,
        before=before,
        after=after,
        frozen_execution_count=frozen_count,
    )


async def reassign_background_execution_user(
    db: AsyncSession,
    *,
    actor_user_id: uuid.UUID,
    agent_id: uuid.UUID,
    resource_type: str,
    resource_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    expected_execution_user_id: uuid.UUID | None = None,
    expected_provided: bool = False,
    audit_reason: str | None = None,
) -> ExecutionIdentityChange:
    """Atomically change only a background resource's future execution user.

    Authorization follows the canonical Agent ``manage`` permission. The row is
    locked, an optional compare-and-swap guard prevents lost updates, and queued
    trigger rows without a snapshot are frozen to the old effective user before
    the trigger is changed. Existing snapshots and every non-identity resource
    field remain byte-for-byte untouched.
    """
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise ExecutionIdentityError("Agent not found")
    actor = await db.get(User, actor_user_id)
    if actor is None or not actor.is_active:
        raise ExecutionIdentityError("The administrator identity is missing or inactive")
    if not is_platform_admin_user(actor) and actor.role != "org_admin":
        raise ExecutionIdentityPermissionError(
            "Only platform administrators and organization administrators may reassign execution users"
        )
    if await get_agent_access_level_for_user_id(db, actor_user_id, agent) != "manage":
        raise ExecutionIdentityPermissionError("Manage access to this Agent is required")

    change = await align_background_execution_user(
        db,
        agent_id=agent_id,
        resource_type=resource_type,
        resource_id=resource_id,
        execution_user_id=execution_user_id,
        expected_execution_user_id=expected_execution_user_id,
        expected_provided=expected_provided,
    )
    if change.before == change.after:
        return change
    db.add(
        AuditLog(
            user_id=actor_user_id,
            agent_id=agent_id,
            action="background_execution_user_changed",
            details={
                "resource_type": resource_type,
                "resource_id": str(change.resource_id),
                "before": str(change.before) if change.before else None,
                "after": str(change.after),
                "frozen_execution_count": change.frozen_execution_count,
                "reason": (audit_reason or "")[:500],
            },
        )
    )
    await db.flush()
    return change


async def handle_reassign_background_execution_user(
    agent_id,
    user_id,
    ctx_session_id,
    turn_anchor_id,
    arguments: dict,
) -> str:
    """Agent-tool boundary: only a manage-authorized human interaction may mutate."""
    from app.database import async_session
    from app.services.session_query import _as_uuid

    aid = _as_uuid(agent_id)
    actor_id = _as_uuid(user_id)
    session_uuid = _as_uuid(ctx_session_id)
    anchor_uuid = _as_uuid(turn_anchor_id)
    resource_id = _as_uuid(arguments.get("resource_id"))
    target_id = _as_uuid(arguments.get("execution_user_id"))
    if None in {aid, actor_id, session_uuid, anchor_uuid, resource_id, target_id}:
        return "❌ agent/session/resource/execution_user must use exact canonical UUIDs"
    resource_type = str(arguments.get("resource_type") or "").strip()
    reason = str(arguments.get("reason") or "").strip()
    if not reason:
        return "❌ reason is required for the audit trail"
    if "expected_execution_user_id" not in arguments:
        return "❌ expected_execution_user_id is required; read the resource before changing it"
    expected_raw = arguments.get("expected_execution_user_id")
    expected_id = _as_uuid(expected_raw) if expected_raw is not None else None
    if expected_raw is not None and expected_id is None:
        return "❌ expected_execution_user_id must be an exact UUID or null"

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
            change = await reassign_background_execution_user(
                db,
                actor_user_id=actor_id,
                agent_id=aid,
                resource_type=resource_type,
                resource_id=resource_id,
                execution_user_id=target_id,
                expected_execution_user_id=expected_id,
                expected_provided=True,
                audit_reason=reason,
            )
            await db.commit()
        except ExecutionIdentityConflict as exc:
            await db.rollback()
            return f"❌ Conflict: {exc}"
        except ExecutionIdentityError as exc:
            await db.rollback()
            return f"❌ {exc}"

    return (
        "✅ Execution user updated atomically. "
        f"resource={change.resource_type}:{change.resource_id}, "
        f"before={change.before}, after={change.after}, "
        f"frozen_queued_executions={change.frozen_execution_count}. "
        "Existing execution snapshots were preserved; only future unsnapshotted runs change."
    )
