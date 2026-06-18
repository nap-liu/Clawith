"""add pat scope

Revision ID: 4f0612ed0e72
Revises: add_personal_access_tokens
Create Date: 2026-06-18 04:35:21.786426
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4f0612ed0e72'
down_revision: Union[str, None] = 'add_personal_access_tokens'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "personal_access_tokens",
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="read"),
    )


def downgrade() -> None:
    op.drop_column("personal_access_tokens", "scope")
