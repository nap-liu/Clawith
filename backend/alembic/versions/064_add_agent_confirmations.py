"""add agent_confirmations

Revision ID: add_agent_confirmations
Revises: merge_agent_mgmt_heads
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "add_agent_confirmations"
down_revision: Union[str, Sequence[str], None] = "merge_agent_mgmt_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("agent_confirmations"):
        return
    op.create_table(
        "agent_confirmations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", sa.String(length=200), nullable=False),
        sa.Column("chat_session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_channel", sa.String(length=20), nullable=False, server_default="web"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("action", postgresql.JSONB(), nullable=True),
        sa.Column("risk_level", sa.String(length=10), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agent_confirmations_agent_id", "agent_confirmations", ["agent_id"])
    op.create_index("ix_agent_confirmations_conversation_id", "agent_confirmations", ["conversation_id"])
    op.create_index("ix_agent_confirmations_status", "agent_confirmations", ["status"])
    op.create_index("ix_agent_confirmations_tenant_id", "agent_confirmations", ["tenant_id"])
    op.create_index("ix_agent_confirmations_created_at", "agent_confirmations", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if "agent_confirmations" in sa.inspect(bind).get_table_names():
        op.drop_table("agent_confirmations")
