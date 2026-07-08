"""Ensure one configured digital employee per DingTalk robot.

Revision ID: dingtalk_unique_robot
Revises: dingtalk_provisioning_sessions
Create Date: 2026-07-08
"""

from typing import Sequence, Union

from alembic import op


revision: str = "dingtalk_unique_robot"
down_revision: Union[str, Sequence[str], None] = "dingtalk_provisioning_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY app_id
                    ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
                ) AS rn
            FROM channel_configs
            WHERE channel_type = 'dingtalk'
              AND is_configured = true
              AND app_id IS NOT NULL
        )
        UPDATE channel_configs AS c
        SET
            app_id = NULL,
            app_secret = NULL,
            encrypt_key = NULL,
            verification_token = NULL,
            is_configured = false,
            is_connected = false,
            extra_config = (
                COALESCE(c.extra_config::jsonb, '{}'::jsonb)
                || jsonb_build_object(
                    'replacement_reason', 'dingtalk_robot_duplicate_migration',
                    'replaced_at', now()::text
                )
            )::json
        FROM ranked
        WHERE c.id = ranked.id
          AND ranked.rn > 1
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_configs_dingtalk_app_configured
        ON channel_configs(app_id)
        WHERE channel_type = 'dingtalk'
          AND is_configured = true
          AND app_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_channel_configs_dingtalk_app_configured")
