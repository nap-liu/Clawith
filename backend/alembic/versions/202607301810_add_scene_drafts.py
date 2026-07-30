"""Add saved scene drafts.

Revision ID: scene_drafts
Revises: agent_scenes
Create Date: 2026-07-30
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "scene_drafts"
down_revision: Union[str, Sequence[str], None] = "agent_scenes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("agent_scenes"):
        return
    columns = {column["name"] for column in inspector.get_columns("agent_scenes")}
    if "draft_config" in columns:
        return
    op.add_column(
        "agent_scenes",
        sa.Column("draft_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("agent_scenes"):
        return
    columns = {column["name"] for column in inspector.get_columns("agent_scenes")}
    if "draft_config" in columns:
        op.drop_column("agent_scenes", "draft_config")
