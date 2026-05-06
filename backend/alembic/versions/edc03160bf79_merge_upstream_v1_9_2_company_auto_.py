"""merge upstream v1.9.2 + company auto-compact heads

Revision ID: edc03160bf79
Revises: 20260430_auto_compact_schema, add_onboarding_phase
Create Date: 2026-05-06 08:43:50.288761
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'edc03160bf79'
down_revision: Union[str, None] = ('20260430_auto_compact_schema', 'add_onboarding_phase')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
