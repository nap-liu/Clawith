"""Add background execution identities and widen token counters.

Revision ID: execution_identity_bigint
Revises: trigger_execution_conversation
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import context, op


revision: str = "execution_identity_bigint"
down_revision: str | Sequence[str] | None = "trigger_execution_conversation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AGENT_BIGINT_COLUMNS = (
    "max_tokens_per_day",
    "max_tokens_per_month",
    "tokens_used_today",
    "tokens_used_month",
    "tokens_used_total",
    "cache_read_tokens_today",
    "cache_read_tokens_month",
    "cache_read_tokens_total",
    "cache_creation_tokens_today",
    "cache_creation_tokens_month",
    "cache_creation_tokens_total",
)
_DAILY_BIGINT_COLUMNS = (
    "tokens_used",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "estimated_tokens",
)


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _add_user_column(
    table: str,
    column: str,
    *,
    index: bool = False,
    validate_existing: bool = True,
) -> None:
    if column not in _columns(table):
        op.add_column(
            table,
            sa.Column(column, postgresql.UUID(as_uuid=True), nullable=True),
        )
    inspector = sa.inspect(op.get_bind())
    foreign_keys = inspector.get_foreign_keys(table)
    if not any(fk.get("constrained_columns") == [column] for fk in foreign_keys):
        constraint_name = f"fk_{table}_{column}_users"
        if op.get_bind().dialect.name == "postgresql" and not validate_existing:
            # Enforce all new writes immediately without scanning and locking a
            # multi-million-row history table during cutover. Historical rows
            # remain nullable; validation can be scheduled independently.
            op.execute(
                sa.text(
                    f'ALTER TABLE "{table}" ADD CONSTRAINT "{constraint_name}" '
                    f'FOREIGN KEY ("{column}") REFERENCES users (id) NOT VALID'
                )
            )
        else:
            op.create_foreign_key(
                constraint_name,
                table,
                "users",
                [column],
                ["id"],
            )
    if index:
        indexes = {item["name"] for item in inspector.get_indexes(table)}
        name = f"ix_{table}_{column}"
        if name not in indexes:
            op.create_index(name, table, [column])


def _widen_columns(table: str, requested: tuple[str, ...]) -> None:
    existing = _columns(table)
    targets = [column for column in requested if column in existing]
    if not targets:
        return
    clauses = ", ".join(
        f'ALTER COLUMN "{column}" TYPE BIGINT USING "{column}"::BIGINT'
        for column in targets
    )
    op.execute(sa.text(f'ALTER TABLE "{table}" {clauses}'))


def _drop_invalid_index_concurrently(name: str) -> None:
    valid = op.get_bind().execute(
        sa.text(
            """
            SELECT index.indisvalid
              FROM pg_index AS index
              JOIN pg_class AS relation ON relation.oid = index.indexrelid
             WHERE relation.relname = :name
            """
        ),
        {"name": name},
    ).scalar_one_or_none()
    if valid is False:
        op.execute(sa.text(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"'))


def upgrade() -> None:
    # Do not wait behind long-running production transactions. The migration is
    # safe to retry and old application versions can read/write BIGINT columns.
    op.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
    _widen_columns("agents", _AGENT_BIGINT_COLUMNS)
    _widen_columns("daily_token_usage", _DAILY_BIGINT_COLUMNS)

    if _columns("agent_triggers"):
        _add_user_column("agent_triggers", "created_by_user_id")
        _add_user_column("agent_triggers", "execution_user_id", index=True)
    if _columns("trigger_executions"):
        # This table can contain millions of historical rows. Add the nullable
        # column without a blocking regular index; the index is built
        # concurrently below.
        _add_user_column(
            "trigger_executions",
            "execution_user_id",
            validate_existing=False,
        )
    if _columns("tasks"):
        _add_user_column("tasks", "execution_user_id", index=True)
    if _columns("task_logs"):
        _add_user_column("task_logs", "execution_user_id")
    if _columns("agent_schedules"):
        _add_user_column("agent_schedules", "execution_user_id", index=True)

    # Preserve the effective legacy principal for all existing work. Creation
    # attribution is recovered only from a valid, existing origin user.
    if _columns("agent_triggers"):
        op.execute(
            sa.text(
                """
                UPDATE agent_triggers AS trigger
                SET execution_user_id = CASE
                        WHEN trigger.type = 'on_message'
                         AND COALESCE(trigger.config->>'_origin_session_id', '') <> ''
                         AND COALESCE(trigger.config->>'_origin_user_id', '')
                             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                         AND EXISTS (
                             SELECT 1 FROM users
                             WHERE users.id = (trigger.config->>'_origin_user_id')::uuid
                               AND users.tenant_id = agent.tenant_id
                         )
                        THEN (trigger.config->>'_origin_user_id')::uuid
                        ELSE agent.creator_id
                    END,
                    created_by_user_id = CASE
                        WHEN COALESCE(trigger.config->>'_origin_user_id', '')
                             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                         AND EXISTS (
                             SELECT 1 FROM users
                             WHERE users.id = (trigger.config->>'_origin_user_id')::uuid
                               AND users.tenant_id = agent.tenant_id
                         )
                        THEN (trigger.config->>'_origin_user_id')::uuid
                        ELSE agent.creator_id
                    END
                FROM agents AS agent
                WHERE trigger.agent_id = agent.id
                  AND trigger.execution_user_id IS NULL
                """
            )
        )
    if _columns("tasks"):
        op.execute(
            sa.text(
                """
                UPDATE tasks AS task
                SET execution_user_id = CASE
                    WHEN task.type::text = 'supervision' THEN task.created_by
                    ELSE agent.creator_id
                END
                FROM agents AS agent
                WHERE task.agent_id = agent.id
                  AND task.execution_user_id IS NULL
                """
            )
        )
    if _columns("agent_schedules"):
        op.execute(
            sa.text(
                """
                UPDATE agent_schedules AS schedule
                SET execution_user_id = agent.creator_id
                FROM agents AS agent
                WHERE schedule.agent_id = agent.id
                  AND schedule.execution_user_id IS NULL
                """
            )
        )
    if _columns("trigger_executions"):
        op.execute(
            sa.text(
                """
                UPDATE trigger_executions AS execution
                SET execution_user_id = COALESCE(
                    CASE
                        WHEN trigger.type = 'on_message'
                         AND COALESCE(execution.payload->>'_origin_session_id', '') <> ''
                         AND COALESCE(execution.payload->>'_origin_user_id', '')
                             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                         AND EXISTS (
                             SELECT 1 FROM users
                             WHERE users.id = (execution.payload->>'_origin_user_id')::uuid
                               AND users.tenant_id = agent.tenant_id
                         )
                        THEN (execution.payload->>'_origin_user_id')::uuid
                        ELSE NULL
                    END,
                    trigger.execution_user_id,
                    agent.creator_id
                )
                FROM agent_triggers AS trigger, agents AS agent
                WHERE execution.trigger_id = trigger.id
                  AND execution.agent_id = agent.id
                  AND execution.execution_user_id IS NULL
                  AND execution.status IN ('pending', 'processing')
                """
            )
        )

    # Scene filtering is a read path over immutable per-message snapshots. Build
    # the expression index online because chat_messages may be large.
    if op.get_bind().dialect.name == "postgresql" and (
        _columns("trigger_executions") or _columns("chat_messages")
    ):
        with context.get_context().autocommit_block():
            if _columns("trigger_executions"):
                _drop_invalid_index_concurrently(
                    "ix_trigger_executions_execution_user_id"
                )
                op.execute(
                    sa.text(
                        """
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS
                            ix_trigger_executions_execution_user_id
                        ON trigger_executions (execution_user_id)
                        """
                    )
                )
            if _columns("chat_messages"):
                _drop_invalid_index_concurrently(
                    "ix_chat_messages_scene_conversation"
                )
                op.execute(
                    sa.text(
                        """
                        CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_chat_messages_scene_conversation
                        ON chat_messages ((message_meta->>'scene_key'), conversation_id)
                        WHERE message_meta ? 'scene_key'
                        """
                    )
                )


def _drop_user_column(table: str, column: str) -> None:
    if column not in _columns(table):
        return
    inspector = sa.inspect(op.get_bind())
    index_name = f"ix_{table}_{column}"
    if index_name in {item["name"] for item in inspector.get_indexes(table)}:
        op.drop_index(index_name, table_name=table)
    for foreign_key in inspector.get_foreign_keys(table):
        if foreign_key.get("constrained_columns") == [column] and foreign_key.get("name"):
            op.drop_constraint(foreign_key["name"], table, type_="foreignkey")
    op.drop_column(table, column)


def downgrade() -> None:
    # Expand-only: retain counters, identity data, and indexes on application
    # rollback. Older releases ignore these nullable columns and remain
    # compatible; deleting them would lose administrator reassignment history.
    pass
