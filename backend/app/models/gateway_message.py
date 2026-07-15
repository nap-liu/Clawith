"""Gateway messages for OpenClaw agent communication."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GatewayMessage(Base):
    """Message queued for delivery to an OpenClaw agent.

    Lifecycle: pending → delivered → completed (or expired).
    """

    __tablename__ = "gateway_messages"
    __table_args__ = (
        CheckConstraint(
            "NOT (sender_agent_id IS NOT NULL AND sender_user_id IS NOT NULL)",
            name="ck_gateway_messages_single_canonical_sender",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Target OpenClaw agent
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    # Canonical sender. A queued row may be system-originated (both NULL), but
    # a human and a digital employee can never be asserted simultaneously.
    sender_agent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"))
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    # Chat session tracking for routing responses back
    conversation_id: Mapped[str | None] = mapped_column(String(100))
    # Message content
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Status tracking
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # pending | delivered | completed
    result: Mapped[str | None] = mapped_column(Text)
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GatewaySendReceipt(Base):
    """Durable idempotency receipt for one proactive Gateway send request."""

    __tablename__ = "gateway_send_receipts"
    __table_args__ = (
        UniqueConstraint(
            "source_agent_id",
            "idempotency_key",
            name="uq_gateway_send_receipts_source_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    response_payload: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
