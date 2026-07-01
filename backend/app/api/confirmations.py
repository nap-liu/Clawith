"""Confirmation card resolve endpoint.

Human-only: POST /api/agents/{agent_id}/confirmations/{cid}/resolve

A confirmation card is a suspended `request_confirmation` tool_call; `cid` is that tool_call
row id. Resolving fills the tool result with the clicked button (faithful relay — the
platform executes nothing) and resumes the agent's loop.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services import confirmation_service

router = APIRouter(prefix="/agents", tags=["confirmations"])


class ResolveBody(BaseModel):
    # The clicked button's value (e.g. "confirm" / "cancel" / "plan_a") and display label.
    # `action` is accepted as a legacy alias for value (old confirm/cancel-only cards).
    value: str | None = None
    label: str | None = None
    action: str | None = None


@router.post("/{agent_id}/confirmations/{cid}/resolve")
async def resolve_confirmation_endpoint(
    agent_id: uuid.UUID,
    cid: uuid.UUID,
    body: ResolveBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a pending confirmation card by recording the clicked button.

    Only authenticated human users with access to the agent may call this.
    Service tokens, agents, and triggers have no path to this endpoint.
    """
    value = (body.value or body.action or "").strip()
    if not value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resolve requires a button 'value'",
        )
    label = body.label or ("确认" if value == "confirm" else "取消" if value == "cancel" else value)

    # Raises 404 if agent not found, 403 if no access.
    await check_agent_access(db, current_user, agent_id)

    result = await confirmation_service.resolve_confirmation(
        agent_id=agent_id,
        call_id=cid,
        button_value=value,
        button_label=label,
        resolving_user_id=current_user.id,
    )
    if result is None:
        # Unknown card, not owned by this agent, or already resolved (idempotent).
        return {"status": "done", "result": None}
    return {"status": "done", "result": result}
