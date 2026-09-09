"""Commit every admitted anchor before interrupting its owning root execution."""

from __future__ import annotations

import asyncio
import uuid

from loguru import logger

from app.database import async_session
from app.models.audit import AuditLog
from app.services.active_turns import (
    finalize_active_turn_stop,
    list_active_turns,
    release_active_turn_stop,
    reserve_active_turn_stop,
)
from app.services.chat_history import mark_turn_cancelled, turn_has_completed_reply


async def commit_reserved_turn_stop(
    *,
    record,
    stop_token: str,
    reason: str,
    audit_action: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    is_admin: bool = False,
) -> str:
    """Finish the existing reservation protocol before sending task cancellation."""
    completed_anchors = 0
    cancelled_anchors = []
    try:
        async with async_session() as db:
            for anchor in tuple(record.durable_anchors):
                cancelled_id = await mark_turn_cancelled(
                    db,
                    agent_id=anchor.agent_id,
                    conversation_id=anchor.session_id,
                    turn_anchor_id=anchor.message_id,
                    reason=reason,
                )
                if cancelled_id is None and await turn_has_completed_reply(
                    db,
                    agent_id=anchor.agent_id,
                    conversation_id=anchor.session_id,
                    turn_anchor_id=anchor.message_id,
                ):
                    completed_anchors += 1
                elif cancelled_id is not None:
                    cancelled_anchors.append(anchor)
            if audit_action is not None:
                db.add(AuditLog(
                    user_id=actor_user_id,
                    agent_id=None,
                    action=audit_action,
                    details={
                        "turn_id": record.turn_id,
                        "turn_owner_user_id": str(record.owner_user_id),
                        "target_agent_id": str(record.agent_id),
                        "session_id": record.session_id,
                        "turn_type": record.turn_type,
                        "platform_admin": is_admin,
                        "completed_anchor_count": completed_anchors,
                        "durable_anchor_count": len(record.durable_anchors),
                    },
                ))
            await db.commit()
    except BaseException:
        await release_active_turn_stop(record, stop_token)
        raise

    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
        publish_conversation_turn_event,
    )

    try:
        for anchor in cancelled_anchors:
            try:
                async with async_session() as db:
                    snapshot = await get_conversation_turn_snapshot(
                        db,
                        agent_id=anchor.agent_id,
                        conversation_id=anchor.session_id,
                        turn_anchor_id=anchor.message_id,
                    )
                await publish_conversation_turn_event(
                    agent_id=anchor.agent_id,
                    conversation_id=anchor.session_id,
                    payload={"type": "done", "role": "assistant", "content": ""},
                    snapshot=snapshot,
                    event_kind="turn_terminal",
                )
            except Exception:
                logger.exception("[turn_control] committed STOP event failed for anchor={}", anchor.message_id)
    finally:
        # STOP is already durable. Observer read/publication failures must not
        # leave its reservation frozen or prevent interrupting the execution.
        if record.durable_anchors and completed_anchors == len(record.durable_anchors):
            await release_active_turn_stop(record, stop_token)
            outcome = "completed"
        else:
            finalized = await finalize_active_turn_stop(record, stop_token)
            outcome = "cancelled" if finalized is not None else "ended"
    return outcome


async def stop_local_registered_turns(anchors: dict[str, str], *, reason: str) -> None:
    """Apply an already-authorized durable STOP to matching local execution owners.

    The registry also contains nested A2A anchors, which are ordinary Sessions
    rather than SubagentRun descendants. Freeze their existing admission gate,
    persist their cancellation, then interrupt the one shared execution.
    """
    for candidate in await list_active_turns():
        if not any(anchors.get(anchor.session_id) == str(anchor.message_id)
                   for anchor in candidate.durable_anchors):
            continue
        record, token = await reserve_active_turn_stop(candidate.turn_id)
        if record is None or token is None:
            continue
        control_task = asyncio.create_task(commit_reserved_turn_stop(
            record=record, stop_token=token, reason=reason,
        ))
        interrupted = False
        while True:
            try:
                await asyncio.shield(control_task)
                break
            except asyncio.CancelledError:
                interrupted = True
                if control_task.done():
                    control_task.result()
                    break
        if interrupted:
            raise asyncio.CancelledError
