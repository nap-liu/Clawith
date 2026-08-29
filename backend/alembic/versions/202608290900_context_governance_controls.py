"""Add normalized context and memory governance controls.

Revision ID: context_governance_controls
Revises: turn_inbox_pending_fifo
"""

import sqlalchemy as sa

from alembic import op

revision = "context_governance_controls"
down_revision = "turn_inbox_pending_fifo"
branch_labels = None
depends_on = None


def _add_check_not_valid(table: str, name: str, expression: str) -> None:
    """Enforce new writes without scanning existing rows under a long DDL lock."""
    op.execute(
        sa.text(
            f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" '
            f"CHECK ({expression}) NOT VALID"
        )
    )


def upgrade() -> None:
    # Production runs this while the previous application version remains
    # online. Never queue behind a long transaction or let an unexpected scan
    # hold a schema lock. The release workflow validates these constraints in
    # separate short-lock transactions after this revision commits.
    op.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    # Eight raw turns consume too much of the usable window. Three complete
    # turns still preserve the current user request and recent tool trajectory.
    op.execute(
        "UPDATE llm_models "
        "SET keep_recent_turns = CASE "
        "WHEN keep_recent_turns = 8 THEN 3 "
        "ELSE GREATEST(3, LEAST(50, keep_recent_turns)) END "
        "WHERE keep_recent_turns = 8 "
        "OR keep_recent_turns < 3 "
        "OR keep_recent_turns > 50"
    )
    op.alter_column("llm_models", "keep_recent_turns", server_default="3")
    _add_check_not_valid(
        "llm_models",
        "ck_llm_models_keep_recent_turns",
        "keep_recent_turns >= 3 AND keep_recent_turns <= 50",
    )
    op.add_column(
        "llm_models",
        sa.Column("context_usage_ratio", sa.Float(), server_default="0.7", nullable=False),
    )
    _add_check_not_valid(
        "llm_models",
        "ck_llm_models_context_usage_ratio",
        "context_usage_ratio >= 0.1 AND context_usage_ratio <= 1.0",
    )
    op.add_column(
        "agents",
        sa.Column("daily_memory_load_days", sa.Integer(), server_default="2", nullable=False),
    )
    _add_check_not_valid(
        "agents",
        "ck_agents_daily_memory_load_days",
        "daily_memory_load_days >= 0 AND daily_memory_load_days <= 30",
    )
    op.add_column(
        "chat_compactions",
        sa.Column("validation_failure_reason", sa.Text(), nullable=True),
    )
    # The exact token count of the final persisted summary is unknown until a
    # provider observes the complete next request.  NULL means unknown; zero
    # must not masquerade as an authoritative tokenizer result.
    op.alter_column("chat_compactions", "summary_tokens", nullable=True)


def downgrade() -> None:
    op.execute("UPDATE chat_compactions SET summary_tokens = 0 WHERE summary_tokens IS NULL")
    op.alter_column("chat_compactions", "summary_tokens", nullable=False)
    op.drop_column("chat_compactions", "validation_failure_reason")
    op.drop_constraint("ck_agents_daily_memory_load_days", "agents", type_="check")
    op.drop_column("agents", "daily_memory_load_days")
    op.drop_constraint("ck_llm_models_context_usage_ratio", "llm_models", type_="check")
    op.drop_column("llm_models", "context_usage_ratio")
    op.drop_constraint("ck_llm_models_keep_recent_turns", "llm_models", type_="check")
    op.alter_column("llm_models", "keep_recent_turns", server_default="8")
