"""Confirmation card resolve endpoint.

Human-only: POST /api/agents/{agent_id}/confirmations/{cid}/resolve
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
    action: str


@router.post("/{agent_id}/confirmations/{cid}/resolve")
async def resolve_confirmation_endpoint(
    agent_id: uuid.UUID,
    cid: uuid.UUID,
    body: ResolveBody,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve (confirm or cancel) a pending confirmation card.

    Only authenticated human users with access to the agent may call this.
    Service tokens, agents, and triggers have no path to this endpoint.
    """
    if body.action not in ("confirm", "cancel"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="action must be 'confirm' or 'cancel'",
        )

    # Raises 404 if agent not found, 403 if no access.
    await check_agent_access(db, current_user, agent_id)

    try:
        c = await confirmation_service.resolve_confirmation(
            confirmation_id=cid,
            agent_id=agent_id,
            decision=body.action,
            resolving_user_id=current_user.id,
        )
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="confirmation not found")

    return {"status": c.status, "result": c.result}
