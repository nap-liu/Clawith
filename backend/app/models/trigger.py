"""Agent trigger model — self-managed wake conditions for autonomous agents."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentTrigger(Base):
    """A trigger that an agent sets for itself to be woken up at a specific time or condition.

    Trigger types:
    - cron: croniter expression, e.g. {"expr": "0 9 * * 1-5"}
    - once: fire at a specific time, e.g. {"at": "2026-03-10T09:00:00+08:00"}
    - interval: fire every N minutes, e.g. {"minutes": 30}
    - poll: HTTP poll with change detection, e.g. {"url": "...", "json_path": "$.status", ...}
    - on_message: fire when receiving a message from a specific agent
    """

    __tablename__ = "agent_triggers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    execution_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )
    model_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "llm_models.id",
            name="fk_agent_triggers_model_id_llm_models",
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
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # cron|once|interval|poll|on_message
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    focus_ref: Mapped[str | None] = mapped_column(String(200))  # optional: related focus item identifier
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fire_count: Mapped[int] = mapped_column(Integer, default=0)
    max_fires: Mapped[int | None] = mapped_column(Integer)  # None = unlimited
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=60)  # 1 min default
    # System triggers (seeded by platform) cannot be deleted by users, only enabled/disabled
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("agent_id", "name", name="uq_agent_trigger_name"),
        CheckConstraint(
            "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
            name="ck_agent_triggers_temperature",
        ),
        CheckConstraint(
            "reasoning_effort IS NULL OR reasoning_effort IN "
            "('none','minimal','low','medium','high','xhigh','max')",
            name="ck_agent_triggers_reasoning_effort",
        ),
    )
