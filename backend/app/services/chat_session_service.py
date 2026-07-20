"""Helpers for first-party chat session selection and creation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.session_identity import require_same_tenant_session_user


async def get_latest_platform_session(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    source_channel: str = "web",
) -> ChatSession | None:
    """Return the newest first-party session for one user+agent+channel."""

    result = await db.execute(
        select(ChatSession)
        .where(
            ChatSession.agent_id == agent_id,
            ChatSession.user_id == user_id,
            ChatSession.source_channel == source_channel,
            ChatSession.is_group == False,
        )
        .order_by(ChatSession.created_at.desc().nulls_last(), ChatSession.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def promote_platform_session(
    db: AsyncSession,
    session: ChatSession,
) -> ChatSession:
    """Make ``session`` the sole primary for its user+agent+channel."""

    if session.user_id is None or session.is_group:
        return session
    if not session.is_primary:
        await db.execute(
            update(ChatSession)
            .where(
                ChatSession.agent_id == session.agent_id,
                ChatSession.user_id == session.user_id,
                ChatSession.source_channel == session.source_channel,
                ChatSession.is_group == False,
                ChatSession.is_primary == True,
                ChatSession.id != session.id,
            )
            .values(is_primary=False)
        )
        session.is_primary = True
        await db.flush()
    return session


async def ensure_primary_platform_session(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    source_channel: str = "web",
) -> ChatSession:
    """Return a guaranteed primary platform session for a given user+agent pair.

    The newest created session is always primary. Runtime reconciliation keeps
    the invariant intact after the one-time data migration.
    """

    await require_same_tenant_session_user(db, agent_id, user_id)
    latest = await get_latest_platform_session(
        db,
        agent_id,
        user_id,
        source_channel=source_channel,
    )
    if latest:
        return await promote_platform_session(db, latest)

    now = datetime.now(timezone.utc)
    session = ChatSession(
        agent_id=agent_id,
        user_id=user_id,
        title=f"Session {now.strftime('%m-%d %H:%M')}",
        source_channel=source_channel,
        is_primary=True,
        created_at=now,
    )
    db.add(session)
    await db.flush()
    return session


async def save_tool_call_log(
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: str,
    tool_name: str,
    arguments: dict | None,
    result: str,
    status: str = "done",
    tool_call_id: str | None = None,
    reasoning_content: str | None = None,
) -> None:
    """Save a tool call execution log into chat history as a ChatMessage."""
    if not conversation_id:
        return
    import json
    from app.database import async_session
    from loguru import logger

    payload = {
        "name": tool_name,
        "args": arguments or {},
        "status": status,
        "result": str(result) if result is not None else "",
        "tool_call_id": tool_call_id,
        "reasoning_content": reasoning_content,
    }

    try:
        async with async_session() as db:
            db.add(ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(payload, ensure_ascii=False, default=str),
                conversation_id=conversation_id,
            ))
            await db.commit()
    except Exception as e:
        logger.warning(f"Failed to save tool call log: {e}")
