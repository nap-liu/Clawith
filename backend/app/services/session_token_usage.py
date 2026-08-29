"""Durable per-session LLM token and prompt-cache accounting."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.audit import ChatMessage
from app.services.token_tracker import TokenUsage

SESSION_USAGE_META_KEY = "llm_usage"
SESSION_CONTEXT_META_KEY = "llm_context_usage"


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
                    ChatMessage.compacted_into.is_(None),
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


async def persist_round_context_usage(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    turn_anchor_id: uuid.UUID | None,
    input_tokens: int,
    output_tokens: int,
    provider: str,
    model: str,
    model_record_id: str = "",
    endpoint: str = "",
    usage_details: dict | None = None,
) -> None:
    """Backfill authoritative provider usage after every completed LLM round.

    Keep a compact rolling record on the durable turn anchor instead of an
    unbounded per-round array.  ``last_input_tokens`` is the exact size of the
    most recently dispatched complete prompt.  Historical peaks are not a
    capacity signal: after the prompt changes, only the latest provider
    measurement describes the context that will be sent next.  Local
    character/byte estimates must never call this function.
    """
    if turn_anchor_id is None or input_tokens <= 0:
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
                    ChatMessage.compacted_into.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if anchor is None:
            return
        meta = dict(anchor.message_meta or {})
        previous = meta.get(SESSION_CONTEXT_META_KEY)
        if not isinstance(previous, dict):
            previous = {}
        try:
            measured_rounds = max(0, int(previous.get("measured_rounds") or 0))
        except (TypeError, ValueError):
            measured_rounds = 0
        payload = {
            "source": "provider_usage",
            "provider": str(provider),
            "model": str(model),
            "model_record_id": str(model_record_id),
            "endpoint": str(endpoint),
            "last_input_tokens": int(input_tokens),
            "last_output_tokens": max(0, int(output_tokens)),
            "measured_rounds": measured_rounds + 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if isinstance(usage_details, dict) and usage_details:
            payload["provider_usage_details"] = usage_details
        meta[SESSION_CONTEXT_META_KEY] = payload
        anchor.message_meta = meta
        await db.commit()


async def load_latest_round_context_usage(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    provider: str,
    model: str,
    model_record_id: str = "",
    endpoint: str = "",
) -> int | None:
    """Load the latest compatible authoritative prompt size for a session."""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(ChatMessage.message_meta)
                .where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == str(session_id),
                    ChatMessage.role == "user",
                    ChatMessage.compacted_into.is_(None),
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(32)
            )
        ).scalars().all()
    expected = (
        str(provider),
        str(model),
        str(model_record_id),
        str(endpoint),
    )
    for meta in rows:
        payload = (meta or {}).get(SESSION_CONTEXT_META_KEY)
        if not isinstance(payload, dict) or payload.get("source") != "provider_usage":
            continue
        observed = (
            str(payload.get("provider") or ""),
            str(payload.get("model") or ""),
            str(payload.get("model_record_id") or ""),
            str(payload.get("endpoint") or ""),
        )
        if observed != expected:
            continue
        try:
            value = int(payload.get("last_input_tokens") or 0)
        except (TypeError, ValueError):
            continue
        return value if value > 0 else None
    return None


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
