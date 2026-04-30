"""Schema for conversation auto-compaction.

Revision ID: 20260430_auto_compact_schema
Revises: 20260430_seed_ext_prompts
Create Date: 2026-04-30

Adds the storage layer for the auto-compaction feature:

* ``chat_compactions`` — one row per compaction event. ``epoch`` numbers
  consecutive compactions of the same session; the ``superseded_by``
  link records progressive accumulation (epoch N+1's input is epoch N's
  summary plus the messages between epoch N's ``compacted_to`` and the
  new round). ``summary_validation_passed`` records whether the gate
  (length / structure / UUID recall ≥ 0.7) accepted the summary —
  failed ones are kept for audit but the system falls back to ``ctx_size``
  truncation in that conversation.
* ``chat_messages.compacted_into`` — pointer back to the compaction
  record that subsumed this row. Loaders that respect compaction skip
  rows where this is non-NULL and inject the compaction's
  ``summary_text`` instead. Original rows stay in the table for audit
  and any future "expand" feature.
* ``llm_models`` gains 4 fields — three behavior knobs
  (``compact_trigger_ratio``, ``keep_recent_turns``,
  ``compact_summary_max_tokens``) plus ``context_window`` which is
  required for the trigger calculation. ``context_window`` is added
  nullable, backfilled with a conservative 32_000-token default for
  every existing row, then locked NOT NULL.

Idempotent: ``compact_trigger_ratio`` etc default at the DDL level so
existing rows pick up the defaults. Downgrade restores the prior
schema cleanly; compactions table is dropped (audit data is lost on
downgrade — that's acceptable since this is a new feature).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "20260430_auto_compact_schema"
down_revision = "20260430_seed_ext_prompts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── llm_models: 4 new columns ────────────────────────────────────
    # context_window is logically required, but adding NOT NULL on a
    # populated table without a default would fail. Add nullable, fill
    # with a conservative default, then enforce NOT NULL.
    op.add_column(
        "llm_models",
        sa.Column("context_window", sa.Integer(), nullable=True),
    )
    op.execute(
        "UPDATE llm_models SET context_window = 32000 WHERE context_window IS NULL"
    )
    op.alter_column("llm_models", "context_window", nullable=False)

    op.add_column(
        "llm_models",
        sa.Column(
            "compact_trigger_ratio",
            sa.Float(),
            nullable=False,
            server_default="0.85",
        ),
    )
    op.add_column(
        "llm_models",
        sa.Column(
            "keep_recent_turns",
            sa.Integer(),
            nullable=False,
            server_default="8",
        ),
    )
    op.add_column(
        "llm_models",
        sa.Column(
            "compact_summary_max_tokens",
            sa.Integer(),
            nullable=False,
            server_default="2000",
        ),
    )

    # ── chat_compactions: history of compaction events ───────────────
    op.create_table(
        "chat_compactions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("session_id", sa.String(200), nullable=False, index=True),
        sa.Column(
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column(
            "compacted_from_message_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "compacted_to_message_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("summary_tokens", sa.Integer(), nullable=False),
        sa.Column("trigger_prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("trigger_ratio", sa.Float(), nullable=False),
        sa.Column(
            "superseded_by",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_compactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "summary_validation_passed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Per-session, the active (non-superseded) marker is unique per epoch.
    op.create_index(
        "ix_chat_compactions_session_epoch",
        "chat_compactions",
        ["session_id", "epoch"],
    )

    # ── chat_messages.compacted_into ────────────────────────────────
    op.add_column(
        "chat_messages",
        sa.Column(
            "compacted_into",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_compactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Loaders filter on compacted_into IS NULL; index helps the common
    # "load active history" query.
    op.create_index(
        "ix_chat_messages_active",
        "chat_messages",
        ["agent_id", "conversation_id", "created_at"],
        postgresql_where=sa.text("compacted_into IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_active", table_name="chat_messages")
    op.drop_column("chat_messages", "compacted_into")

    op.drop_index("ix_chat_compactions_session_epoch", table_name="chat_compactions")
    op.drop_table("chat_compactions")

    op.drop_column("llm_models", "compact_summary_max_tokens")
    op.drop_column("llm_models", "keep_recent_turns")
    op.drop_column("llm_models", "compact_trigger_ratio")
    op.drop_column("llm_models", "context_window")
