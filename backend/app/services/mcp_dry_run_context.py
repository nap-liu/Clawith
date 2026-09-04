"""Identity context and header masking helpers for MCP dry runs."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.user import User
from app.services.mcp_secret_fields import mask_sensitive_headers
from app.services.placeholder_engine import PlaceholderContext


async def _build_user_ctx(
    db: AsyncSession,
    current_user: User,
    agent_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
) -> PlaceholderContext:
    """Build a PlaceholderContext from the authenticated caller + agent row.

    When agent_id is provided, the Agent row is loaded so ${agent.name} /
    ${agent.slug} resolve to real values (otherwise placeholders render to
    empty strings, which surprises users seeing the live preview).
    """
    agent_name = ""
    agent_slug = ""
    if agent_id is not None:
        agent_row = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
        if agent_row is not None:
            agent_name = agent_row.name or ""
            agent_slug = getattr(agent_row, "slug", "") or ""
    return PlaceholderContext(
        user={
            "id": str(current_user.id),
            "email": current_user.identity.email if current_user.identity else "",
            "phone": current_user.identity.phone if current_user.identity else "",
            "name": current_user.display_name or "",
            "display_name": current_user.display_name or "",
        },
        agent={"id": str(agent_id) if agent_id else "", "name": agent_name, "slug": agent_slug},
        tenant={"id": str(tenant_id or current_user.tenant_id or "")},
        session={"id": "preview-session"},
        channel={"type": "web"},
    )


def _mask_auth_headers(headers: dict[str, str]) -> dict[str, str]:
    return mask_sensitive_headers(headers)
