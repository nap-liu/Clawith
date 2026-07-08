"""Add IM thinking output settings.

Revision ID: im_thinking_output_settings
Revises: dingtalk_unique_robot
Create Date: 2026-07-08
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "im_thinking_output_settings"
down_revision: Union[str, Sequence[str], None] = "dingtalk_unique_robot"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "im_thinking_output_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "chat_sessions",
        sa.Column(
            "im_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "im_config")
    op.drop_column("agents", "im_thinking_output_enabled")
