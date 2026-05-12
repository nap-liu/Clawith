"""MCP server registry — one row per (tenant_id, server URL).

Replaces the prior pattern of repeating ``mcp_server_url`` / ``instructions`` /
``system_prompt_block`` across every tool row of the same server. Tools FK
into ``mcp_servers``; per-tenant and per-agent overrides live in
``mcp_server_overrides``.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MCPServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    name: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(200))
    base_url_template: Mapped[str] = mapped_column(Text)
    headers_template: Mapped[dict] = mapped_column(JSONB, default=dict)
    credential_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt_block: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MCPServerOverride(Base):
    __tablename__ = "mcp_server_overrides"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mcp_servers.id", ondelete="CASCADE"),
    )
    scope_type: Mapped[str] = mapped_column(String(10))  # 'tenant' | 'agent'
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    system_prompt_block: Mapped[str | None] = mapped_column(Text, nullable=True)
    url_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    headers_template: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    credential_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_modified_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("mcp_server_id", "scope_type", "scope_id", name="uq_mcp_override_server_scope"),
        CheckConstraint("scope_type IN ('tenant', 'agent')", name="ck_mcp_override_scope_type"),
    )
