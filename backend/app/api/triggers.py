"""Triggers REST API — CRUD endpoints for the Aware page frontend."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api.auth import get_current_user
from app.core.permissions import check_agent_access
from app.database import async_session
from app.models.trigger import AgentTrigger

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


class TriggerUpdate(BaseModel):
    config: dict | None = None
    reason: str | None = None
    is_enabled: bool | None = None
    max_fires: int | None = None
    cooldown_seconds: int | None = None
    expires_at: str | None = None


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


def _contains_private_config(value) -> bool:
    if isinstance(value, dict):
        return any(
            _is_private_config_key(key) or _contains_private_config(item)
            for key, item in value.items()
        )
    return isinstance(value, list) and any(_contains_private_config(item) for item in value)


def _private_config(value):
    if not isinstance(value, dict):
        return {}
    private = {}
    for key, item in value.items():
        if _is_private_config_key(key):
            private[key] = item
        elif isinstance(item, dict):
            nested = _private_config(item)
            if nested:
                private[key] = nested
    return private


def _merge_private_config(public, private):
    merged = dict(public)
    for key, value in private.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_private_config(merged[key], value)
        else:
            merged[key] = value
    return merged


async def _require_manage(db, user, agent_id: uuid.UUID) -> None:
    _, access = await check_agent_access(db, user, agent_id)
    if access != "manage":
        raise HTTPException(403, "Manage access to this agent is required")


@router.get("/{agent_id}/triggers", response_model=list[TriggerResponse])
async def list_agent_triggers(agent_id: uuid.UUID, user=Depends(get_current_user)):
    """List all triggers for an agent."""
    async with async_session() as db:
        await _require_manage(db, user, agent_id)
        result = await db.execute(
            select(AgentTrigger)
            .where(AgentTrigger.agent_id == agent_id)
            .order_by(AgentTrigger.created_at.desc())
        )
        triggers = result.scalars().all()

    return [
        TriggerResponse(
            id=str(t.id),
            name=t.name,
            type=t.type,
            config=_public_config(t.config or {}),
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
        )
        for t in triggers
    ]


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
            )
        )
        trigger = result.scalar_one_or_none()
        if not trigger:
            raise HTTPException(404, "Trigger not found")

        changed_fields = body.model_fields_set
        if trigger.is_system and changed_fields - {"is_enabled"}:
            raise HTTPException(403, "System triggers can only be enabled or disabled")

        if body.config is not None:
            if _contains_private_config(body.config):
                raise HTTPException(422, "Trigger config contains reserved internal fields")
            trigger.config = _merge_private_config(
                body.config,
                _private_config(trigger.config or {}),
            )
        if body.reason is not None:
            trigger.reason = body.reason
        if body.is_enabled is not None:
            trigger.is_enabled = body.is_enabled
        if body.max_fires is not None:
            trigger.max_fires = body.max_fires
        if body.cooldown_seconds is not None:
            trigger.cooldown_seconds = body.cooldown_seconds
        if body.expires_at is not None:
            from datetime import datetime
            trigger.expires_at = datetime.fromisoformat(body.expires_at)

        await db.commit()

    return {"ok": True}


@router.delete("/{agent_id}/triggers/{trigger_id}")
async def delete_trigger(
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID,
    user=Depends(get_current_user),
):
    """Delete a trigger entirely."""
    async with async_session() as db:
        await _require_manage(db, user, agent_id)
        result = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.id == trigger_id,
                AgentTrigger.agent_id == agent_id,
            )
        )
        trigger = result.scalar_one_or_none()
        if not trigger:
            raise HTTPException(404, "Trigger not found")
        if trigger.is_system:
            raise HTTPException(403, "System triggers cannot be deleted")

        await db.delete(trigger)
        await db.commit()

    return {"ok": True}
