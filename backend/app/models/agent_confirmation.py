import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentConfirmation(Base):
    __tablename__ = "agent_confirmations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    # conversation_id 与 ChatMessage.conversation_id 同义(web 下 = str(session_id))
    conversation_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    chat_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_channel: Mapped[str] = mapped_column(String(20), nullable=False, default="web")

    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {"tool": str, "args": dict}
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False, default="medium")

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)

    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
