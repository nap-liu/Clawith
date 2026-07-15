"""Organization structure models — departments and members synced from Feishu."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OrgDepartment(Base):
    """Department from Feishu org structure."""

    __tablename__ = "org_departments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_id: Mapped[str | None] = mapped_column(String(100), index=True)
    provider_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)  # No FK - soft coupling

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_departments.id"))
    path: Mapped[str] = mapped_column(String(500), default="")
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    members: Mapped[list["OrgMember"]] = relationship(back_populates="department")
    # provider: Mapped["IdentityProvider | None"] = relationship()  # Removed - use program to query


class OrgMember(Base):
    """Person from an identity provider's org structure."""

    __tablename__ = "org_members"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Generic identity fields (use these instead of provider-specific fields)
    open_id: Mapped[str | None] = mapped_column(Text, index=True)
    unionid: Mapped[str | None] = mapped_column(Text, index=True)
    external_id: Mapped[str | None] = mapped_column(Text, index=True)
    provider_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)  # No FK - soft coupling

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    name_translit_full: Mapped[str | None] = mapped_column(String(255), index=True)
    name_translit_initial: Mapped[str | None] = mapped_column(String(50), index=True)
    email: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str] = mapped_column(String(200), default="")
    department_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("org_departments.id"))
    department_path: Mapped[str] = mapped_column(String(500), default="")
    phone: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="active")
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)  # No FK - soft coupling
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    department: Mapped["OrgDepartment | None"] = relationship(back_populates="members")


class ChannelUserBinding(Base):
    """Internal mapping from a channel-scoped subject to a canonical User.

    ``subject`` is deliberately stored in full.  Provider identifiers must never
    be shortened before lookup because prefixes are not identities.  The
    installation scope is explicit so app-scoped identifiers such as Feishu
    ``open_id`` values cannot collide across installations.
    """

    __tablename__ = "channel_user_bindings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "installation_scope",
            "id_type",
            "subject",
            name="uq_channel_user_binding_subject",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("identity_providers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    installation_scope: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_type: Mapped[str] = mapped_column(String(50), nullable=False)
    id_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User")


class AgentRelationship(Base):
    """Relationship between an agent and an org member."""

    __tablename__ = "agent_relationships"
    __table_args__ = (
        UniqueConstraint("agent_id", "user_id", name="uq_agent_relationship_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Transitional directory/profile pointer. Authorization and relationship
    # identity are based on user_id; legacy channel consumers may still load the
    # associated OrgMember while they migrate to ChannelUserBinding.
    member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("org_members.id", ondelete="SET NULL"), nullable=True
    )
    relation: Mapped[str] = mapped_column(String(50), nullable=False, default="collaborator")
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    member: Mapped["OrgMember | None"] = relationship()
    user = relationship("User", foreign_keys=[user_id])


class AgentAgentRelationship(Base):
    """Relationship between two agents (digital employees)."""

    __tablename__ = "agent_agent_relationships"
    __table_args__ = (
        UniqueConstraint("agent_id", "target_agent_id", name="uq_agent_agent_relationship_target"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    target_agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    relation: Mapped[str] = mapped_column(String(50), nullable=False, default="collaborator")
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=func.now())
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)

    target_agent = relationship("Agent", foreign_keys=[target_agent_id])


class RelationshipSuppression(Base):
    """Durable user intent preventing automatic relationship recreation."""

    __tablename__ = "relationship_suppressions"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "target_type", "target_id", name="uq_relationship_suppression"
        ),
        CheckConstraint(
            "target_type IN ('user', 'agent')",
            name="ck_relationship_suppression_target_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
