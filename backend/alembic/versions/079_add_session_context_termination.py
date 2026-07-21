"""Add persistent context termination marker to chat sessions.

Revision ID: session_context_termination
Revises: latest_primary_sessions
Create Date: 2026-07-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "session_context_termination"
down_revision: Union[str, Sequence[str], None] = "latest_primary_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("context_terminated_reason", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "context_terminated_reason")
