"""Persist human messages through the existing external-agent gateway queue."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.gateway_message import GatewayMessage


async def enqueue_user_gateway_message(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    content: str,
    turn_anchor_id: uuid.UUID | None,
) -> GatewayMessage:
    """Queue and bind the durable anchor; the caller owns the transaction."""
    anchor = await db.get(ChatMessage, turn_anchor_id) if turn_anchor_id else None
    if turn_anchor_id is not None:
        if anchor is None:
            raise RuntimeError("OpenClaw turn anchor disappeared before queue commit")
        if (
            anchor.agent_id != agent_id
            or anchor.conversation_id != conversation_id
            or anchor.user_id != user_id
        ):
            raise PermissionError("Gateway turn anchor ownership changed")

    message = GatewayMessage(
        agent_id=agent_id,
        sender_user_id=user_id,
        conversation_id=conversation_id,
        content=content,
        status="pending",
    )
    db.add(message)
    await db.flush()
    if anchor is not None:
        anchor.message_meta = {
            **dict(anchor.message_meta or {}),
            "gateway_message_id": str(message.id),
        }
    return message
