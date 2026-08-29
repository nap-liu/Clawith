"""Durable follow-up inbox shared by every external IM transport."""

from __future__ import annotations

import asyncio
import uuid

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession

TURN_INBOX_MAX_MESSAGES = 20
TURN_INBOX_MAX_BYTES = 24 * 1024
TURN_INBOX_CHANNELS = frozenset(
    {
        "dingtalk",
        "feishu",
        "wecom",
        "wechat",
        "slack",
        "discord",
        "teams",
        "microsoft_teams",
        "whatsapp",
    }
)


def is_turn_inbox_channel(source_channel: str | None) -> bool:
    return str(source_channel or "").lower() in TURN_INBOX_CHANNELS


async def _resume_promoted_turn(anchor: ChatMessage) -> None:
    """Own one promoted anchor with bounded event-local retry, never polling."""

    from app.services.redis_lease_lock import RedisLeaseBusyError, RedisLeaseLock
    from app.services.turn_recovery import resume_turn

    last_error: Exception | None = None
    for delay in (0, 0.25, 1, 4, 10):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with RedisLeaseLock(
                str(anchor.id),
                namespace="turn-inbox-resume",
            ):
                if await resume_turn(anchor):
                    return
                raise RuntimeError("promoted turn was not accepted by recovery")
        except RedisLeaseBusyError:
            # Another replica owns the exact anchor. It will either finish the
            # turn or leave the durable running row for restart recovery.
            return
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
    await _fail_exhausted_promoted_turn(anchor, last_error)


async def _fail_exhausted_promoted_turn(
    anchor: ChatMessage,
    error: Exception | None,
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

    asyncio.create_task(
        _resume_promoted_turn(anchor),
        name=f"turn-inbox:{anchor.id}",
    )
    return True


async def drain_turn_inbox(
    *,
    session_id: str,
    active_turn_anchor_id: uuid.UUID,
    execution_agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
) -> list[dict]:
    """Claim a bounded FIFO batch at one ordinary LLM round boundary."""

    try:
        durable_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return []

    async with async_session() as db:
        session = await db.get(ChatSession, durable_session_id, with_for_update=True)
        if session is None or session.agent_id != execution_agent_id:
            return []
        from app.services.conversation_turn_lifecycle import (
            conversation_turn_snapshot_for_session,
        )

        snapshot = conversation_turn_snapshot_for_session(session)
        if (
            snapshot.status != "running"
            or snapshot.anchor_id != active_turn_anchor_id
        ):
            raise asyncio.CancelledError

        candidates = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(session.id),
                        ChatMessage.message_meta["turn_inbox_state"].as_string()
                        == "pending",
                        ChatMessage.message_meta["turn_inbox_mode"].as_string()
                        == "current_turn",
                        ChatMessage.user_id == execution_user_id,
                        ChatMessage.message_meta["turn_inbox_anchor_id"].as_string()
                        == str(active_turn_anchor_id),
                        ChatMessage.message_meta["turn_inbox_generation"].as_integer()
                        == snapshot.generation,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                    .limit(TURN_INBOX_MAX_MESSAGES * 4)
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        selected: list[ChatMessage] = []
        total_bytes = 0
        for row in candidates:
            meta = dict(row.message_meta or {})
            content = str(row.content or "")
            if session.is_group:
                from app.services.sender_attribution import wrap_with_sender

                content = wrap_with_sender(
                    content,
                    row.user_id,
                    meta.get("sender_display_name") or meta.get("sender_nickname"),
                )
            size = len(content.encode("utf-8")) + 256
            if selected and (
                len(selected) >= TURN_INBOX_MAX_MESSAGES
                or total_bytes + size > TURN_INBOX_MAX_BYTES
            ):
                break
            selected.append(row)
            total_bytes += min(size, TURN_INBOX_MAX_BYTES)

        injected: list[dict] = []
        for row in selected:
            meta = dict(row.message_meta or {})
            content = str(row.content or "")
            if session.is_group:
                from app.services.sender_attribution import wrap_with_sender

                content = wrap_with_sender(
                    content,
                    row.user_id,
                    meta.get("sender_display_name") or meta.get("sender_nickname"),
                )
            message = {"role": "user", "content": content}
            attachments = list(meta.get("attachments") or [])
            if attachments:
                message["attachments"] = attachments
            injected.append(message)
            row.message_meta = {**meta, "turn_inbox_state": "delivered"}
        if selected:
            await db.commit()
            from app.services.channel_dispatch import advance_channel_receipt_anchor

            await advance_channel_receipt_anchor([row.id for row in selected])
        return injected
