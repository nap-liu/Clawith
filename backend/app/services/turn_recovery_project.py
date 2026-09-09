"""Reconcile project cohort roots without replaying their group input."""

from __future__ import annotations

import uuid

from app.database import async_session
from app.models.chat_session import ChatSession
from app.services.project_group_turn_lifecycle import (
    GROUP_ANCHOR_KINDS,
    reconcile_and_publish_project_group_turn,
)


async def reconcile_project_recovery_anchor(anchor) -> bool | None:
    """A project group root summarizes child Turns; it is not another LLM run."""
    if (anchor.message_meta or {}).get("kind") not in GROUP_ANCHOR_KINDS:
        return None
    try:
        session_id = uuid.UUID(anchor.conversation_id)
    except (TypeError, ValueError):
        return None
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        if (
            session is None
            or session.agent_id != anchor.agent_id
            or session.source_channel != "project"
            or not session.is_group
            or session.project_id is None
        ):
            return None
        project_id = session.project_id
    # Existing ProjectRun/Subagent workers retain execution ownership. Their
    # durable completions and this root share the established cohort reconciler.
    projection = await reconcile_and_publish_project_group_turn(
        project_id=project_id,
        session_id=session_id,
        payload={"type": "turn_state"},
        event_kind="turn_lifecycle",
    )
    return projection is not None
