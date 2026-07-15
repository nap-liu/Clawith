"""Tenant invariants for canonical human chat sessions."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.user import User


async def require_same_tenant_session_user(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Reject a P2P session pair unless both canonical IDs share a tenant."""

    row = (
        await db.execute(
            select(Agent.tenant_id, User.tenant_id)
            .select_from(Agent)
            .join(User, User.id == user_id)
            .where(Agent.id == agent_id)
        )
    ).one_or_none()
    if row is None or row[0] is None or row[0] != row[1]:
        raise ValueError("chat session agent_id and user_id must share a tenant")
