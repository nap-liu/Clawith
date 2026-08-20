"""Add durable Subagent lifecycle state.

Revision ID: subagent_runs
Revises: published_page_actor
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "subagent_runs"
down_revision: str | Sequence[str] | None = "published_page_actor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _install_chat_session_guard(*, allow_subagent: bool) -> None:
    subagent_branch = """
            IF NEW.source_channel = 'subagent' THEN
                IF NEW.is_group IS TRUE OR NEW.peer_agent_id IS NOT NULL THEN
                    RAISE EXCEPTION 'invalid subagent chat session identity';
                END IF;
                IF NEW.user_id IS NULL THEN
                    RETURN NEW;
                END IF;
                SELECT tenant_id INTO human_tenant FROM users WHERE id = NEW.user_id;
                IF human_tenant IS NULL OR source_tenant IS DISTINCT FROM human_tenant THEN
                    RAISE EXCEPTION 'cross-tenant subagent human edge';
                END IF;
                RETURN NEW;
            END IF;
    """ if allow_subagent else ""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION enforce_chat_session_agent_tenant()
        RETURNS trigger AS $$
        DECLARE
            source_tenant UUID;
            peer_tenant UUID;
            human_tenant UUID;
        BEGIN
            SELECT tenant_id INTO source_tenant FROM agents WHERE id = NEW.agent_id;
            IF source_tenant IS NULL THEN
                RAISE EXCEPTION 'chat session source agent has no tenant';
            END IF;
            IF NEW.source_channel = 'agent' THEN
                IF NEW.is_group IS TRUE OR NEW.user_id IS NOT NULL
                   OR NEW.peer_agent_id IS NULL THEN
                    RAISE EXCEPTION 'invalid canonical A2A chat session identity';
                END IF;
                SELECT tenant_id INTO peer_tenant FROM agents WHERE id = NEW.peer_agent_id;
                IF peer_tenant IS NULL OR source_tenant IS DISTINCT FROM peer_tenant THEN
                    RAISE EXCEPTION 'cross-tenant chat session agent edge';
                END IF;
                RETURN NEW;
            END IF;
            {subagent_branch}
            IF NEW.is_group IS TRUE OR NEW.source_channel = 'trigger' THEN
                IF NEW.user_id IS NOT NULL OR NEW.peer_agent_id IS NOT NULL THEN
                    RAISE EXCEPTION 'non-human chat session cannot carry a human or peer placeholder';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.peer_agent_id IS NOT NULL OR NEW.user_id IS NULL THEN
                RAISE EXCEPTION 'human P2P chat session requires exactly one user_id';
            END IF;
            SELECT tenant_id INTO human_tenant FROM users WHERE id = NEW.user_id;
            IF human_tenant IS NULL OR source_tenant IS DISTINCT FROM human_tenant THEN
                RAISE EXCEPTION 'cross-tenant chat session human edge';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
    inspector = sa.inspect(op.get_bind())
    if "subagent_runs" not in inspector.get_table_names():
        op.create_table(
            "subagent_runs",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("parent_session_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("execution_user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("origin_tool_call_id", sa.String(length=255), nullable=False),
            sa.Column("mode", sa.String(length=10), nullable=False),
            sa.Column("model", sa.String(length=100), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("lease_owner", sa.String(length=128), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["execution_user_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["id"], ["chat_sessions.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["parent_session_id"], ["chat_sessions.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "parent_session_id",
                "origin_tool_call_id",
                name="uq_subagent_runs_parent_tool_call",
            ),
        )
        op.create_index("ix_subagent_runs_parent_session_id", "subagent_runs", ["parent_session_id"])
        op.create_index("ix_subagent_runs_execution_user_id", "subagent_runs", ["execution_user_id"])
        op.create_index("ix_subagent_runs_status_lease", "subagent_runs", ["status", "lease_expires_at"])
    _install_chat_session_guard(allow_subagent=True)


def downgrade() -> None:
    bind = op.get_bind()
    if "chat_sessions" in sa.inspect(bind).get_table_names():
        child_count = bind.execute(
            sa.text(
                "SELECT count(*) FROM chat_sessions "
                "WHERE source_channel = 'subagent'"
            )
        ).scalar_one()
        if child_count:
            raise RuntimeError(
                "Refusing to downgrade subagent_runs while Subagent sessions "
                "exist; preserve their audit history and roll forward with a "
                "Subagent-compatible application binary instead."
            )
    if "subagent_runs" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("subagent_runs")
    # An older application builds its LLM tool surface from these persisted
    # rows but has no Subagent handlers or child-only filtering. Remove the
    # builtin definitions (AgentTool rows cascade) so a successful downgrade
    # cannot expose tools that the running binary cannot execute. A later
    # re-upgrade recreates them through the normal builtin seeder.
    if "tools" in sa.inspect(bind).get_table_names():
        bind.execute(
            sa.text(
                "DELETE FROM tools WHERE type = 'builtin' AND name IN "
                "('run_subagent', 'send_message_to_subagent', "
                "'stop_subagent', 'send_message_to_parent')"
            )
        )
    _install_chat_session_guard(allow_subagent=False)
