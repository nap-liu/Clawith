"""Discover unfinished execution and delivery through durable turn state."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import func, or_, select, text

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.chat_history import (
    is_incomplete_delivery_progress,
    load_recoverable_messages_for_turn,
)
from app.services.conversation_turn_lifecycle import (
    ACTIVE_TURN_STATUS,
    SUSPENDED_TURN_STATUS,
    TERMINAL_TURN_STATUSES,
    ConversationTurnSnapshot,
    conversation_turn_snapshot_for_session,
)
from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_NAME



async def _load_recoverable_anchors(db, *, include_legacy: bool = True) -> list[ChatMessage]:
    """Snapshot durable unfinished work without an execution age cutoff.

    A durable session owner is authoritative even when newer user messages are
    queued behind it. The recent message-tail scan remains as a compatibility
    fallback for turns created before the lifecycle pointer existed.

    This query deliberately has no candidate limit. Startup creates one task
    for every eligible turn; the existing workload-capacity layer remains the
    only execution admission boundary.
    """
    # The public recovery facade re-exports this scanner; import lazily.
    from app.services.turn_recovery import _load_recovery_origin, _metadata_int, _recovery_max_age_hours

    cutoff = datetime.now(UTC) - timedelta(hours=_recovery_max_age_hours())
    latest = (
        select(
            ChatMessage.id.label("id"),
            func.row_number()
            .over(
                partition_by=(ChatMessage.agent_id, ChatMessage.conversation_id),
                order_by=(ChatMessage.created_at.desc(), ChatMessage.id.desc()),
            )
            .label("rn"),
        )
        .where(
            ChatMessage.compacted_into.is_(None),
            ChatMessage.created_at >= cutoff,
        )
        .subquery()
    )
    latest_rows = []
    if include_legacy:
        result = await db.execute(
            select(ChatMessage).join(latest, ChatMessage.id == latest.c.id)
            .where(latest.c.rn == 1).order_by(ChatMessage.created_at, ChatMessage.id)
        )
        latest_rows = list(result.scalars().all())

    recent_session_ids: set[uuid.UUID] = set()
    for row in latest_rows:
        try:
            recent_session_ids.add(uuid.UUID(str(row.conversation_id)))
        except (TypeError, ValueError):
            continue

    # Durable unfinished ownership has no age cutoff. The recent tail is only
    # a fallback for old messages without a lifecycle pointer.
    sessions = list((await db.execute(select(ChatSession).where(or_(
        ChatSession.id.in_(recent_session_ids),
        text("im_config->'conversation_turn'->>'status' IN ('running', 'suspended')"),
    )))).scalars())

    authoritative_sessions: dict[
        tuple[uuid.UUID, str],
        tuple[ChatSession, ConversationTurnSnapshot],
    ] = {}
    owner_anchor_ids: set[uuid.UUID] = set()
    for session in sessions:
        snapshot = conversation_turn_snapshot_for_session(session)
        if snapshot.status not in {ACTIVE_TURN_STATUS, SUSPENDED_TURN_STATUS}:
            continue
        if session.source_channel == "subagent":
            continue
        if snapshot.anchor_id is None:
            continue
        authoritative_sessions[(session.agent_id, str(session.id))] = (
            session,
            snapshot,
        )
        owner_anchor_ids.add(snapshot.anchor_id)

    owner_rows: dict[uuid.UUID, ChatMessage] = {}
    if owner_anchor_ids:
        owner_rows = {
            row.id: row
            for row in (
                (
                    await db.execute(
                        select(ChatMessage).where(ChatMessage.id.in_(owner_anchor_ids))
                    )
                ).scalars()
            )
        }

    anchors: list[ChatMessage] = []
    selected_anchor_ids: set[uuid.UUID] = set()
    for (agent_id, conversation_id), (_session, snapshot) in sorted(
        authoritative_sessions.items(),
        key=lambda item: item[0][1],
    ):
        anchor = owner_rows.get(snapshot.anchor_id)
        meta = (
            dict(anchor.message_meta or {})
            if anchor is not None
            else {}
        )
        if (
            anchor is None
            or anchor.agent_id != agent_id
            or anchor.conversation_id != conversation_id
            or anchor.role not in {"user", "system"}
            or _metadata_int(meta, "turn_generation") != snapshot.generation
            or _metadata_int(meta, "turn_revision") != snapshot.revision
            or str(meta.get("turn_status") or "") != snapshot.status
            or meta.get("consumed_by_onmessage")
            or meta.get("kind") == "on_message_event"
        ):
            logger.warning(
                "[turn_recovery] ignored inconsistent durable owner "
                "conversation={} anchor={}",
                conversation_id,
                snapshot.anchor_id,
            )
            continue
        if await _load_recovery_origin(db, anchor) is None:
            continue
        anchors.append(anchor)
        selected_anchor_ids.add(anchor.id)

    for latest_row in latest_rows:
        if (
            latest_row.agent_id,
            latest_row.conversation_id,
        ) in authoritative_sessions:
            continue
        if not await _latest_row_needs_recovery(db, latest_row):
            continue
        anchor = await _find_turn_anchor_for_latest(db, latest_row)
        if anchor is None or anchor.id in selected_anchor_ids:
            continue
        if await _load_recovery_origin(db, anchor) is None:
            continue
        anchors.append(anchor)
        selected_anchor_ids.add(anchor.id)
    # A reply can be committed before a legacy finalizer or outbound receipt.
    # Such tails must be reconciled even though their turn is already terminal.
    unfinished_completion = list((await db.execute(select(ChatMessage).where(
        text("message_meta->'background_execution'->>'delivered' = 'false'"),
    ))).scalars())
    for anchor in unfinished_completion:
        if anchor.id not in selected_anchor_ids:
            anchors.append(anchor)
            selected_anchor_ids.add(anchor.id)
    pending_replies = list((await db.execute(select(ChatMessage).where(
        text("role = 'assistant' AND message_meta->'delivery'->>'status' = 'pending' "
             "AND message_meta->>'turn_status' IN ('completed','failed','cancelled')"),
        ChatMessage.message_meta["delivery"]["origin"].is_not(None),
    ))).scalars())
    for reply in pending_replies:
        if reply.id not in selected_anchor_ids:
            anchors.append(reply)
            selected_anchor_ids.add(reply.id)
    return sorted(anchors, key=lambda row: (row.created_at, str(row.id)))


async def _latest_row_needs_recovery(db, row: ChatMessage) -> bool:
    from app.services.turn_recovery import _tool_payload

    try:
        session = await db.get(ChatSession, uuid.UUID(str(row.conversation_id)))
    except (TypeError, ValueError):
        session = None
    if session is not None and session.source_channel == "subagent":
        return False
    meta = row.message_meta if isinstance(getattr(row, "message_meta", None), dict) else {}
    if meta.get("consumed_by_onmessage") or meta.get("kind") == "on_message_event":
        return False
    if row.role == "assistant":
        if meta.get("artifact_role") == "intermediate_assistant":
            return True
        return is_incomplete_delivery_progress(row)
    if row.role == "user":
        return str(meta.get("turn_status") or "") not in TERMINAL_TURN_STATUSES
    if row.role != "tool_call":
        return False
    payload = _tool_payload(row)
    if payload is None:
        return False
    if payload.get("name") == REQUEST_CONFIRMATION_TOOL_NAME and payload.get("status") == "pending":
        return False
    return payload.get("status") in {"running", "done", "pending"}


async def _find_turn_anchor_for_latest(db, latest_row: ChatMessage) -> ChatMessage | None:
    candidate = latest_row
    if latest_row.role != "user":
        candidate = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.agent_id == latest_row.agent_id,
                    ChatMessage.conversation_id == latest_row.conversation_id,
                    ChatMessage.role == "user",
                    ChatMessage.compacted_into.is_(None),
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if candidate is None:
        return None

    meta = candidate.message_meta if isinstance(candidate.message_meta, dict) else {}
    injected_root_id = meta.get("subagent_turn_anchor_id")
    if not injected_root_id and meta.get("turn_inbox_state") == "delivered":
        injected_root_id = meta.get("turn_inbox_anchor_id")
        try:
            session_id = uuid.UUID(str(candidate.conversation_id))
            inbox_generation = int(meta.get("turn_inbox_generation") or 0)
        except (TypeError, ValueError):
            return None
        session = await db.get(ChatSession, session_id)
        if session is None or session.agent_id != candidate.agent_id:
            return None
        from app.services.conversation_turn_lifecycle import (
            conversation_turn_snapshot_for_session,
        )

        snapshot = conversation_turn_snapshot_for_session(session)
        if (
            str(snapshot.anchor_id or "") != str(injected_root_id or "")
            or snapshot.generation != inbox_generation
        ):
            return None
    if not injected_root_id:
        return candidate
    try:
        root_id = uuid.UUID(str(injected_root_id))
    except (TypeError, ValueError):
        return None
    root = await db.get(ChatMessage, root_id)
    if (
        root is None
        or root.role != "user"
        or root.agent_id != candidate.agent_id
        or root.conversation_id != candidate.conversation_id
    ):
        return None
    return root


async def _tail_has_pending_confirmation(db, anchor: ChatMessage, *, ctx_size: int) -> bool:
    from app.services.turn_recovery import _tool_payload

    rows = await load_recoverable_messages_for_turn(
        db,
        agent_id=anchor.agent_id,
        conversation_id=anchor.conversation_id,
        turn_anchor_id=anchor.id,
        ctx_size=ctx_size,
    )
    for row in rows:
        if getattr(row, "role", None) != "tool_call":
            continue
        payload = _tool_payload(row)
        if payload and payload.get("name") == REQUEST_CONFIRMATION_TOOL_NAME and payload.get("status") == "pending":
            return True
    return False
