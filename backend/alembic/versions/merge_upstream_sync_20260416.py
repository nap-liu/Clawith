"""Merge upstream sync heads.

Revision ID: merge_sync_20260416
Revises: f8a934bf9f17, increase_api_key_length
Create Date: 2026-04-16
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'merge_sync_20260416'
down_revision: Union[str, Sequence[str]] = ('f8a934bf9f17', 'increase_api_key_length')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
