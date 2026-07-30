"""Add versioned scenes.

Revision ID: agent_scenes
Revises: miniprogram_primary_session
Create Date: 2026-07-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "agent_scenes"
down_revision: Union[str, Sequence[str], None] = "miniprogram_primary_session"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Fresh installations register the current ORM metadata before Alembic
    # runs, so both scene tables may already exist. Existing installations
    # reach this migration without them. Keep the migration safe in both
    # startup paths instead of depending on their execution order.
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    if "agent_scenes" in existing_tables and "agent_scene_revisions" in existing_tables:
        return

    if "agent_scenes" not in existing_tables:
        op.create_table(
            "agent_scenes",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("scene_key", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
            sa.Column("current_revision", sa.Integer(), server_default="0", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("agent_id", "scene_key", name="uq_agent_scenes_agent_key"),
        )
        op.create_index("ix_agent_scenes_agent_id", "agent_scenes", ["agent_id"])
        op.create_index("ix_agent_scenes_tenant_id", "agent_scenes", ["tenant_id"])

    if "agent_scene_revisions" not in existing_tables:
        op.create_table(
            "agent_scene_revisions",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("scene_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
            sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("created_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["created_by_agent_id"], ["agents.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["scene_id"], ["agent_scenes.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("scene_id", "revision", name="uq_agent_scene_revisions_number"),
        )
        op.create_index("ix_agent_scene_revisions_scene_id", "agent_scene_revisions", ["scene_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_tables = set(inspector.get_table_names())
    if "agent_scene_revisions" in existing_tables:
        op.drop_table("agent_scene_revisions")
    if "agent_scenes" in existing_tables:
        op.drop_table("agent_scenes")
