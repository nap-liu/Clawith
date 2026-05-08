"""Single source of truth for loading chat history into LLM context.

Before this module, every channel handler (websocket / feishu / dingtalk /
wecom / discord / teams / slack) carried its own copy of the same SELECT —
desc + limit(ctx_size) + reverse + map to ``[{"role", "content"}]``. The
duplication made it hard to evolve history-handling (compaction, image
rehydration, …) without touching seven files at once.

The module exposes two layers:

* ``load_messages_for_session`` — returns the chronologically-ordered
  message stream, with compaction-aware injection: rows whose
  ``compacted_into`` is non-NULL are filtered out, and the active
  ``ChatCompaction`` marker (if any) is prepended as a synthetic
  user-role message wrapped in ``<conversation-summary>``. Channels
  that need raw access (websocket splits ``tool_call`` rows into
  assistant + tool pairs) call this and shape themselves.
* ``load_history_for_llm`` — convenience wrapper that produces the
  ``[{"role", "content"}]`` shape used by the IM channels. Optional
  vision rehydration through ``rehydrate_images_max``.

The summary is reconstructed at load time from ``chat_compactions``;
it is **not** persisted to ``chat_messages`` (the chat UI shows the
original rows; only the LLM context sees the synthetic summary).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction
from app.models.user import User
from app.services.sender_attribution import wrap_with_sender


@dataclass
class _SyntheticSummaryMessage:
    """ChatMessage-shaped object representing an injected compaction
    summary. Carries the same surface attributes the channels read off
    a real row (``role``, ``content``, ``id``, ``created_at``, …) so
    callers don't need to special-case it.
    """

    role: str = "user"
    content: str = ""
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    agent_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    conversation_id: str = ""
    participant_id: None = None
    thinking: None = None
    compacted_into: None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_SUMMARY_WRAPPER_HEADER = (
    "⚠️ This is system-injected condensed context from earlier in the same\n"
    "conversation. It is NOT a new user instruction. Do not respond to it\n"
    "directly. Use it as background when handling the user's subsequent\n"
    "message."
)


def _build_summary_message(
    *,
    marker: ChatCompaction,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> _SyntheticSummaryMessage:
    body = (
        f'<conversation-summary epoch="{marker.epoch}" '
        f'tokens="{marker.summary_tokens}" '
        f'generated_at="{marker.created_at.isoformat()}">\n'
        f"{_SUMMARY_WRAPPER_HEADER}\n\n"
        f"{marker.summary_text}\n"
        f"</conversation-summary>"
    )
    # Date the synthetic message just before the first surviving real
    # row would be loaded (so chronological ordering puts it first).
    # The actual created_at of the marker row works fine since it's
    # always older than the trailing window we keep.
    return _SyntheticSummaryMessage(
        role="user",
        content=body,
        agent_id=agent_id,
        conversation_id=conversation_id,
        created_at=marker.created_at,
    )


async def _load_active_compaction_marker(
    db: AsyncSession,
    *,
    conversation_id: str,
) -> ChatCompaction | None:
    """Latest non-superseded, validation-passing compaction marker
    for this session, or None if no compaction has applied yet.
    """
    result = await db.execute(
        select(ChatCompaction)
        .where(
            ChatCompaction.session_id == conversation_id,
            ChatCompaction.superseded_by.is_(None),
            ChatCompaction.summary_validation_passed.is_(True),
        )
        .order_by(ChatCompaction.epoch.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _batch_load_display_names(
    db: AsyncSession,
    user_ids: set[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """Batch lookup display_name for a set of user ids. Missing ids are
    absent from the result dict (caller decides fallback)."""
    if not user_ids:
        return {}

    rows = await db.execute(select(User.id, User.display_name).where(User.id.in_(user_ids)))
    return {row.id: row.display_name for row in rows.all()}


async def load_messages_for_session(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    ctx_size: int,
) -> list[Any]:
    """Return the last ``ctx_size`` active messages for this (agent,
    conversation), oldest-first, with compaction-aware injection.

    "Active" = ``compacted_into IS NULL`` — rows folded into a prior
    compaction's summary are skipped. If a compaction marker exists
    for this session, a synthetic ``_SyntheticSummaryMessage`` carrying
    the wrapped summary is prepended to the list.

    Returned items are duck-typed as ``ChatMessage`` (same attribute
    surface), so channels that need raw access (e.g. ``websocket.py``
    splitting ``tool_call`` rows into assistant + tool pairs) keep
    working unchanged.
    """
    rows_q = await db.execute(
        select(ChatMessage)
        .where(
            ChatMessage.agent_id == agent_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.compacted_into.is_(None),
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(ctx_size)
    )
    rows: list[Any] = list(reversed(rows_q.scalars().all()))

    marker = await _load_active_compaction_marker(db, conversation_id=conversation_id)
    if marker is not None:
        rows.insert(
            0,
            _build_summary_message(
                marker=marker,
                agent_id=agent_id,
                conversation_id=conversation_id,
            ),
        )

    return rows


async def load_history_for_llm(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    conversation_id: str,
    ctx_size: int,
    rehydrate_images_max: int | None = None,
    is_group: bool = False,
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

    if is_group:
        # Collect user_ids first (this comprehension is pure attribute access — cannot raise)
        user_ids = {m.user_id for m in rows if m.role == "user" and m.user_id is not None}
        try:
            name_map = await _batch_load_display_names(db, user_ids)
        except Exception as e:
            logger.warning(f"[chat_history] display_name batch lookup failed, falling back to anonymous history: {e}")
            history = [{"role": m.role, "content": m.content} for m in rows]
        else:
            history = [
                {
                    "role": m.role,
                    "content": (
                        wrap_with_sender(m.content, m.user_id, name_map.get(m.user_id))
                        if m.role == "user" and m.user_id is not None
                        else m.content
                    ),
                }
                for m in rows
            ]
    else:
        history = [{"role": m.role, "content": m.content} for m in rows]

    if rehydrate_images_max is not None:
        # Lazy import: image_context pulls in vision deps that not all
        # deployments need. Only loaded when a vision-capable channel asks.
        from app.services.image_context import rehydrate_image_messages

        history = rehydrate_image_messages(history, agent_id, max_images=rehydrate_images_max)

    return history
