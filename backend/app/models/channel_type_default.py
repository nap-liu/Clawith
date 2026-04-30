"""Channel-type-level prompt defaults.

Single source of truth for the system-prompt block injected when an agent
has any channel of a given type configured (`feishu` / `dingtalk` / etc.).
A per-agent override lives in `channel_configs.system_prompt_block`; this
table is the fallback when that override is NULL.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ChannelTypeDefault(Base):
    __tablename__ = "channel_type_defaults"

    channel_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    system_prompt_block: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
