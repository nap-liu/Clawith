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
        # The production entrypoint runs metadata.create_all() before Alembic.
        # On an existing database that creates newly introduced project tables
        # from the current ORM metadata, so this column can already exist even
        # though the Alembic revision has not run yet. Keep the migration safe
        # for both that bootstrap path and a normal Alembic-only upgrade.
        inspector = sa.inspect(op.get_bind())
        if "execution_user_id" not in {
            column["name"] for column in inspector.get_columns(table_name)
        }:
            op.add_column(
                table_name,
                sa.Column("execution_user_id", postgresql.UUID(as_uuid=True), nullable=True),
            )

        foreign_key_name = f"fk_{table_name}_execution_user_id_users"
        inspector = sa.inspect(op.get_bind())
        if foreign_key_name not in {
            foreign_key["name"] for foreign_key in inspector.get_foreign_keys(table_name)
        }:
            op.create_foreign_key(
                foreign_key_name,
                table_name,
                "users",
                ["execution_user_id"],
                ["id"],
                ondelete="RESTRICT" if table_name == "project_runs" else "SET NULL",
            )

        index_name = f"ix_{table_name}_execution_user_id"
        inspector = sa.inspect(op.get_bind())
        if index_name not in {
            index["name"] for index in inspector.get_indexes(table_name)
        }:
            op.create_index(
                index_name,
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
