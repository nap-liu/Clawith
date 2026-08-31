"""Single source of truth for loading chat history into LLM context.

Before this module, every channel handler (websocket / feishu / dingtalk /
wecom / discord / teams / slack) carried its own history query and row slice.
The duplication made it hard to evolve history-handling (complete-turn
protection, compaction, attachment handling, …) without touching seven files at
once.

The module exposes two layers:

* ``load_messages_for_session`` — returns the chronologically-ordered
  message stream, with compaction-aware injection: rows whose
  ``compacted_into`` is non-NULL are filtered out, and the active
  ``ChatCompaction`` marker (if any) is prepended as a synthetic
  user-role message wrapped in ``<conversation-summary>``. Channels
  that need raw access (websocket splits ``tool_call`` rows into
  assistant + tool pairs) call this and shape themselves.
* ``load_history_for_llm`` — convenience wrapper that produces the
  provider-neutral message shape used by the IM channels.

The summary is reconstructed at load time from ``chat_compactions``;
it is **not** persisted to ``chat_messages`` (the chat UI shows the
original rows; only the LLM context sees the synthetic summary).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.chat_attachments import normalize_chat_message_attachments
from app.services.sender_attribution import wrap_with_sender
from app.services.user_output import sanitize_user_visible_text

if TYPE_CHECKING:
    from app.services.confirmation_service import PendingConfirmation

HIDDEN_ONBOARDING_ANCHOR_KIND = "onboarding_turn_anchor"
HIDDEN_PROJECT_CONTINUATION_ANCHOR_KIND = "project_subagent_external_continuation"
HIDDEN_RUNTIME_ANCHOR_KINDS = frozenset(
    {
        HIDDEN_ONBOARDING_ANCHOR_KIND,
        HIDDEN_PROJECT_CONTINUATION_ANCHOR_KIND,
    }
)
INTERMEDIATE_ASSISTANT_ARTIFACT_ROLE = "intermediate_assistant"


def _is_hidden_runtime_anchor(row: Any) -> bool:
    metadata = getattr(row, "message_meta", None)
    return bool(
        isinstance(metadata, dict)
        and metadata.get("kind") in HIDDEN_RUNTIME_ANCHOR_KINDS
    )


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
    sender_user_id: uuid.UUID | None = None
    sender_agent_id: uuid.UUID | None = None
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
    token_attribute = (
        f' tokens="{marker.summary_tokens}"'
        if marker.summary_tokens is not None
        else ""
    )
    body = (
        f'<conversation-summary epoch="{marker.epoch}"{token_attribute} '
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


def _prepend_compaction_context(
    rows: list[Any],
    *,
    marker: ChatCompaction,
    agent_id: uuid.UUID,
    conversation_id: str,
) -> None:
    prefix: list[Any] = [
        _build_summary_message(
            marker=marker,
            agent_id=agent_id,
            conversation_id=conversation_id,
        )
    ]
    rows[0:0] = prefix




__all__ = [name for name in globals() if not name.startswith("__")]
