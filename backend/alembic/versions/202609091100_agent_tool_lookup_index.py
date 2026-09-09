"""Index Agent tool assignments without changing assignment semantics."""

from alembic import op
from sqlalchemy import text

revision = "agent_tool_lookup_index"
down_revision = "repair_bootstrap_indexes"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_agent_tools_agent_tool"


def upgrade() -> None:
    connection = op.get_bind()
    with op.get_context().autocommit_block():
        previous_lock = connection.scalar(text("SHOW lock_timeout"))
        previous_statement = connection.scalar(text("SHOW statement_timeout"))
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '120s'")
        try:
            existing = connection.execute(
                text(
                    "SELECT i.indisvalid, i.indisready, i.indisunique, "
                    "i.indnkeyatts = 2 AND i.indnatts = 2 AS two_keys, "
                    "i.indrelid = 'agent_tools'::regclass AS correct_table, "
                    "i.indpred IS NULL AND i.indexprs IS NULL AS plain_index, "
                    "am.amname, "
                    "ARRAY(SELECT a.attname::text FROM unnest(i.indkey) "
                    "WITH ORDINALITY k(attnum, pos) JOIN pg_attribute a "
                    "ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
                    "ORDER BY k.pos) AS columns "
                    "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                    "JOIN pg_am am ON am.oid = c.relam "
                    "WHERE i.indexrelid = to_regclass(:name)"
                ),
                {"name": INDEX_NAME},
            ).mappings().one_or_none()
            if existing is not None:
                if not (
                    existing["correct_table"]
                    and existing["plain_index"]
                    and existing["two_keys"]
                    and not existing["indisunique"]
                    and existing["amname"] == "btree"
                    and existing["columns"] == ["agent_id", "tool_id"]
                ):
                    raise RuntimeError("Agent tool lookup index has an unexpected definition")
                if existing["indisvalid"] and existing["indisready"]:
                    return
                op.execute(f'DROP INDEX CONCURRENTLY "{INDEX_NAME}"')
            op.execute(
                f'CREATE INDEX CONCURRENTLY "{INDEX_NAME}" '
                'ON agent_tools (agent_id, tool_id)'
            )
        finally:
            connection.execute(
                text("SELECT set_config('lock_timeout', :value, false)"),
                {"value": previous_lock},
            )
            connection.execute(
                text("SELECT set_config('statement_timeout', :value, false)"),
                {"value": previous_statement},
            )


def downgrade() -> None:
    connection = op.get_bind()
    with op.get_context().autocommit_block():
        previous_lock = connection.scalar(text("SHOW lock_timeout"))
        previous_statement = connection.scalar(text("SHOW statement_timeout"))
        op.execute("SET lock_timeout = '5s'")
        op.execute("SET statement_timeout = '120s'")
        try:
            op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{INDEX_NAME}"')
        finally:
            connection.execute(
                text("SELECT set_config('lock_timeout', :value, false)"),
                {"value": previous_lock},
            )
            connection.execute(
                text("SELECT set_config('statement_timeout', :value, false)"),
                {"value": previous_statement},
            )
