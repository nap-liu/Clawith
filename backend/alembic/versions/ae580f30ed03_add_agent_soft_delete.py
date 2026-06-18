"""add agent soft delete

Revision ID: ae580f30ed03
Revises: 4f0612ed0e72
Create Date: 2026-06-18 06:19:14.165185
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ae580f30ed03'
down_revision: Union[str, None] = '4f0612ed0e72'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("agents", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "deleted_at")
    op.drop_column("agents", "is_deleted")
