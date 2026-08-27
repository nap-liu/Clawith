"""Allow a restored project member to receive a fresh durable child session.

Revision ID: project_member_generations
Revises: project_repository_operations
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "project_member_generations"
down_revision: str | Sequence[str] | None = "project_repository_operations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "uq_subagent_runs_project_group_member"


def _has_constraint() -> bool:
    return any(
        item.get("name") == CONSTRAINT
        for item in sa.inspect(op.get_bind()).get_unique_constraints("subagent_runs")
    )


def upgrade() -> None:
    if not _has_constraint():
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("subagent_runs") as batch:
            batch.drop_constraint(CONSTRAINT, type_="unique")
    else:
        op.drop_constraint(CONSTRAINT, "subagent_runs", type_="unique")


def downgrade() -> None:
    if _has_constraint():
        return
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("subagent_runs") as batch:
            batch.create_unique_constraint(CONSTRAINT, ["parent_session_id", "project_member_id"])
    else:
        op.create_unique_constraint(
            CONSTRAINT,
            "subagent_runs",
            ["parent_session_id", "project_member_id"],
        )
