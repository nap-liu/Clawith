"""Stable IM groups and observed participants, independent of Sessions."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentGroup(Base):
    __tablename__ = "agent_groups"
    __table_args__ = (
        UniqueConstraint("agent_id", "channel", "installation_scope", "external_group_id",
                         name="uq_agent_groups_identity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    installation_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    external_group_id: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")
    rules: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentGroupMember(Base):
    """A provider subject observed in one group, not a second person directory."""

    __tablename__ = "agent_group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "subject_type", "subject", name="uq_agent_group_member_subject"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_groups.id", ondelete="CASCADE"), index=True)
    subject_type: Mapped[str] = mapped_column(String(20), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="", server_default="")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
