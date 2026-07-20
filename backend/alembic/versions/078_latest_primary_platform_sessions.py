"""Elect the latest created first-party session as primary.

Revision ID: latest_primary_sessions
Revises: canonical_tenant_user
Create Date: 2026-07-20
"""

from typing import Sequence, Union

from alembic import op


revision: str = "latest_primary_sessions"
down_revision: Union[str, Sequence[str], None] = "canonical_tenant_user"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE chat_sessions
        SET is_primary = false
        WHERE source_channel IN ('web', 'wechat_miniprogram')
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
                    PARTITION BY agent_id, user_id, source_channel
                    ORDER BY created_at DESC NULLS LAST, id DESC
                ) AS position
            FROM chat_sessions
            WHERE source_channel IN ('web', 'wechat_miniprogram')
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


def downgrade() -> None:
    # The previous primary choice cannot be reconstructed after re-election.
    pass
