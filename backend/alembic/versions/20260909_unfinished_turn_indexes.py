"""Index unfinished execution discovery without scanning conversation history."""

from alembic import op
from sqlalchemy import text

revision = "unfinished_turn_indexes"
down_revision = "merge_openapi_mcp_index"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_chat_messages_background_unfinished", "chat_messages", "created_at, id",
     "message_meta->'background_execution'->>'delivered' = 'false'"),
    ("ix_chat_sessions_active_turn", "chat_sessions", "id",
     "im_config->'conversation_turn'->>'status' IN ('running', 'suspended')"),
    ("ix_chat_messages_terminal_pending", "chat_messages", "created_at, id",
     "role = 'assistant' AND message_meta->'delivery'->>'status' = 'pending' "
     "AND message_meta->>'turn_status' IN ('completed','failed','cancelled')"),
)


def upgrade():
    connection = op.get_bind()
    with op.get_context().autocommit_block():
        for name, table, columns, predicate in INDEXES:
            valid = connection.scalar(text(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"
            ), {"name": name})
            if valid is False:
                op.execute(f"DROP INDEX CONCURRENTLY {name}")
            op.execute(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} ({columns}) WHERE {predicate}")


def downgrade():
    with op.get_context().autocommit_block():
        for name, _, _, _ in reversed(INDEXES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
