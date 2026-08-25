"""Add the global webhook inbox event sequence.

Revision ID: webhook_event_sequence
Revises: chat_message_conversation_index
"""

from collections.abc import Sequence

from alembic import op


revision: str = "webhook_event_sequence"
down_revision: str | Sequence[str] | None = "chat_message_conversation_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS webhook_event_id_seq AS BIGINT START WITH 1")


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS webhook_event_id_seq")
