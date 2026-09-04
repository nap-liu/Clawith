"""Backfill unambiguous company MCP tool groups.

Revision ID: company_mcp_group_backfill
Revises: reasoning_effort
"""

from __future__ import annotations

import logging
import re
import uuid

import sqlalchemy as sa

from alembic import op

revision = "company_mcp_group_backfill"
down_revision = "reasoning_effort"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


def _internal_name(display_name: str, server_id: uuid.UUID) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "_", display_name.lower()).strip("_-") or "mcp"
    return f"{base[:87]}-{server_id.hex[:12]}"[:100]


def backfill_company_mcp_groups(bind) -> tuple[int, int]:
    """Link only self-contained legacy groups; never infer identity from URL."""
    rows = bind.execute(
        sa.text("""
        SELECT tenant_id, mcp_server_url, mcp_server_name,
               MIN(mcp_server_instructions) AS instructions,
               MIN(system_prompt_block) AS prompt_block
          FROM tools
         WHERE type = 'mcp'
           AND source = 'admin'
           AND mcp_server_id IS NULL
           AND mcp_server_url IS NOT NULL
           AND mcp_server_url <> ''
           AND mcp_server_name IS NOT NULL
           AND mcp_server_name <> ''
         GROUP BY tenant_id, mcp_server_url, mcp_server_name
        HAVING BOOL_AND(COALESCE(config::text, '{}') IN ('{}', 'null'))
           AND COUNT(DISTINCT COALESCE(mcp_server_instructions, '')) <= 1
           AND COUNT(DISTINCT COALESCE(system_prompt_block, '')) <= 1
    """)
    ).all()

    linked = 0
    for row in rows:
        tenant_id, url, display_name, instructions, prompt_block = row
        server_id = uuid.uuid4()
        internal_name = _internal_name(display_name, server_id)
        bind.execute(
            sa.text("""
                INSERT INTO mcp_servers
                    (id, tenant_id, name, display_name, base_url_template,
                     headers_template, system_prompt_block, instructions)
                VALUES
                    (CAST(:id AS uuid), CAST(:tenant_id AS uuid), :name, :display_name,
                     :url, CAST('{}' AS jsonb), :prompt_block, :instructions)
            """),
            {
                "id": str(server_id),
                "tenant_id": str(tenant_id) if tenant_id else None,
                "name": internal_name,
                "display_name": display_name,
                "url": url,
                "prompt_block": prompt_block,
                "instructions": instructions,
            },
        )
        result = bind.execute(
            sa.text("""
                UPDATE tools
                   SET mcp_server_id = CAST(:server_id AS uuid),
                       mcp_server_name = :internal_name
                 WHERE type = 'mcp'
                   AND source = 'admin'
                   AND mcp_server_id IS NULL
                   AND mcp_server_url = :url
                   AND mcp_server_name = :display_name
                   AND COALESCE(tenant_id::text, '') = COALESCE(CAST(:tenant_id AS text), '')
                   AND COALESCE(config::text, '{}') IN ('{}', 'null')
            """),
            {
                "server_id": str(server_id),
                "internal_name": internal_name,
                "url": url,
                "display_name": display_name,
                "tenant_id": str(tenant_id) if tenant_id else None,
            },
        )
        linked += result.rowcount or 0

    skipped = bind.execute(
        sa.text("""
        SELECT COUNT(*)
          FROM tools
         WHERE type = 'mcp'
           AND source = 'admin'
           AND mcp_server_id IS NULL
    """)
    ).scalar_one()
    log.info("company MCP group backfill linked %d tools; left %d ambiguous rows", linked, skipped)
    return linked, skipped


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    bind.execute(sa.text("SET LOCAL statement_timeout = '5min'"))
    backfill_company_mcp_groups(bind)


def downgrade() -> None:
    # Deliberately retain repaired ownership links. Removing them after later
    # edits could switch tools back to the less-safe legacy runtime path.
    pass
