"""Restore indexes omitted by older create_all-and-stamp installations."""

from alembic import op
from sqlalchemy import text

revision = "repair_bootstrap_indexes"
down_revision = "company_mcp_group_backfill"
branch_labels = None
depends_on = None

# Frozen migration DDL: fresh databases declare these indexes in model metadata.
INDEXES = (
    (
        "uq_identities_email_lower_not_null",
        "UNIQUE",
        "identities (lower(btrim(email))) "
        "WHERE email IS NOT NULL AND btrim(email) <> ''",
    ),
    (
        "uq_identities_phone_normalized_not_null",
        "UNIQUE",
        "identities (regexp_replace(phone, '[[:space:]+-]', '', 'g')) "
        "WHERE phone IS NOT NULL AND btrim(phone) <> ''",
    ),
    (
        "uq_channel_configs_dingtalk_app_configured",
        "UNIQUE",
        "channel_configs (app_id) WHERE channel_type = 'dingtalk' "
        "AND is_configured = true AND app_id IS NOT NULL",
    ),
    (
        "ix_chat_messages_subagent_dispatch_pending",
        "",
        "chat_messages (created_at, id) "
        "WHERE (message_meta->>'subagent_dispatch_state') = 'pending'",
    ),
    (
        "ix_chat_messages_leader_dispatch_pending",
        "",
        "chat_messages (conversation_id, created_at, id) "
        "WHERE (message_meta->>'kind') = 'project_subagent_reply' "
        "AND (message_meta->>'leader_batch_state') IN ('pending', 'claimed')",
    ),
    (
        "ix_chat_messages_turn_inbox_pending_fifo",
        "",
        "chat_messages (conversation_id, created_at, id) "
        "WHERE (message_meta->>'turn_inbox_state') = 'pending'",
    ),
)


def upgrade() -> None:
    connection = op.get_bind()
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            for name, uniqueness, definition in INDEXES:
                valid = connection.scalar(
                    text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
                    {"name": name},
                )
                if valid is False:
                    op.execute(f'DROP INDEX CONCURRENTLY "{name}"')
                # Duplicate legacy data fails here without merging or deleting it.
                # A retry repairs any invalid index left by the failed build.
                op.execute(
                    f'CREATE {uniqueness} INDEX CONCURRENTLY IF NOT EXISTS "{name}" ON {definition}'
                )
        finally:
            op.execute("RESET lock_timeout")


def downgrade() -> None:
    # These indexes already belong to earlier revisions. Downgrading this repair
    # must not remove constraints that the previous schema was meant to enforce.
    pass
