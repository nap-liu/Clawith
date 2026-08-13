"""Published page models for HTML hosting and access control."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PublishedPage(Base):
    """An HTML page published from an agent workspace."""

    __tablename__ = "published_pages"
    __table_args__ = (
        CheckConstraint("access_mode IN ('public', 'authenticated', 'restricted')", name="ck_published_pages_access_mode"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    short_id: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    last_published_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="")
    access_mode: Mapped[str] = mapped_column(String(20), default="public", server_default="public", nullable=False)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PublishedPageAccess(Base):
    """A user's approved access or pending/rejected access request."""

    __tablename__ = "published_page_access"
    __table_args__ = (
        UniqueConstraint("page_id", "user_id", name="uq_published_page_access_page_user"),
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_published_page_access_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("published_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="approved", nullable=False)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class PublishedPageVisitor(Base):
    """Aggregated visits by authenticated users; anonymous visits stay anonymous."""

    __tablename__ = "published_page_visitors"
    __table_args__ = (
        UniqueConstraint("page_id", "user_id", name="uq_published_page_visitors_page_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("published_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    view_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PublishedPageAnonymousVisitor(Base):
    """A repeat anonymous browser visiting a public page."""

    __tablename__ = "published_page_anonymous_visitors"
    __table_args__ = (
        UniqueConstraint("page_id", "visitor_key", name="uq_published_page_anonymous_visitor"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    page_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("published_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    visitor_key: Mapped[str] = mapped_column(String(64), nullable=False)
    view_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    first_viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_viewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
