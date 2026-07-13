"""Baseline legacy on-message recovery at upgrade.

Revision ID: baseline_legacy_onmessage
Revises: onmessage_session_chain
"""

from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "baseline_legacy_onmessage"
down_revision: Union[str, None] = "onmessage_session_chain"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    """Prevent upgraded legacy triggers from replaying pre-cutover history."""
    op.execute(
        sa.text(
            """
            UPDATE agent_triggers
            SET config = config || jsonb_build_object(
                '_legacy_scan_floor', CURRENT_TIMESTAMP
            )
            WHERE type = 'on_message'
              AND COALESCE(config ->> '_watch_session_id', '') = ''
              AND NOT (config ? '_legacy_scan_floor')
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE agent_triggers
            SET config = config - '_legacy_scan_floor'
            WHERE type = 'on_message'
              AND config ? '_legacy_scan_floor'
            """
        )
    )
