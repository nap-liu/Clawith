"""Add H5 primary platform session uniqueness.

Revision ID: h5_primary_platform_sessions
Revises: im_thinking_output_settings
Create Date: 2026-07-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "h5_primary_platform_sessions"
down_revision: Union[str, Sequence[str], None] = "im_thinking_output_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    index_names = {idx["name"] for idx in inspector.get_indexes("chat_sessions")}

    if "uq_chat_sessions_primary_h5_platform" not in index_names:
        op.create_index(
            "uq_chat_sessions_primary_h5_platform",
            "chat_sessions",
            ["agent_id", "user_id"],
            unique=True,
            postgresql_where=sa.text(
                "is_primary = true AND source_channel = 'wechat_miniprogram' AND is_group = false"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    index_names = {idx["name"] for idx in inspector.get_indexes("chat_sessions")}

    if "uq_chat_sessions_primary_h5_platform" in index_names:
        op.drop_index("uq_chat_sessions_primary_h5_platform", table_name="chat_sessions")
