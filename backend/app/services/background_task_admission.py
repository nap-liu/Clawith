"""Durable acceptance for task-like adapters; execution belongs to the turn core."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession


async def create_background_turn(
    db: AsyncSession,
    *,
    agent: Agent,
    kind: str,
    reference_id: uuid.UUID,
    user_prompt: str,
    execution_user_id: uuid.UUID | None,
    title: str | None = None,
    settings: dict | None = None,
    completion: dict | None = None,
) -> ChatMessage:
    """Save one occurrence and its immutable input in the caller's transaction."""
    from app.services.background_turns import initialize_background_turn

    anchor_id = uuid.uuid5(reference_id, f"{kind}:anchor")
    existing = await db.get(ChatMessage, anchor_id)
    if existing is not None:
        return existing
    session_id = uuid.uuid5(reference_id, f"{kind}:session")
    now = datetime.now(UTC)
    session = ChatSession(
        id=session_id,
        agent_id=agent.id,
        project_id=agent.project_id,
        user_id=None,
        source_channel="trigger",
        external_conv_id=f"{kind}:{reference_id}",
        title=(title or user_prompt)[:200],
        is_primary=False,
        is_group=False,
        last_message_at=now,
    )
    anchor = ChatMessage(
        id=anchor_id,
        agent_id=agent.id,
        user_id=None,
        role="user",
        content=user_prompt,
        conversation_id=str(session_id),
        external_event_key=f"background:{agent.id}:{kind}:{reference_id}",
        message_meta={"source_channel": session.source_channel},
        created_at=now,
    )
    db.add_all([session, anchor])
    await db.flush()
    # The insert hook attributes user rows to a human. Background rows carry
    # execution authority without claiming that the executor authored input.
    anchor.user_id = execution_user_id
    execution_settings = dict(settings or {})
    execution_settings["execution_user_id"] = str(execution_user_id) if execution_user_id else None
    return await initialize_background_turn(
        db,
        session=session,
        anchor=anchor,
        kind=kind,
        reference_id=str(reference_id),
        settings=execution_settings,
        completion=completion,
    )


def resource_execution_settings(resource) -> dict:
    """Snapshot the shared model and memory controls without runtime objects."""
    return {
        "model_override_id": str(resource.model_id) if resource.model_id else None,
        "temperature_override": resource.temperature,
        "reasoning_effort_override": resource.reasoning_effort,
        "include_soul": resource.soul,
        "include_memory": resource.memory,
        "max_tool_rounds_override": 50,
    }
