"""ChatCompaction: one row per conversation auto-compaction event.

Compaction is the act of taking a span of ``chat_messages`` belonging to
the same session and replacing them in the LLM context with a single
LLM-generated structured summary. The original rows stay in the table —
they're flagged via ``ChatMessage.compacted_into`` pointing here — and
the summary lives in ``ChatCompaction.summary_text``.

``epoch`` numbers consecutive compactions of the same session. When a
later epoch's input includes the prior epoch's summary (progressive
accumulation), the prior epoch's row gets ``superseded_by`` set to the
new epoch's id so loaders only consider the latest active marker per
session.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ChatCompaction(Base):
    __tablename__ = "chat_compactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)

    # Span of original messages that this compaction replaces (inclusive).
    # Nullable because the FK uses ON DELETE SET NULL — a manual purge of
    # old chat_messages should not orphan a compaction record.
    compacted_from_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )
    compacted_to_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="SET NULL"), nullable=True
    )

    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    summary_tokens: Mapped[int] = mapped_column(Integer, nullable=False)

    # Trigger metadata — what we observed when deciding to compact.
    trigger_prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_ratio: Mapped[float] = mapped_column(Float, nullable=False)

    # Progressive accumulation: when a later epoch's input includes this
    # epoch's summary, the new epoch's id goes here.
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_compactions.id", ondelete="SET NULL"), nullable=True
    )

    # Validation gate result — see compactor's gate (length, structure,
    # UUID recall ≥ 0.7). Records that failed validation are kept for
    # audit but the system falls back to ctx_size truncation that round.
    summary_validation_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
