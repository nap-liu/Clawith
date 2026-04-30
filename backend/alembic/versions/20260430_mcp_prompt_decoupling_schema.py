"""Schema for prompt-block decoupling — tools / channels / channel_type defaults.

Revision ID: 20260430_mcp_prompt_schema
Revises: ece41fd11d66
Create Date: 2026-04-30

Removes the hardcoded ``_has_feishu`` / ``_has_atlassian`` / ragflow branches
in ``agent_context.py`` and replaces them with data-driven prompt blocks:

* ``tools.system_prompt_block`` — DBA-fillable per-tool injection text.
  Used for plugin tools (e.g. mcp_ragflow_*) that need to teach the LLM how
  to consume their output.
* ``tools.mcp_server_instructions`` — auto-captured from the MCP server's
  ``initialize`` response (``server.instructions`` field). One value per
  server, but stored on each tool row of that server because the registry
  currently has no server-level table; the collector deduplicates by
  ``mcp_server_url`` before injection.
* ``channel_configs.system_prompt_block`` — per-agent override.
  Falls back to ``channel_type_defaults`` when null.
* ``channel_type_defaults(channel_type, system_prompt_block, …)`` —
  type-level defaults for built-in channels (feishu / dingtalk / atlassian
  / wecom / slack / discord / microsoft_teams / agentbay).

Data migration for the existing hardcoded prompts is in a separate
revision so schema can be reverted without touching content choices.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260430_mcp_prompt_schema"
down_revision = "ece41fd11d66"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tools",
        sa.Column("system_prompt_block", sa.Text(), nullable=True),
    )
    op.add_column(
        "tools",
        sa.Column("mcp_server_instructions", sa.Text(), nullable=True),
    )
    op.add_column(
        "channel_configs",
        sa.Column("system_prompt_block", sa.Text(), nullable=True),
    )

    op.create_table(
        "channel_type_defaults",
        sa.Column("channel_type", sa.String(50), primary_key=True, nullable=False),
        sa.Column("system_prompt_block", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("channel_type_defaults")
    op.drop_column("channel_configs", "system_prompt_block")
    op.drop_column("tools", "mcp_server_instructions")
    op.drop_column("tools", "system_prompt_block")
