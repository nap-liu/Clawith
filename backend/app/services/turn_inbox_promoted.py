from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from loguru import logger

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession

FailExhaustedPromotedTurn = Callable[[ChatMessage, Exception | None], Awaitable[None]]
KickPromotedTurnInbox = Callable[..., Awaitable[bool]]
PromoteNextTurnInbox = Callable[..., Awaitable[uuid.UUID | None]]


async def resume_promoted_turn(
    anchor: ChatMessage,
    *,
    fail_exhausted_promoted_turn: FailExhaustedPromotedTurn,
) -> None:
    """Own one promoted anchor with bounded event-local retry, never polling."""

    from app.services.redis_lease_lock import RedisLeaseBusyError, RedisLeaseLock
    from app.services.turn_recovery import resume_turn

    last_error: Exception | None = None
    for delay in (0, 0.25, 1, 4, 10):
        if delay:
            await asyncio.sleep(delay)
        resume_owner = False
        try:
            async with RedisLeaseLock(
                str(anchor.id),
                namespace="turn-inbox-resume",
            ):
                resume_owner = True
                if await resume_turn(anchor):
                    return
                raise RuntimeError("promoted turn was not accepted by recovery")
        except RedisLeaseBusyError as exc:
            if not resume_owner:
                return
            last_error = exc
            logger.warning(
                "[turn_inbox] conversation lease busy during resume "
                "anchor={} delay={}",
                anchor.id,
                delay,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(
                "[turn_inbox] promoted turn retry anchor={} delay={}: {}",
                anchor.id,
                delay,
                exc,
            )
    await fail_exhausted_promoted_turn(anchor, last_error)


async def fail_exhausted_promoted_turn(
    anchor: ChatMessage,
    error: Exception | None,
    *,
    promote_next_turn_inbox: PromoteNextTurnInbox,
    kick_promoted_turn_inbox: KickPromotedTurnInbox,
) -> None:
    """Release an exhausted promoted owner instead of leaving it running."""

    async with async_session() as db:
        session = await db.get(
            ChatSession,
            uuid.UUID(str(anchor.conversation_id)),
            with_for_update=True,
        )
        if session is None or session.agent_id != anchor.agent_id:
            return
        from app.services.conversation_turn_lifecycle import (
            conversation_turn_snapshot_for_session,
            transition_conversation_turn,
        )

        snapshot = conversation_turn_snapshot_for_session(session)
        if snapshot.status != "running" or snapshot.anchor_id != anchor.id:
            return
        failed = await transition_conversation_turn(
            db,
            agent_id=anchor.agent_id,
            conversation_id=anchor.conversation_id,
            turn_anchor_id=anchor.id,
            status="failed",
        )
        next_anchor_id = await promote_next_turn_inbox(
            db,
            session=session,
        )
        await db.commit()

    from app.services.conversation_turn_lifecycle import (
        publish_conversation_turn_event,
    )

    await publish_conversation_turn_event(
        agent_id=anchor.agent_id,
        conversation_id=anchor.conversation_id,
        payload={
            "type": "done",
            "role": "assistant",
            "content": "",
            "error": str(error or "promoted turn recovery exhausted")[:200],
        },
        snapshot=failed,
        event_kind="turn_terminal",
    )
    if next_anchor_id is not None:
        await kick_promoted_turn_inbox(
            agent_id=anchor.agent_id,
            session_id=anchor.conversation_id,
        )
