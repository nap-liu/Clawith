"""Durable lifecycle state for child Agent executions."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SubagentRun(Base):
    """One resumable child execution backed by a standard ``ChatSession``.

    ``id`` is deliberately shared with the child session, so the public
    ``subagent_id`` is also the ordinary session id. Task text and results live
    in ``ChatMessage``; this row contains lifecycle state only.
    """

    __tablename__ = "subagent_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    parent_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    project_member_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_member_snapshots.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    execution_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    origin_tool_call_id: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "parent_session_id",
            "origin_tool_call_id",
            name="uq_subagent_runs_parent_tool_call",
        ),
        UniqueConstraint(
            "parent_session_id",
            "project_member_id",
            name="uq_subagent_runs_project_group_member",
        ),
        Index("ix_subagent_runs_status_lease", "status", "lease_expires_at"),
    )
