"""Tenant catalog visibility and explicit Skill installation updates."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "skill_management"
down_revision = "agent_login_links"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "skill_tenant_policies",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "skill_update_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("folder_name", sa.String(100), nullable=False),
        sa.Column("files", postgresql.JSONB(), nullable=False),
        sa.Column("targets", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_skill_update_jobs_skill_id", "skill_update_jobs", ["skill_id"])


def downgrade():
    op.drop_table("skill_update_jobs")
    op.drop_table("skill_tenant_policies")
