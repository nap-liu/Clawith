"""Schema for per-server MCP registry — mcp_servers + overrides + tools FK.

Revision ID: 20260509_mcp_servers
Revises: edc03160bf79
Create Date: 2026-05-09

P0a of the mcp-prompt-and-userinfo design:
* New ``mcp_servers`` table — one row per (tenant_id, server URL),
  replacing the prior pattern of duplicating mcp_server_url across
  every tool row of the same server.
* New ``mcp_server_overrides`` table — tenant- and agent-scoped
  overrides (prompt appends, URL/headers/credential replacements).
* ``tools.mcp_server_id`` FK column with ON DELETE SET NULL.

Code does NOT switch to reading these tables in this migration; that
happens in P0b. Existing collector path keeps reading ``tools.*``
fields so this migration is fully revertible.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "20260509_mcp_servers"
down_revision = "edc03160bf79"  # verified head as of 2026-05-09; verify with ./wt-alembic.sh current
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("base_url_template", sa.Text(), nullable=False),
        sa.Column("headers_template", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False, server_default="{}"),
        sa.Column("credential_template", sa.Text(), nullable=True),
        sa.Column("system_prompt_block", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("instructions_captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    # Partial unique indexes for tenant+name uniqueness — works on both PG and SQLite.
    op.create_index(
        "ix_mcp_servers_tenant_name",
        "mcp_servers",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
        sqlite_where=sa.text("tenant_id IS NOT NULL"),
    )
    op.create_index(
        "ix_mcp_servers_platform_name",
        "mcp_servers",
        ["name"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
        sqlite_where=sa.text("tenant_id IS NULL"),
    )

    op.create_table(
        "mcp_server_overrides",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "mcp_server_id",
            UUID(as_uuid=True),
            sa.ForeignKey("mcp_servers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope_type", sa.String(10), nullable=False),
        sa.Column("scope_id", UUID(as_uuid=True), nullable=False),
        sa.Column("system_prompt_block", sa.Text(), nullable=True),
        sa.Column("url_template", sa.Text(), nullable=True),
        sa.Column("headers_template", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=True),
        sa.Column("credential_template", sa.Text(), nullable=True),
        sa.Column("last_modified_by_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("mcp_server_id", "scope_type", "scope_id", name="uq_mcp_overrides_scope"),
        sa.CheckConstraint("scope_type IN ('tenant','agent')", name="ck_mcp_overrides_scope_type"),
    )

    op.add_column(
        "tools",
        sa.Column(
            "mcp_server_id",
            UUID(as_uuid=True),
            sa.ForeignKey("mcp_servers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_tools_mcp_server_id", "tools", ["mcp_server_id"])


def downgrade() -> None:
    op.drop_index("ix_tools_mcp_server_id", table_name="tools")
    op.drop_column("tools", "mcp_server_id")
    op.drop_table("mcp_server_overrides")
    op.drop_index("ix_mcp_servers_platform_name", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_tenant_name", table_name="mcp_servers")
    op.drop_table("mcp_servers")
