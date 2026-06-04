"""add webhook_queue_max to agents

Revision ID: 72382a243406
Revises: dab5b3231188
Create Date: 2026-06-04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "72382a243406"
down_revision: Union[str, None] = "dab5b3231188"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("webhook_queue_max", sa.Integer(), nullable=False, server_default="1000"))


def downgrade() -> None:
    op.drop_column("agents", "webhook_queue_max")
