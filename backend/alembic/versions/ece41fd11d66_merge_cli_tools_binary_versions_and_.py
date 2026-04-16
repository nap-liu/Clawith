"""merge cli-tools binary_versions and upstream_sync heads

Revision ID: ece41fd11d66
Revises: 20260416_binary_versions, merge_sync_20260416
Create Date: 2026-04-16 09:17:24.072243
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ece41fd11d66'
down_revision: Union[str, None] = ('20260416_binary_versions', 'merge_sync_20260416')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
