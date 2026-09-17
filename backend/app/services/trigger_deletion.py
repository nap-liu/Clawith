"""Shared hard-delete contract for trigger definitions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update

from app.models.trigger import AgentTrigger
from app.models.trigger_execution import TriggerExecution


class TriggerDeletionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeletedTrigger:
    id: uuid.UUID
    name: str


async def delete_trigger_definition(
    db,
    *,
    agent_id: uuid.UUID,
    trigger_id: uuid.UUID | None = None,
    name: str | None = None,
) -> DeletedTrigger:
    """Delete one inactive definition while retaining its execution history."""
    query = select(AgentTrigger).where(AgentTrigger.agent_id == agent_id)
    if trigger_id is not None:
        query = query.where(AgentTrigger.id == trigger_id)
    elif name:
        query = query.where(AgentTrigger.name == name)
    else:
        raise TriggerDeletionError("selector_required", "Provide trigger id or name")

    trigger = (await db.execute(query.with_for_update())).scalar_one_or_none()
    if trigger is None:
        raise TriggerDeletionError("not_found", "Trigger not found")
    if trigger.is_system:
        raise TriggerDeletionError("system", "System triggers cannot be deleted")
    if trigger.is_enabled:
        raise TriggerDeletionError("enabled", "Cancel the trigger before deleting it")

    unfinished = await db.scalar(
        select(TriggerExecution.id)
        .where(
            TriggerExecution.trigger_id == trigger.id,
            TriggerExecution.status.in_(("pending", "processing")),
        )
        .limit(1)
    )
    if unfinished is not None:
        raise TriggerDeletionError(
            "unfinished",
            "Wait for the trigger's current run to finish before deleting it",
        )

    await db.execute(
        update(TriggerExecution)
        .where(
            TriggerExecution.trigger_id == trigger.id,
            TriggerExecution.trigger_name == "",
        )
        .values(trigger_name=trigger.name)
    )
    deleted = DeletedTrigger(id=trigger.id, name=trigger.name)
    await db.delete(trigger)
    return deleted
