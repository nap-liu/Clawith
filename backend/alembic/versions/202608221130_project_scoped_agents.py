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
    # Older releases do not understand project scope and would expose these
    # rows in the global Agent directory. A rollback therefore removes the new
    # project domain before dropping its discriminator columns. Production
    # rollback requires the release backup made before migration so project
    # data can be restored when this version is promoted again.
    # Project runtime writes also appear in established audit/session tables.
    # Remove only rows that point at project Agents before deleting the owning
    # project. The catalog loop keeps this compatible with every pre-project
    # table that has a NO ACTION/RESTRICT Agent reference, without encoding a
    # second copy of the platform schema here.
    op.execute("DELETE FROM project_run_member_snapshots")
    op.execute("DELETE FROM project_member_snapshots")
    op.execute(
        """
        DO $$
        DECLARE
            reference record;
        BEGIN
            FOR reference IN
                SELECT namespace.nspname AS schema_name,
                       relation.relname AS table_name,
                       attribute.attname AS column_name
                  FROM pg_constraint constraint_row
                  JOIN pg_class relation
                    ON relation.oid = constraint_row.conrelid
                  JOIN pg_namespace namespace
                    ON namespace.oid = relation.relnamespace
                  JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY key_column(attnum, ord)
                    ON true
                  JOIN pg_attribute attribute
                    ON attribute.attrelid = constraint_row.conrelid
                   AND attribute.attnum = key_column.attnum
                 WHERE constraint_row.contype = 'f'
                   AND constraint_row.confrelid = 'agents'::regclass
                   AND constraint_row.confdeltype IN ('a', 'r')
                   AND relation.relname NOT LIKE 'project_%'
            LOOP
                EXECUTE format(
                    'DELETE FROM %I.%I WHERE %I IN (SELECT id FROM agents WHERE scope = ''project'')',
                    reference.schema_name,
                    reference.table_name,
                    reference.column_name
                );
            END LOOP;
        END
        $$
        """
    )
    op.execute("DELETE FROM projects")
    op.execute("DELETE FROM agents WHERE scope = 'project'")
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
