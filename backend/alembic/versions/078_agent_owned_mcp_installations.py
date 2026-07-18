"""agent-owned MCP installations and deterministic assignment uniqueness

Revision ID: agent_owned_mcp_installations
Revises: canonical_tenant_user
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "agent_owned_mcp_installations"
down_revision: Union[str, Sequence[str], None] = "canonical_tenant_user"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column("owner_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "mcp_servers",
        sa.Column("installation_key", sa.String(length=100), nullable=True),
    )
    op.create_foreign_key(
        "fk_mcp_servers_owner_agent_id",
        "mcp_servers",
        "agents",
        ["owner_agent_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_mcp_servers_owner_agent_id",
        "mcp_servers",
        ["owner_agent_id"],
    )
    op.create_check_constraint(
        "ck_mcp_servers_private_identity",
        "mcp_servers",
        "(owner_agent_id IS NULL AND installation_key IS NULL) OR "
        "(owner_agent_id IS NOT NULL AND installation_key IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_mcp_servers_owner_installation",
        "mcp_servers",
        ["owner_agent_id", "installation_key"],
    )

    # Collapse duplicate raw tools deterministically. Re-point assignments to
    # the newest tool first; the assignment cleanup below then resolves any
    # collisions created by that re-pointing.
    op.execute(
        """
        WITH ranked AS (
          SELECT id,
                 first_value(id) OVER (
                   PARTITION BY mcp_server_id, mcp_tool_name
                   ORDER BY created_at DESC NULLS LAST, id DESC
                 ) AS keep_id,
                 row_number() OVER (
                   PARTITION BY mcp_server_id, mcp_tool_name
                   ORDER BY created_at DESC NULLS LAST, id DESC
                 ) AS rn
          FROM tools
          WHERE mcp_server_id IS NOT NULL AND mcp_tool_name IS NOT NULL
        )
        UPDATE agent_tools a
        SET tool_id = ranked.keep_id
        FROM ranked
        WHERE ranked.rn > 1 AND a.tool_id = ranked.id
        """
    )
    op.execute(
        """
        DELETE FROM tools t
        USING (
          SELECT id FROM (
            SELECT id, row_number() OVER (
              PARTITION BY mcp_server_id, mcp_tool_name
              ORDER BY created_at DESC NULLS LAST, id DESC
            ) AS rn
            FROM tools
            WHERE mcp_server_id IS NOT NULL AND mcp_tool_name IS NOT NULL
          ) ranked WHERE rn > 1
        ) duplicates
        WHERE t.id = duplicates.id
        """
    )

    # Keep the newest assignment deterministically before enforcing the
    # concurrency invariant. Duplicate rows point at the same effective pair.
    op.execute(
        """
        DELETE FROM agent_tools a
        USING (
          SELECT id FROM (
            SELECT id, row_number() OVER (
              PARTITION BY agent_id, tool_id
              ORDER BY created_at DESC NULLS LAST, id DESC
            ) AS rn
            FROM agent_tools
          ) ranked WHERE rn > 1
        ) duplicates
        WHERE a.id = duplicates.id
        """
    )
    op.create_unique_constraint(
        "uq_agent_tools_agent_tool",
        "agent_tools",
        ["agent_id", "tool_id"],
    )

    op.create_unique_constraint(
        "uq_tools_mcp_server_tool",
        "tools",
        ["mcp_server_id", "mcp_tool_name"],
    )

    tool_server_fk = next((
        fk.get("name")
        for fk in sa.inspect(op.get_bind()).get_foreign_keys("tools")
        if fk.get("constrained_columns") == ["mcp_server_id"]
    ), None)
    if tool_server_fk:
        op.drop_constraint(tool_server_fk, "tools", type_="foreignkey")
    op.create_foreign_key(
        "tools_mcp_server_id_fkey",
        "tools",
        "mcp_servers",
        ["mcp_server_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("tools_mcp_server_id_fkey", "tools", type_="foreignkey")
    op.create_foreign_key(
        "tools_mcp_server_id_fkey",
        "tools",
        "mcp_servers",
        ["mcp_server_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint("uq_tools_mcp_server_tool", "tools", type_="unique")
    op.drop_constraint("uq_agent_tools_agent_tool", "agent_tools", type_="unique")
    op.drop_constraint("uq_mcp_servers_owner_installation", "mcp_servers", type_="unique")
    op.drop_constraint("ck_mcp_servers_private_identity", "mcp_servers", type_="check")
    op.drop_index("ix_mcp_servers_owner_agent_id", table_name="mcp_servers")
    op.drop_constraint("fk_mcp_servers_owner_agent_id", "mcp_servers", type_="foreignkey")
    op.drop_column("mcp_servers", "installation_key")
    op.drop_column("mcp_servers", "owner_agent_id")
