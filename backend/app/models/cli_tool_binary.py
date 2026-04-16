"""CLI-tool binary version history.

One row per uploaded binary per tool. The authoritative source of truth
for "which binaries exist" lives here; ``Tool.config.binary`` is kept in
sync as a convenience projection of the current version.

Design notes:
- ``is_current=True`` points at the active version. Exactly one row per
  tool_id should have ``is_current=True`` at rest (enforced by a partial
  unique index, see the alembic migration).
- Rollback is a cheap swap: clear the previous current row, set the new
  one. Old rows stay until the retention cap (see
  ``services.cli_tools.versioning.MAX_RETAINED_VERSIONS``) evicts them.
- Hard delete: when a tool is removed, ``ON DELETE CASCADE`` drops every
  version row. The on-disk ``.bin`` files for that tool are removed by
  the existing cascade in ``app.api.cli_tools.delete_cli_tool`` which
  ``BinaryStorage.delete_tool(tenant_key, tool_id)`` handles.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CliToolBinaryVersion(Base):
    """Immutable(-ish) binary upload history for a CLI tool.

    ``is_current`` is the one mutable flag — flipped on rollback. Every
    other field is set at row creation and never changed.
    """

    __tablename__ = "cli_tool_binary_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tool_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tools.id", ondelete="CASCADE"),
        nullable=False,
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Optional free-form reason — "rolled back to v2 after oom fix regression",
    # "ship 1.4.0", etc. Kept short because it goes into audit trails.
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index(
            "ix_cli_tool_binary_versions_tool_id_uploaded_at",
            "tool_id",
            "uploaded_at",
        ),
        # Partial unique index: at most one row per tool_id with
        # is_current=True. Created in the Alembic migration via raw SQL
        # because SQLAlchemy's declarative Index doesn't portably express
        # postgres partial indexes on the ORM side.
    )
