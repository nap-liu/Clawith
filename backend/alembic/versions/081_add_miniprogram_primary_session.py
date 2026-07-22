"""Add the neutral mini-program primary-session index.

Revision ID: miniprogram_primary_session
Revises: oauth_subject_bindings
Create Date: 2026-07-22
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "miniprogram_primary_session"
down_revision: Union[str, Sequence[str], None] = "oauth_subject_bindings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE chat_sessions
        SET is_primary = false
        WHERE source_channel = 'miniprogram'
          AND COALESCE(is_group, false) = false
          AND is_primary = true
        """
    )
    op.execute(
        """
        WITH ranked_sessions AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY agent_id, user_id
                    ORDER BY created_at DESC NULLS LAST, id DESC
                ) AS position
            FROM chat_sessions
            WHERE source_channel = 'miniprogram'
              AND COALESCE(is_group, false) = false
              AND user_id IS NOT NULL
        )
        UPDATE chat_sessions AS session
        SET is_primary = true
        FROM ranked_sessions AS ranked
        WHERE session.id = ranked.id
          AND ranked.position = 1
        """
    )

    bind = op.get_bind()
    index_names = {index["name"] for index in sa.inspect(bind).get_indexes("chat_sessions")}
    if "uq_chat_sessions_primary_miniprogram" not in index_names:
        op.create_index(
            "uq_chat_sessions_primary_miniprogram",
            "chat_sessions",
            ["agent_id", "user_id"],
            unique=True,
            postgresql_where=sa.text(
                "is_primary = true AND source_channel = 'miniprogram' AND is_group = false"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    index_names = {index["name"] for index in sa.inspect(bind).get_indexes("chat_sessions")}
    if "uq_chat_sessions_primary_miniprogram" in index_names:
        op.drop_index(
            "uq_chat_sessions_primary_miniprogram",
            table_name="chat_sessions",
        )
