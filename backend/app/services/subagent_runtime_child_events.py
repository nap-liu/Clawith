"""Deliver a child's completion to a parent that is itself a leased child."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.subagent_run import SubagentRun


async def dispatch_child_parent_event(event_id: uuid.UUID) -> bool:
    from app.services.subagent_runtime import (
        INPUT_PENDING, RUN_CANCELLED, RUN_QUEUED, SUBAGENT_DISPATCH_DELIVERED,
        SUBAGENT_DISPATCH_DISCARDED, SUBAGENT_INPUT, TERMINAL_STATUSES,
        _parent_event_content, _parent_event_external_key, _validate_execution_identity,
    )

    async with async_session() as db:
        event = await db.get(ChatMessage, event_id)
        if event is None:
            return True
        child = await db.get(ChatSession, uuid.UUID(event.conversation_id))
        child_run = await db.get(SubagentRun, child.id) if child else None
        if child_run is None:
            return True
        # Parent Run lock is also the admission/stop boundary for its inbox.
        parent_run = await db.get(SubagentRun, child_run.parent_session_id, with_for_update=True)
        parent = await db.get(ChatSession, child_run.parent_session_id)
        if parent_run is None or parent is None:
            return False
        event = await db.get(ChatMessage, event_id, with_for_update=True)
        if event.message_meta.get("subagent_dispatch_state") in {SUBAGENT_DISPATCH_DELIVERED, SUBAGENT_DISPATCH_DISCARDED}:
            return True
        if (parent_run.status == RUN_CANCELLED or parent.agent_id != child.agent_id
                or parent_run.execution_user_id != child_run.execution_user_id):
            event.message_meta = {**event.message_meta, "subagent_dispatch_state": SUBAGENT_DISPATCH_DISCARDED}
            await db.commit()
            return True
        await _validate_execution_identity(db, parent_run, parent)
        await _validate_execution_identity(db, child_run, child)
        key = _parent_event_external_key(event.id)
        existing = await db.scalar(select(ChatMessage.id).where(ChatMessage.external_event_key == key))
        if existing is None:
            from app.services.conversation_turn_lifecycle import conversation_turn_snapshot_for_session

            prior_anchor = await db.get(ChatMessage, conversation_turn_snapshot_for_session(parent).anchor_id)
            prior_meta = dict(prior_anchor.message_meta or {}) if prior_anchor else {}
            inherited = {key: value for key, value in prior_meta.items()
                         if key.startswith("causal_") or key in {"scene_key", "scene_revision", "activation_source"}}
            row = ChatMessage(
                agent_id=parent.agent_id, user_id=parent_run.execution_user_id,
                sender_agent_id=child.agent_id, conversation_id=str(parent.id), role="user",
                content=_parent_event_content(event, child), external_event_key=key,
                message_meta={
                    **inherited,
                    "kind": SUBAGENT_INPUT, "subagent_input_state": INPUT_PENDING,
                    "subagent_id": str(child.id), "child_message_id": str(event.id),
                    "attachments": event.message_meta.get("attachments", []),
                    "media_result": event.message_meta.get("media_result"),
                },
            )
            db.add(row)
            parent.last_message_at = datetime.now(timezone.utc)
            if parent_run.status in TERMINAL_STATUSES:
                parent_run.status = RUN_QUEUED
                parent_run.lease_owner = None
                parent_run.lease_expires_at = None
        event.message_meta = {**event.message_meta, "subagent_dispatch_state": SUBAGENT_DISPATCH_DELIVERED}
        await db.commit()
    return True
