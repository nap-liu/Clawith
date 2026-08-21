"""Add the chat-message conversation lookup index.

Revision ID: chat_message_conversation_index
Revises: simple_skill_market
"""

from collections.abc import Sequence

from alembic import op


revision: str = "chat_message_conversation_index"
down_revision: str | Sequence[str] | None = "simple_skill_market"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # This table is large and receives live chat traffic. Building the index
    # concurrently keeps ordinary reads and writes available during rollout.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_chat_messages_conversation_id ON chat_messages (conversation_id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_chat_messages_conversation_id")
