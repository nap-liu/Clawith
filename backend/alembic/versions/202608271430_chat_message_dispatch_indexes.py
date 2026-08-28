"""Add active chat-message dispatch indexes.

Revision ID: chat_message_dispatch_indexes
Revises: repair_tenant_boundary_triggers
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "chat_message_dispatch_indexes"
down_revision: str | Sequence[str] | None = "repair_tenant_boundary_triggers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAMES = (
    "ix_chat_messages_subagent_dispatch_pending",
    "ix_chat_messages_leader_dispatch_pending",
)


def _invalid_indexes() -> set[str]:
    bind = op.get_bind()
    invalid: set[str] = set()
    for name in INDEX_NAMES:
        is_invalid = bind.execute(
            text(
                """
                SELECT NOT index_state.indisvalid
                FROM pg_class AS index_relation
                JOIN pg_index AS index_state
                  ON index_state.indexrelid = index_relation.oid
                JOIN pg_namespace AS namespace
                  ON namespace.oid = index_relation.relnamespace
                WHERE namespace.nspname = current_schema()
                  AND index_relation.relname = :name
                """
            ),
            {"name": name},
        ).scalar_one_or_none()
        if is_invalid:
            invalid.add(name)
    return invalid


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    invalid = _invalid_indexes()
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        for name in INDEX_NAMES:
            if name in invalid:
                op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                ix_chat_messages_subagent_dispatch_pending
            ON chat_messages (created_at, id)
            WHERE (message_meta->>'subagent_dispatch_state') = 'pending'
            """
        )
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                ix_chat_messages_leader_dispatch_pending
            ON chat_messages (conversation_id, created_at, id)
            WHERE (message_meta->>'kind') = 'project_subagent_reply'
              AND (message_meta->>'leader_batch_state') IN ('pending', 'claimed')
            """
        )
        op.execute("RESET lock_timeout")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "ix_chat_messages_leader_dispatch_pending"
        )
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS "
            "ix_chat_messages_subagent_dispatch_pending"
        )
