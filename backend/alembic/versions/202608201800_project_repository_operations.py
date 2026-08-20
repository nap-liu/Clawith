"""Add durable project repository operation journal.

Revision ID: project_repository_operations
Revises: project_planning_kickoff
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "project_repository_operations"
down_revision: str | Sequence[str] | None = "project_planning_kickoff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("project_repository_operations"):
        return
    op.create_table(
        "project_repository_operations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("operation_type", sa.String(30), server_default="clone", nullable=False),
        sa.Column("state", sa.String(20), server_default="prepared", nullable=False),
        sa.Column("old_head", sa.String(64), nullable=False),
        sa.Column("new_head", sa.String(64), nullable=False),
        sa.Column("backup_name", sa.String(120), nullable=False),
        sa.Column("staging_name", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", name="uq_project_repository_operation_project"),
    )
    op.create_index(
        "ix_project_repository_operations_tenant_id",
        "project_repository_operations",
        ["tenant_id"],
    )
    op.create_index(
        "ix_project_repository_operations_project_id",
        "project_repository_operations",
        ["project_id"],
    )
    op.create_index(
        "ix_project_repository_operations_state",
        "project_repository_operations",
        ["state", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("project_repository_operations")
