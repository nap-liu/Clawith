"""Identity models for managing multiple authentication providers and SSO sessions."""

from enum import Enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AuthProviderType(str, Enum):
    """Supported authentication provider types."""

    FEISHU = "feishu"
    DINGTALK = "dingtalk"
    WECOM = "wecom"
    GOOGLE_WORKSPACE = "google_workspace"
    MICROSOFT_TEAMS = "microsoft_teams"
    GOOGLE = "google"
    GITHUB = "github"


class IdentityProvider(Base):
    """Configuration for external identity providers (Feishu, DingTalk, WeCom, etc.)."""

    __tablename__ = "identity_providers"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id", name="uq_identity_provider_tenant"),
        CheckConstraint(
            "sync_interval_unit IS NULL OR sync_interval_unit IN ('hour', 'day', 'week', 'month')",
            name="ck_identity_providers_sync_interval_unit",
        ),
        CheckConstraint(
            "sync_interval_value IS NULL OR sync_interval_value > 0",
            name="ck_identity_providers_sync_interval_value",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Use plain String instead of PostgreSQL native Enum to stay compatible with the
    # existing production schema (character varying(50)) and avoid type-cast errors.
    provider_type: Mapped[AuthProviderType] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # When True, this provider can be used for SSO login (not just directory sync)
    sso_login_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sso_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config: Mapped[dict] = mapped_column(JSON, default=dict)

    # Directory scheduling is provider-scoped. Login enablement is independent.
    sync_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_interval_value: Mapped[int | None] = mapped_column(Integer)
    sync_interval_unit: Mapped[str | None] = mapped_column(String(10))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_sync_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Optional tenant_id for enterprise-specific providers (no FK - soft coupling)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SSOScanSession(Base):
    """Temporary session for SSO QR code scanning/login."""

    __tablename__ = "sso_scan_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(50), default="pending")  # pending, scanned, authorized, expired, completed
    provider_type: Mapped[AuthProviderType | None] = mapped_column(String(50))
    error_msg: Mapped[str | None] = mapped_column(Text)

    # Context (no FK - soft coupling)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    access_token: Mapped[str | None] = mapped_column(Text)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
