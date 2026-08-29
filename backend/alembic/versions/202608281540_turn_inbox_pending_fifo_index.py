"""Add the bounded external-IM inbox FIFO lookup index.

Revision ID: turn_inbox_pending_fifo
Revises: chat_message_dispatch_indexes
"""

from collections.abc import Sequence

from alembic import op


revision: str = "turn_inbox_pending_fifo"
down_revision: str | Sequence[str] | None = "chat_message_dispatch_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_chat_messages_turn_inbox_pending_fifo"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        op.execute(
            f"""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME}
            ON chat_messages (conversation_id, created_at, id)
            WHERE (message_meta->>'turn_inbox_state') = 'pending'
            """
        )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}")
