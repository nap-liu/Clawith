"""Durable per-session LLM token and prompt-cache accounting."""

from __future__ import annotations

import uuid
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.audit import ChatMessage
from app.services.token_tracker import TokenUsage

SESSION_USAGE_META_KEY = "llm_usage"


def _usage_from_meta(meta: dict | None) -> TokenUsage:
    payload = (meta or {}).get(SESSION_USAGE_META_KEY)
    if not isinstance(payload, dict):
        return TokenUsage()
    usage = TokenUsage()
    for field_name in asdict(usage):
        try:
            setattr(usage, field_name, max(0, int(payload.get(field_name) or 0)))
        except (TypeError, ValueError):
            continue
    return usage


async def persist_turn_token_usage(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    turn_anchor_id: uuid.UUID | None,
    usage: TokenUsage,
) -> None:
    """Merge one completed logical turn's usage into its durable user anchor."""
    if turn_anchor_id is None or usage.total_tokens <= 0:
        return
    async with async_session() as db:
        anchor = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.id == turn_anchor_id,
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == str(session_id),
                    ChatMessage.role == "user",
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if anchor is None:
            return
        accumulated = _usage_from_meta(anchor.message_meta)
        accumulated.add(usage)
        meta = dict(anchor.message_meta or {})
        meta[SESSION_USAGE_META_KEY] = asdict(accumulated)
        anchor.message_meta = meta
        await db.commit()


async def load_session_token_usage(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
) -> tuple[TokenUsage, int]:
    """Aggregate all tracked logical turns in one ChatSession."""
    rows = (
        await db.execute(
            select(ChatMessage.message_meta).where(
                ChatMessage.agent_id == agent_id,
                ChatMessage.conversation_id == str(session_id),
                ChatMessage.role == "user",
            )
        )
    ).scalars().all()
    total = TokenUsage()
    tracked_turns = 0
    for meta in rows:
        usage = _usage_from_meta(meta)
        if usage.total_tokens <= 0:
            continue
        total.add(usage)
        tracked_turns += 1
    return total, tracked_turns
