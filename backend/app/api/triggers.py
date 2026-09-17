"""Triggers REST API — CRUD endpoints for the Aware page frontend."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.api.auth import get_current_user
from app.core.permissions import check_agent_access
from app.database import async_session
from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution
from app.models.user import User
from app.services.llm.reasoning import ReasoningEffort

router = APIRouter(prefix="/api/agents", tags=["triggers"])


class TriggerResponse(BaseModel):
    id: str
    name: str
    type: str
    config: dict
    reason: str
    focus_ref: str | None = None
    is_enabled: bool
    is_system: bool = False
    fire_count: int
    max_fires: int | None = None
    cooldown_seconds: int
    last_fired_at: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    created_by_user_id: str | None = None
    creator_display_name: str | None = None
    execution_user_id: str | None = None
    execution_user_display_name: str | None = None
    model_id: str | None = None
    temperature: float | None = None
    reasoning_effort: ReasoningEffort | None = None
    soul: bool = True
    memory: bool = True


class TriggerUpdate(BaseModel):
    config: dict | None = None
    clear_fields: list[str] = []
    focus_ref: str | None = None
    reason: str | None = None
    is_enabled: bool | None = None
    max_fires: int | None = None
    cooldown_seconds: int | None = None
    expires_at: str | None = None
    execution_user_id: uuid.UUID | None = None
    expected_execution_user_id: uuid.UUID | None = None
    model_id: uuid.UUID | None = None
    temperature: float | None = None
    reasoning_effort: ReasoningEffort | None = None
    soul: bool = True
    memory: bool = True


class TriggerExecutionResponse(BaseModel):
    id: str
    trigger_id: str
    trigger_name: str
    source: str
    status: str
    conversation_id: str | None = None
    scheduled_at: str
    started_at: str | None = None
    finished_at: str | None = None
    last_error: str | None = None
    execution_user_id: str | None = None


class ScheduleValidationRequest(BaseModel):
    kind: Literal["cron", "datetime", "timezone"]
    value: str
    timezone: str | None = None


class ScheduleValidationResponse(BaseModel):
    valid: bool


_PRIVATE_CONFIG_PARTS = ("token", "secret", "password", "api_key", "webhook_queue")


def _is_private_config_key(key: object) -> bool:
    normalized = str(key).lower()
    return normalized.startswith("_") or any(part in normalized for part in _PRIVATE_CONFIG_PARTS)


def _public_config(value):
    if isinstance(value, dict):
        return {
            key: _public_config(item)
            for key, item in value.items()
            if not _is_private_config_key(key)
        }
    if isinstance(value, list):
        return [_public_config(item) for item in value]
    return value


def _member_visible_config(trigger_type: str, value: object) -> dict:
    """Expose only display-safe scheduling fields to non-managing viewers."""
    if not isinstance(value, dict):
        return {}
    safe_keys = {
        "cron": ("expr",),
        "once": ("at",),
        "interval": ("minutes",),
    }.get(trigger_type, ())
    return {key: value[key] for key in safe_keys if key in value}


async def _require_manage(db, user, agent_id: uuid.UUID) -> None:
    _, access = await check_agent_access(db, user, agent_id)
    if access != "manage":
        raise HTTPException(403, "Manage access to this agent is required")


@router.get("/{agent_id}/triggers", response_model=list[TriggerResponse])
async def list_agent_triggers(agent_id: uuid.UUID, user=Depends(get_current_user)):
    """List all triggers for an agent."""
    async with async_session() as db:
        _agent, access_level = await check_agent_access(db, user, agent_id)
        result = await db.execute(
            select(AgentTrigger)
            .where(AgentTrigger.agent_id == agent_id)
            .order_by(AgentTrigger.created_at.desc())
        )
        triggers = result.scalars().all()
        user_ids = {
            user_id
            for trigger in triggers
            for user_id in (trigger.created_by_user_id, trigger.execution_user_id)
            if user_id
        }
        user_names: dict[uuid.UUID, str] = {}
        if user_ids:
            users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
            user_names = {item.id: item.display_name for item in users_result.scalars().all()}

    return [
        TriggerResponse(
            id=str(t.id),
            name=t.name,
            type=t.type,
            config=(
                _public_config(t.config or {})
                if access_level == "manage"
                else _member_visible_config(t.type, t.config or {})
            ),
            reason=t.reason or "",
            focus_ref=t.focus_ref,
            is_enabled=t.is_enabled,
            is_system=t.is_system,
            fire_count=t.fire_count,
            max_fires=t.max_fires,
            cooldown_seconds=t.cooldown_seconds,
            last_fired_at=t.last_fired_at.isoformat() if t.last_fired_at else None,
            created_at=t.created_at.isoformat() if t.created_at else None,
            expires_at=t.expires_at.isoformat() if t.expires_at else None,
            created_by_user_id=str(t.created_by_user_id) if t.created_by_user_id else None,
            creator_display_name=user_names.get(t.created_by_user_id),
            execution_user_id=str(t.execution_user_id) if t.execution_user_id else None,
            execution_user_display_name=user_names.get(t.execution_user_id),
            model_id=str(t.model_id) if t.model_id else None,
            temperature=t.temperature,
            reasoning_effort=t.reasoning_effort,
            soul=t.soul,
            memory=t.memory,
        )
        for t in triggers
    ]


@router.get(
    "/{agent_id}/trigger-executions",
    response_model=list[TriggerExecutionResponse],
)
async def list_trigger_executions(
    agent_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=200),
    user=Depends(get_current_user),
):
    """List durable trigger runs and their canonical Web conversation."""
    async with async_session() as db:
        await _require_manage(db, user, agent_id)
        rows = (
            await db.execute(
                select(TriggerExecution, AgentTrigger.name)
                .outerjoin(AgentTrigger, AgentTrigger.id == TriggerExecution.trigger_id)
                .where(TriggerExecution.agent_id == agent_id)
                .order_by(TriggerExecution.scheduled_at.desc())
                .limit(limit)
            )
        ).all()

    return [
        TriggerExecutionResponse(
            id=str(execution.id),
            trigger_id=str(execution.trigger_id),
            trigger_name=execution.trigger_name or trigger_name or str(execution.trigger_id),
            source=execution.source,
            status=execution.status,
            conversation_id=str(execution.conversation_id) if execution.conversation_id else None,
            scheduled_at=execution.scheduled_at.isoformat(),
            started_at=execution.started_at.isoformat() if execution.started_at else None,
            finished_at=execution.finished_at.isoformat() if execution.finished_at else None,
            last_error=execution.last_error,
            execution_user_id=(
                str(execution.execution_user_id) if execution.execution_user_id else None
            ),
        )
        for execution, trigger_name in rows
    ]


@router.post(
    "/{agent_id}/scheduling/validate",
    response_model=ScheduleValidationResponse,
)
async def validate_scheduling_value(
    agent_id: uuid.UUID,
    body: ScheduleValidationRequest,
    user=Depends(get_current_user),
):
    """Validate editable values with the runtime scheduling parsers."""
    from app.services.schedule_config_validation import validate_schedule_value

    async with async_session() as db:
        await _require_manage(db, user, agent_id)
    return ScheduleValidationResponse(
        valid=validate_schedule_value(body.kind, body.value, body.timezone),
    )


@router.patch("/{agent_id}/triggers/{trigger_id}")
async def update_trigger(
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    body: TriggerUpdate,
    user=Depends(get_current_user),
):
    """Update a trigger (from frontend management UI)."""
    async with async_session() as db:
        await _require_manage(db, user, agent_id)
        result = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.id == trigger_id,
                AgentTrigger.agent_id == agent_id,
            ).with_for_update()
        )
        trigger = result.scalar_one_or_none()
        if not trigger:
            raise HTTPException(404, "Trigger not found")

        if "model_id" in body.model_fields_set:
            from app.models.agent import Agent
            from app.services.chat_model_selection import validate_agent_model_override

            agent = await db.get(Agent, agent_id)
            try:
                await validate_agent_model_override(db, agent=agent, model_id=body.model_id)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

        changed_fields = body.model_fields_set
        if trigger.is_system and (changed_fields - {"clear_fields"}) - {
            "is_enabled",
            "execution_user_id",
            "expected_execution_user_id",
            "model_id",
            "temperature",
            "reasoning_effort",
            "soul",
            "memory",
        }:
            raise HTTPException(
                403,
                "System triggers can only be enabled/disabled or reassigned by an Agent manager",
            )

        from app.services.trigger_patch import prepare_patch, resolve_patch_recipient, apply_patch, serialize_trigger, check_enable_limit, recover_enabled_trigger
        from app.services.timezone_utils import get_agent_timezone
        patch = body.model_dump(exclude_unset=True, exclude={"execution_user_id", "expected_execution_user_id"})
        try:
            values = prepare_patch(trigger, patch, await get_agent_timezone(agent_id), private_key=_is_private_config_key)
            await resolve_patch_recipient(db, agent_id, trigger, values)
            await check_enable_limit(db, trigger, values)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        identity_reassigned = False
        if "execution_user_id" in changed_fields:
            if body.execution_user_id is None:
                raise HTTPException(422, "execution_user_id cannot be null")
            if "expected_execution_user_id" not in changed_fields:
                raise HTTPException(422, "expected_execution_user_id is required")
            from app.services.execution_identity import (
                ExecutionIdentityConflict,
                ExecutionIdentityError,
                ExecutionIdentityPermissionError,
                reassign_background_execution_user,
            )

            try:
                await reassign_background_execution_user(
                    db,
                    actor_user_id=user.id,
                    agent_id=agent_id,
                    resource_type="trigger",
                    resource_id=trigger_id,
                    execution_user_id=body.execution_user_id,
                    expected_execution_user_id=body.expected_execution_user_id,
                    expected_provided=True,
                )
            except ExecutionIdentityConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            except ExecutionIdentityPermissionError as exc:
                raise HTTPException(403, str(exc)) from exc
            except ExecutionIdentityError as exc:
                raise HTTPException(422, str(exc)) from exc
            identity_reassigned = True
        elif "expected_execution_user_id" in changed_fields:
            raise HTTPException(422, "expected_execution_user_id requires execution_user_id")

        if changed_fields and not identity_reassigned:
            from app.services.execution_identity import align_background_execution_user

            await align_background_execution_user(
                db,
                agent_id=agent_id,
                resource_type="trigger",
                resource_id=trigger.id,
                execution_user_id=user.id,
            )

        apply_patch(trigger, values)

        await db.commit()
        await recover_enabled_trigger(trigger, values)

    return {"ok": True, "trigger": serialize_trigger(trigger, config_projector=_public_config)}


@router.delete("/{agent_id}/triggers/{trigger_id}")
async def delete_trigger(
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    user=Depends(get_current_user),
):
    """Delete a disabled trigger."""
    from app.services.trigger_deletion import TriggerDeletionError, delete_trigger_definition

    async with async_session() as db:
        await _require_manage(db, user, agent_id)
        try:
            deleted = await delete_trigger_definition(
                db,
                agent_id=agent_id,
                trigger_id=trigger_id,
            )
        except TriggerDeletionError as exc:
            status = 404 if exc.code == "not_found" else 403 if exc.code == "system" else 409
            raise HTTPException(status, str(exc)) from exc
        await db.commit()

    return {"ok": True, "deleted_trigger": {"id": str(deleted.id), "name": deleted.name}}
