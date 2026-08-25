"""Freeze the selected execution user on projects and project runs.

Revision ID: project_execution_users
Revises: project_scoped_agents
Create Date: 2026-08-25 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "project_execution_users"
down_revision: str | Sequence[str] | None = "project_scoped_agents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table_name in ("projects", "project_runs"):
        op.add_column(
            table_name,
            sa.Column("execution_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_{table_name}_execution_user_id_users",
            table_name,
            "users",
            ["execution_user_id"],
            ["id"],
            ondelete="RESTRICT" if table_name == "project_runs" else "SET NULL",
        )
        op.create_index(
            f"ix_{table_name}_execution_user_id",
            table_name,
            ["execution_user_id"],
            unique=False,
        )


def downgrade() -> None:
    for table_name in ("project_runs", "projects"):
        op.drop_index(f"ix_{table_name}_execution_user_id", table_name=table_name)
        op.drop_constraint(
            f"fk_{table_name}_execution_user_id_users",
            table_name,
            type_="foreignkey",
        )
        op.drop_column(table_name, "execution_user_id")
