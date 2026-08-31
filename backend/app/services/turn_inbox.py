"""Durable follow-up inbox shared by every external IM transport."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services import turn_inbox_promoted as _turn_inbox_promoted
from app.services.turn_inbox_drain import drain_turn_inbox
from app.services.turn_inbox_receipts import (
    _acknowledge_live_receipt_handoff,
    acknowledge_durable_channel_receipt_cleanup,
    bind_durable_channel_receipt_anchor,
    cleanup_durable_channel_receipt_anchor,
    cleanup_stale_channel_receipt_anchors,
    durable_channel_receipt_anchor_id,
    load_durable_channel_receipt_anchor,
)
from app.services.turn_inbox_shared import (
    CHANNEL_RECEIPT_ANCHOR_KEY,
    CHANNEL_RECEIPT_CLEANUP_BATCH_SIZE,
    CHANNEL_RECEIPT_CLEANUP_DIAGNOSTIC_RING_SIZE,
    CHANNEL_RECEIPT_CLEANUP_DIAGNOSTICS_KEY,
    CHANNEL_RECEIPT_CLEANUP_MAX_ATTEMPTS,
    CHANNEL_RECEIPT_PROVIDER_META_KEY,
    CHANNEL_RECEIPT_STARTUP_MAX_BATCHES,
    TURN_INBOX_CHANNELS,
    TURN_INBOX_MAX_BYTES,
    TURN_INBOX_MAX_MESSAGES,
    _marker_cleanup_message_ids,
    is_turn_inbox_channel,
)


async def _resume_promoted_turn(anchor: ChatMessage) -> None:
    await _turn_inbox_promoted.resume_promoted_turn(
        anchor,
        fail_exhausted_promoted_turn=_fail_exhausted_promoted_turn,
    )


def schedule_durable_turn_resume(anchor: ChatMessage) -> None:
    """Resume any admitted durable turn after its foreground owner exits."""

    asyncio.create_task(
        _resume_promoted_turn(anchor),
        name=f"durable-turn-resume:{anchor.id}",
    )


async def _fail_exhausted_promoted_turn(
    anchor: ChatMessage,
    error: Exception | None,
) -> None:
    await _turn_inbox_promoted.fail_exhausted_promoted_turn(
        anchor,
        error,
        promote_next_turn_inbox=promote_next_turn_inbox,
        kick_promoted_turn_inbox=kick_promoted_turn_inbox,
    )


async def promote_next_turn_inbox(
    db,
    *,
    session: ChatSession,
) -> uuid.UUID | None:
    """Atomically admit the oldest deferred input after one turn terminates."""

    root = (
        await db.execute(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(session.id),
                ChatMessage.message_meta["turn_inbox_state"].as_string()
                == "pending",
            )
            .order_by(ChatMessage.created_at, ChatMessage.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if root is None:
        return None
    from app.services.conversation_turn_lifecycle import transition_conversation_turn

    promoted = await transition_conversation_turn(
        db,
        agent_id=session.agent_id,
        conversation_id=str(session.id),
        turn_anchor_id=root.id,
        status="running",
    )
    root.message_meta = {
        **dict(root.message_meta or {}),
        "turn_inbox_state": "promoted",
        "turn_inbox_anchor_id": str(root.id),
        "turn_inbox_generation": promoted.generation,
        "turn_inbox_mode": "current_turn",
    }
    await db.flush()
    return root.id


async def kick_promoted_turn_inbox(
    *,
    agent_id: uuid.UUID,
    session_id: str,
) -> bool:
    """Start a committed promoted input; startup recovery is the durable fallback."""

    try:
        durable_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return False
    async with async_session() as db:
        session = await db.get(ChatSession, durable_session_id)
        if session is None or session.agent_id != agent_id:
            return False
        from app.services.conversation_turn_lifecycle import (
            conversation_turn_snapshot_for_session,
        )

        snapshot = conversation_turn_snapshot_for_session(session)
        if snapshot.status != "running" or snapshot.anchor_id is None:
            return False
        anchor = await db.get(ChatMessage, snapshot.anchor_id)
        if anchor is None or dict(anchor.message_meta or {}).get("turn_inbox_state") != "promoted":
            return False

    schedule_durable_turn_resume(anchor)
    return True
