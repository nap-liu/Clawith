"""mcp_server_placeholder_allowlist

Revision ID: 20260512_mcp_allowlist
Revises: 20260512_mcp_creator
Create Date: 2026-05-12 11:09:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '20260512_mcp_allowlist'
down_revision: Union[str, None] = '20260512_mcp_creator'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column("placeholder_allowlist", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mcp_servers", "placeholder_allowlist")
