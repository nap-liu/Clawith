"""Add durable Subagent Soul and memory loading controls.

Revision ID: subagent_context_flags
Revises: subagent_runs
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "subagent_context_flags"
down_revision: str | Sequence[str] | None = "subagent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("subagent_runs")
    }
    if "soul" not in columns:
        op.add_column(
            "subagent_runs",
            sa.Column(
                "soul",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )
    if "memory" not in columns:
        op.add_column(
            "subagent_runs",
            sa.Column(
                "memory",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )


def downgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("subagent_runs")
    }
    if "memory" in columns:
        op.drop_column("subagent_runs", "memory")
    if "soul" in columns:
        op.drop_column("subagent_runs", "soul")
