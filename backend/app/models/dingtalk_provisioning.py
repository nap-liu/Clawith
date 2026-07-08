"""DingTalk channel provisioning session model."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


DINGTALK_PROVISIONING_STATUS_WAITING = "waiting_for_authorization"
DINGTALK_PROVISIONING_STATUS_POLLING = "polling"
DINGTALK_PROVISIONING_STATUS_CONFIGURED = "configured"
DINGTALK_PROVISIONING_STATUS_FAILED = "failed"
DINGTALK_PROVISIONING_STATUS_EXPIRED = "expired"
DINGTALK_PROVISIONING_STATUS_CANCELLED = "cancelled"

DINGTALK_PROVISIONING_ACTIVE_STATUSES = (
    DINGTALK_PROVISIONING_STATUS_WAITING,
    DINGTALK_PROVISIONING_STATUS_POLLING,
)
DINGTALK_PROVISIONING_TERMINAL_STATUSES = (
    DINGTALK_PROVISIONING_STATUS_CONFIGURED,
    DINGTALK_PROVISIONING_STATUS_FAILED,
    DINGTALK_PROVISIONING_STATUS_EXPIRED,
    DINGTALK_PROVISIONING_STATUS_CANCELLED,
)


class DingTalkChannelProvisioningSession(Base):
    """Persisted DingTalk device-flow session for a digital employee."""

    __tablename__ = "dingtalk_channel_provisioning_sessions"
    __table_args__ = (
        Index(
            "ix_dingtalk_provisioning_due",
            "status",
            "next_poll_at",
            "expires_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    channel_type: Mapped[str] = mapped_column(String(40), nullable=False, default="dingtalk")
    status: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        default=DINGTALK_PROVISIONING_STATUS_WAITING,
        index=True,
    )

    device_code: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    authorization_url: Mapped[str] = mapped_column(Text, nullable=False)
    verification_uri: Mapped[str | None] = mapped_column(Text, nullable=True)

    registration_source: Mapped[str] = mapped_column(String(80), nullable=False, default="openClaw")
    registration_base_url: Mapped[str] = mapped_column(String(255), nullable=False, default="https://oapi.dingtalk.com")
    registration_result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    poll_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_poll_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
