"""Single source of truth for loading chat history into LLM context.

Before this module, every channel handler (websocket / feishu / dingtalk /
wecom / discord / teams / slack) carried its own copy of the same SELECT —
desc + limit(ctx_size) + reverse + map to ``[{"role", "content"}]``. The
duplication made it hard to evolve history-handling (compaction, image
rehydration, …) without touching seven files at once.

The module exposes two layers:

* ``load_messages_for_session`` — returns the chronologically-ordered
  ``ChatMessage`` rows. Channels that need raw access (websocket splits
  ``tool_call`` rows into assistant + tool pairs) call this and shape
  themselves.
* ``load_history_for_llm`` — convenience wrapper that produces the
  ``[{"role", "content"}]`` shape used by the IM channels. Optional
  vision rehydration through ``rehydrate_images_max``.

The split lets the upcoming auto-compaction layer hook at the row level
(swap out compacted spans with synthetic summary rows) without touching
each channel's shaping code.

This commit is a behavior-preserving refactor — the row set returned is
identical to what each channel produced before. Compaction-aware logic
lands in a follow-up commit so the refactor diff stays auditable.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage


async def load_messages_for_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    ctx_size: int,
) -> list[ChatMessage]:
    """Return the last ``ctx_size`` ``ChatMessage`` rows for this
    (agent, conversation), oldest-first.

    Channels that need to peek at fields beyond ``role`` / ``content``
    (e.g. ``websocket.py`` splits ``tool_call`` rows into an assistant +
    tool pair using stored JSON) call this directly and do their own
    shaping.
    """
    result = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(ctx_size)
    )
    return list(reversed(result.scalars().all()))


async def load_history_for_llm(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    ctx_size: int,
    rehydrate_images_max: int | None = None,
) -> list[dict[str, Any]]:
    """Return ``[{"role", "content"}]`` history ready to feed an LLM call.

    Used by IM channels (feishu / dingtalk / wecom / discord / teams /
    slack) that don't need raw row access.

    Args:
        db: Active SQLAlchemy async session.
        agent_id: Filter messages to this agent.
        conversation_id: Channel-specific session key (e.g.
            ``feishu_p2p_<open_id>``, ``dingtalk_p2p_<staff_id>``, web
            ``conversation_id`` UUID).
        ctx_size: Per-agent ``context_window_size`` cap on message count.
        rehydrate_images_max: When set, post-process with
            ``image_context.rehydrate_image_messages`` so vision models
            still see prior image uploads. Only the most recent ``N``
            images are inlined to keep request size sane.
    """
    rows = await load_messages_for_session(
        db,
        agent_id=agent_id,
        conversation_id=conversation_id,
        ctx_size=ctx_size,
    )
    history: list[dict[str, Any]] = [
        {"role": m.role, "content": m.content} for m in rows
    ]

    if rehydrate_images_max is not None:
        # Lazy import: image_context pulls in vision deps that not all
        # deployments need. Only loaded when a vision-capable channel asks.
        from app.services.image_context import rehydrate_image_messages
        history = rehydrate_image_messages(
            history, agent_id, max_images=rehydrate_images_max
        )

    return history
