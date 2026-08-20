"""Add the minimal first-party Skill market.

Revision ID: simple_skill_market
Revises: subagent_runs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "simple_skill_market"
down_revision: str | Sequence[str] | None = "subagent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_name_key")
    op.execute("ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_folder_name_key")

    op.add_column(
        "skills",
        sa.Column("publisher_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "skills",
        sa.Column("publisher_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "skills",
        sa.Column("visibility", sa.String(length=20), server_default="tenant", nullable=False),
    )
    op.add_column(
        "skills",
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
    )
    op.add_column(
        "skills",
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "skills",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_foreign_key(
        "fk_skills_publisher_user_id_users",
        "skills",
        "users",
        ["publisher_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_skills_publisher_agent_id_agents",
        "skills",
        "agents",
        ["publisher_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_skills_publisher_user_id", "skills", ["publisher_user_id"])
    op.create_index("ix_skills_publisher_agent_id", "skills", ["publisher_agent_id"])
    op.create_index(
        "uq_skills_tenant_folder_name",
        "skills",
        ["tenant_id", "folder_name"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )
    op.create_index(
        "uq_skills_global_folder_name",
        "skills",
        ["folder_name"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
    )
    op.create_check_constraint(
        "ck_skills_visibility",
        "skills",
        "visibility IN ('tenant', 'public')",
    )
    op.create_check_constraint(
        "ck_skills_status",
        "skills",
        "status IN ('draft', 'published', 'offline')",
    )
    op.execute(
        """
        UPDATE skills
        SET visibility = 'public', status = 'published'
        WHERE tenant_id IS NULL AND is_builtin = true
        """
    )

    op.create_table(
        "skill_installs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installed_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("installed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("installed_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["installed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["installed_by_agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "agent_id", name="uq_skill_installs_skill_agent"),
    )
    op.create_index("ix_skill_installs_tenant_id", "skill_installs", ["tenant_id"])
    op.create_index("ix_skill_installs_skill_id", "skill_installs", ["skill_id"])
    op.create_index("ix_skill_installs_agent_id", "skill_installs", ["agent_id"])

    op.execute(
        """
        UPDATE agents
        SET autonomy_policy =
            '{"install_skill_from_market":"L3","publish_skill_to_market":"L3"}'::jsonb
            || COALESCE(autonomy_policy::jsonb, '{}'::jsonb)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE agents
        SET autonomy_policy = COALESCE(autonomy_policy::jsonb, '{}'::jsonb)
            - 'install_skill_from_market'
            - 'publish_skill_to_market'
        """
    )
    op.drop_table("skill_installs")

    op.drop_constraint("ck_skills_status", "skills", type_="check")
    op.drop_constraint("ck_skills_visibility", "skills", type_="check")
    op.drop_index("uq_skills_global_folder_name", table_name="skills")
    op.drop_index("uq_skills_tenant_folder_name", table_name="skills")
    op.drop_index("ix_skills_publisher_agent_id", table_name="skills")
    op.drop_index("ix_skills_publisher_user_id", table_name="skills")
    op.drop_constraint("fk_skills_publisher_agent_id_agents", "skills", type_="foreignkey")
    op.drop_constraint("fk_skills_publisher_user_id_users", "skills", type_="foreignkey")

    op.drop_column("skills", "updated_at")
    op.drop_column("skills", "version")
    op.drop_column("skills", "status")
    op.drop_column("skills", "visibility")
    op.drop_column("skills", "publisher_agent_id")
    op.drop_column("skills", "publisher_user_id")

    # A tenant-scoped market can legitimately contain the same display name
    # or folder in different tenants. Preserve every row during rollback by
    # deterministically disambiguating only the duplicate legacy values.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY folder_name ORDER BY created_at, id) AS rn
            FROM skills
        )
        UPDATE skills AS s
        SET folder_name = left(s.folder_name, 90) || '-' || substr(s.id::text, 1, 8)
        FROM ranked AS r
        WHERE s.id = r.id AND r.rn > 1
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY name ORDER BY created_at, id) AS rn
            FROM skills
        )
        UPDATE skills AS s
        SET name = left(s.name, 90) || ' ' || substr(s.id::text, 1, 8)
        FROM ranked AS r
        WHERE s.id = r.id AND r.rn > 1
        """
    )
    op.create_unique_constraint("skills_folder_name_key", "skills", ["folder_name"])
    op.create_unique_constraint("skills_name_key", "skills", ["name"])
