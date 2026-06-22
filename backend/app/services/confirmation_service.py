"""Confirmation service — create, serialize, and broadcast confirmation cards.

create_confirmation: insert AgentConfirmation into DB, broadcast confirmation_card
    event to web WebSocket clients.
serialize_confirmation_for_display: produce the dict shape consumed by the frontend card.

Task 6 will add resolve_confirmation (approve/reject + resume agent loop).
Task 8 will add non-web channel text fallbacks.
"""

import uuid
from datetime import datetime, timedelta, timezone

from app.database import async_session
from app.models.agent_confirmation import AgentConfirmation

CONFIRMATION_EXPIRY_HOURS = 24


def _action_preview(action: dict | None) -> str:
    if not action:
        return ""
    return str(action.get("tool") or "")


def serialize_confirmation_for_display(c: AgentConfirmation) -> dict:
    return {
        "role": "confirmation",
        "confirmation_id": str(c.id),
        "title": c.title,
        "summary": c.summary,
        "action_preview": _action_preview(c.action),
        "status": c.status,
        "risk_level": c.risk_level,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


async def _broadcast(agent_id: uuid.UUID, conversation_id: str, payload: dict) -> None:
    """Best-effort broadcast to web WebSocket clients. Never raises."""
    try:
        from app.api.websocket import manager  # lazy import — avoids services→api circular dependency

        await manager.send_to_session(str(agent_id), str(conversation_id), payload)
    except Exception:  # best-effort; broadcast failure must not affect DB write
        pass


async def create_confirmation(
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    chat_session_id: uuid.UUID | None,
    source_channel: str,
    title: str,
    summary: str,
    action: dict | None,
    risk_level: str,
    requested_by_user_id: uuid.UUID | None,
) -> AgentConfirmation:
    """Insert an AgentConfirmation row, then broadcast a confirmation_card event.

    Opens its own DB session (not caller-supplied) so it can be called from tool
    execution context that already holds a separate session.
    """
    async with async_session() as db:
        tenant_id = None
        try:
            from app.services.agent_tools import _get_agent_tenant_id  # lazy — agent_tools is heavy

            tenant_id = await _get_agent_tenant_id(agent_id)
        except Exception:
            pass

        c = AgentConfirmation(
            agent_id=agent_id,
            tenant_id=tenant_id,
            conversation_id=str(conversation_id),
            chat_session_id=chat_session_id,
            source_channel=source_channel or "web",
            title=title,
            summary=summary,
            action=action,
            risk_level=risk_level,
            status="pending",
            requested_by_user_id=requested_by_user_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=CONFIRMATION_EXPIRY_HOURS),
        )
        db.add(c)
        await db.commit()
        await db.refresh(c)

    payload = {"type": "confirmation_card", **serialize_confirmation_for_display(c)}
    await _broadcast(agent_id, conversation_id, payload)
    # Non-web channel text fallback: see Task 8
    return c
