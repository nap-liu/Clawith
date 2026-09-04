"""Task models for digital employees."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, Float, ForeignKey, String, Text, func, true
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Task(Base):
    """Task assigned to or managed by a digital employee."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "NOT (supervision_target_user_id IS NOT NULL "
            "AND supervision_target_agent_id IS NOT NULL)",
            name="ck_task_single_supervision_target",
        ),
        CheckConstraint(
            "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
            name="ck_tasks_temperature",
        ),
        CheckConstraint(
            "reasoning_effort IS NULL OR reasoning_effort IN "
            "('none','minimal','low','medium','high','xhigh','max')",
            name="ck_tasks_reasoning_effort",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(
        Enum("todo", "supervision", name="task_type_enum", create_constraint=False),
        default="todo",
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Enum("pending", "doing", "done", name="task_status_enum"),
        default="pending",
        nullable=False,
    )
    priority: Mapped[str] = mapped_column(
        Enum("low", "medium", "high", "urgent", name="task_priority_enum"),
        default="medium",
        nullable=False,
    )
    assignee: Mapped[str] = mapped_column(String(50), default="self")  # "self" or user_id
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    execution_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "llm_models.id",
            name="fk_tasks_model_id_llm_models",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    soul: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    memory: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Supervision specific fields
    supervision_target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    supervision_target_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )
    # Display snapshot only. Never use this value to resolve or authorize a target.
    supervision_target_name: Mapped[str | None] = mapped_column(String(100))
    supervision_channel: Mapped[str | None] = mapped_column(String(50))
    remind_schedule: Mapped[str | None] = mapped_column(String(100))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def created_by_user_id(self) -> uuid.UUID:
        """Canonical response alias; keep legacy ``created_by`` unchanged."""
        return self.created_by

    # Relationships
    agent: Mapped["Agent"] = relationship(back_populates="tasks", foreign_keys=[agent_id])
    creator: Mapped["User"] = relationship("User", foreign_keys=[created_by])
    logs: Mapped[list["TaskLog"]] = relationship(back_populates="task", cascade="all, delete-orphan")


class TaskLog(Base):
    """Progress log entry for a task."""

    __tablename__ = "task_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    execution_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped["Task"] = relationship(back_populates="logs")


# Resolve forward refs
from app.models.agent import Agent  # noqa: E402, F401
from app.models.user import User  # noqa: E402, F401
