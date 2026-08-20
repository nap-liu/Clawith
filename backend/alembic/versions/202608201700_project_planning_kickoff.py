"""Make planning the default state for newly created AI-native projects.

Revision ID: project_planning_kickoff
Revises: project_group_sessions
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "project_planning_kickoff"
down_revision: str | Sequence[str] | None = "project_group_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("projects") as batch:
            batch.alter_column(
                "status",
                existing_type=sa.String(length=20),
                server_default="planning",
                existing_nullable=False,
            )
    else:
        op.alter_column(
            "projects",
            "status",
            existing_type=sa.String(length=20),
            server_default="planning",
            existing_nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("projects") as batch:
            batch.alter_column(
                "status",
                existing_type=sa.String(length=20),
                server_default="initializing",
                existing_nullable=False,
            )
    else:
        op.alter_column(
            "projects",
            "status",
            existing_type=sa.String(length=20),
            server_default="initializing",
            existing_nullable=False,
        )
