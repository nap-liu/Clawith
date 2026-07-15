"""Canonical target validation for supervision tasks.

Supervision targets are frozen as one tenant ``User.id`` or ``Agent.id``.
Names are returned only as display snapshots and never participate in lookup.
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.recipient_resolver import (
    resolve_agent_recipient,
    resolve_human_channel_recipient,
)


@dataclass(frozen=True)
class ResolvedSupervisionTarget:
    target_type: str
    target_id: uuid.UUID
    display_name: str
    channel: str | None = None


async def resolve_supervision_target(
    db: AsyncSession,
    source_agent_id: uuid.UUID,
    *,
    target_user_id: uuid.UUID | None,
    target_agent_id: uuid.UUID | None,
    channel: str | None = None,
) -> ResolvedSupervisionTarget:
    """Resolve exactly one active, related, same-tenant supervision target."""

    if (target_user_id is None) == (target_agent_id is None):
        raise ValueError(
            "exactly one supervision_target_user_id or "
            "supervision_target_agent_id is required"
        )

    if target_agent_id is not None:
        recipient = await resolve_agent_recipient(
            db, source_agent_id, target_agent_id
        )
        return ResolvedSupervisionTarget(
            target_type="agent",
            target_id=recipient.target_agent.id,
            display_name=recipient.target_agent.name,
        )

    route = await resolve_human_channel_recipient(
        db,
        source_agent_id,
        target_user_id,
        channel=channel,
    )
    return ResolvedSupervisionTarget(
        target_type="user",
        target_id=route.user.id,
        display_name=route.user.display_name,
        channel=route.channel,
    )
