"""Add project-scoped Agent identity and lineage.

Revision ID: project_scoped_agents
Revises: agent_file_activity_actions
Create Date: 2026-08-22 11:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "project_scoped_agents"
down_revision: str | Sequence[str] | None = "agent_file_activity_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("scope", sa.String(length=20), server_default="standard", nullable=False),
    )
    op.add_column(
        "agents",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column("source_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("agents", sa.Column("agent_dir", sa.String(length=500), nullable=True))

    op.create_foreign_key(
        "fk_agents_project_id_projects",
        "agents",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_agents_source_agent_id_agents",
        "agents",
        "agents",
        ["source_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_agents_scope",
        "agents",
        "scope IN ('standard', 'project')",
    )
    op.create_check_constraint(
        "ck_agents_project_scope",
        "agents",
        "(scope = 'standard' AND project_id IS NULL) OR "
        "(scope = 'project' AND project_id IS NOT NULL AND agent_dir IS NOT NULL)",
    )
    op.create_index("ix_agents_project_id", "agents", ["project_id"], unique=False)
    op.create_index("ix_agents_source_agent_id", "agents", ["source_agent_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_agents_source_agent_id", table_name="agents")
    op.drop_index("ix_agents_project_id", table_name="agents")
    op.drop_constraint("ck_agents_project_scope", "agents", type_="check")
    op.drop_constraint("ck_agents_scope", "agents", type_="check")
    op.drop_constraint("fk_agents_source_agent_id_agents", "agents", type_="foreignkey")
    op.drop_constraint("fk_agents_project_id_projects", "agents", type_="foreignkey")
    op.drop_column("agents", "agent_dir")
    op.drop_column("agents", "source_agent_id")
    op.drop_column("agents", "project_id")
    op.drop_column("agents", "scope")
