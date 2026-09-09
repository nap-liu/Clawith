"""Current-turn drain logic for the durable turn inbox."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import String, cast, func, select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.chat_history import build_llm_message_from_row
from app.services.turn_inbox_receipts import _acknowledge_live_receipt_handoff
from app.services.turn_inbox_shared import (
    CHANNEL_RECEIPT_ANCHOR_KEY,
    TURN_INBOX_MAX_BYTES,
    TURN_INBOX_MAX_MESSAGES,
    _marker_cleanup_message_ids,
)


async def drain_turn_inbox(
    *,
    session_id: str,
    active_turn_anchor_id: uuid.UUID,
    execution_agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    before_injection=None,
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

        anchor = await db.get(ChatMessage, active_turn_anchor_id)
        if anchor is None:
            raise asyncio.CancelledError
        target_agent_id = str(
            (anchor.message_meta or {}).get("execution_agent_id") or anchor.agent_id
        )
        candidates = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(session.id),
                        ChatMessage.message_meta["turn_inbox_state"].as_string()
                        == "pending",
                        func.coalesce(
                            ChatMessage.message_meta["execution_agent_id"].as_string(),
                            cast(ChatMessage.agent_id, String),
                        ) == target_agent_id,
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

        consumed_at = max(datetime.now(UTC), anchor.created_at + timedelta(microseconds=2))
        if selected and before_injection is not None:
            await before_injection(
                created_at=consumed_at - timedelta(microseconds=1)
            )

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
            if "external_context" in meta:
                sender = row.sender_user_id or row.user_id
                message = build_llm_message_from_row(
                    row, wrap_user_names=session.is_group,
                    name_map={sender: meta.get("sender_display_name") or meta.get("sender_nickname")},
                )
            injected.append(message)
            # The live session snapshot fences the consumer. Pending inputs
            # belong to the conversation, including a prior owner's backlog.
            row.message_meta = {
                **meta,
                "turn_inbox_state": "delivered",
                "turn_inbox_mode": "current_turn",
                "turn_inbox_anchor_id": str(active_turn_anchor_id),
                "turn_inbox_generation": snapshot.generation,
                "turn_inbox_consumed_at": consumed_at.isoformat(),
            }
        marker_advanced = False
        if selected:
            prior_marker = dict(session.im_config or {}).get(
                CHANNEL_RECEIPT_ANCHOR_KEY
            )
            from app.services.channel_reaction_recovery import (
                supports_recovered_channel_reactions,
            )

            tracks_reactions = isinstance(
                prior_marker,
                dict,
            ) or supports_recovered_channel_reactions(session.source_channel)
            if tracks_reactions:
                cleanup_message_ids = (
                    _marker_cleanup_message_ids(prior_marker)
                    if isinstance(prior_marker, dict)
                    else []
                )
                for row in selected:
                    if row.id not in cleanup_message_ids:
                        cleanup_message_ids.append(row.id)
                session.im_config = {
                    **dict(session.im_config or {}),
                    CHANNEL_RECEIPT_ANCHOR_KEY: {
                        "message_id": str(selected[-1].id),
                        "turn_anchor_id": str(active_turn_anchor_id),
                        "generation": snapshot.generation,
                        "cleanup_message_ids": [
                            str(message_id) for message_id in cleanup_message_ids
                        ],
                        "cleanup_attempts": (
                            dict(prior_marker.get("cleanup_attempts") or {})
                            if isinstance(prior_marker, dict) and isinstance(
                                prior_marker.get("cleanup_attempts"), dict
                            )
                            else {}
                        ),
                    },
                }
                marker_advanced = True
            await db.commit()
            from app.services.channel_dispatch import advance_channel_receipt_anchor

            previous_cleanup_completed = await advance_channel_receipt_anchor(
                [row.id for row in selected]
            )
            if previous_cleanup_completed and marker_advanced:
                await _acknowledge_live_receipt_handoff(
                    session_id=durable_session_id,
                    agent_id=execution_agent_id,
                    turn_anchor_id=active_turn_anchor_id,
                    generation=snapshot.generation,
                    message_id=selected[-1].id,
                )
        return injected
