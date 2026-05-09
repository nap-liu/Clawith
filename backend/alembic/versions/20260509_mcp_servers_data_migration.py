"""Migrate existing MCP tools data into mcp_servers table.

Revision ID: 20260509_mcp_data
Revises: 20260509_mcp_servers
Create Date: 2026-05-09

For each distinct (tenant_id, mcp_server_url) in tools:
1. Derive a server name from URL host (collision-suffixed).
2. Insert one mcp_servers row, copying ``mcp_server_instructions``
   into ``instructions``.
3. For ``system_prompt_block``: collect all non-NULL values across
   that server's tool rows; if all bytes-identical, copy to
   ``mcp_servers.system_prompt_block``; if conflict, take dictionary
   max + WARNING log listing losers.
4. UPDATE tools SET mcp_server_id = ... WHERE mcp_server_url matches.

``tools.system_prompt_block`` is intentionally LEFT INTACT — old
collector path still reads it in P0a; will be NULL'd out in a later
P0c migration after P0b stabilizes.

Idempotent: skips rows whose mcp_server_id is already populated.
Downgrade: clears tools.mcp_server_id and deletes the populated rows.
"""
from __future__ import annotations

import logging
import re
import uuid
from urllib.parse import urlparse

from alembic import op
import sqlalchemy as sa


revision = "20260509_mcp_data"
down_revision = "20260509_mcp_servers"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")


def _derive_name(url: str, taken: set[str]) -> str:
    """smithery.ai/@user/foo → 'foo'; collisions get -2, -3 suffixes."""
    parsed = urlparse(url)
    last_seg = parsed.path.rstrip("/").split("/")[-1] or parsed.netloc.split(".")[0]
    base = re.sub(r"[^a-z0-9_-]", "_", last_seg.lower()) or "mcp_server"
    if base not in taken:
        taken.add(base)
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    name = f"{base}-{i}"
    taken.add(name)
    return name


def upgrade() -> None:
    bind = op.get_bind()

    distinct_rows = bind.execute(sa.text("""
        SELECT tenant_id, mcp_server_url,
               MIN(mcp_server_instructions) AS instructions
          FROM tools
         WHERE type = 'mcp'
           AND mcp_server_url IS NOT NULL
           AND mcp_server_url <> ''
           AND mcp_server_id IS NULL
         GROUP BY tenant_id, mcp_server_url
    """)).all()

    taken_names: set[str] = set()
    inserted = 0

    for row in distinct_rows:
        tenant_id, url, instructions = row.tenant_id, row.mcp_server_url, row.instructions

        # Resolve prompt block conflict
        prompt_rows = bind.execute(
            sa.text("""
                SELECT DISTINCT system_prompt_block
                  FROM tools
                 WHERE type = 'mcp'
                   AND mcp_server_url = :url
                   AND COALESCE(tenant_id::text, '00000000-0000-0000-0000-000000000000') =
                       COALESCE(CAST(:tenant_id AS text), '00000000-0000-0000-0000-000000000000')
                   AND system_prompt_block IS NOT NULL
                   AND system_prompt_block <> ''
            """),
            {"url": url, "tenant_id": str(tenant_id) if tenant_id is not None else None},
        ).all()
        non_null_blocks = [r.system_prompt_block for r in prompt_rows]
        if not non_null_blocks:
            chosen_block = None
        elif len(set(non_null_blocks)) == 1:
            chosen_block = non_null_blocks[0]
        else:
            chosen_block = max(non_null_blocks)
            losers = [b for b in non_null_blocks if b != chosen_block]
            log.warning(
                "mcp_data_migration: prompt conflict for url=%s tenant=%s; "
                "kept %d chars, dropped %d alternatives: %r",
                url, tenant_id, len(chosen_block), len(losers), [l[:80] for l in losers],
            )

        new_id = uuid.uuid4()
        name = _derive_name(url, taken_names)
        bind.execute(
            sa.text("""
                INSERT INTO mcp_servers
                  (id, tenant_id, name, display_name, base_url_template,
                   headers_template, system_prompt_block, instructions)
                VALUES
                  (CAST(:id AS uuid), CAST(:tenant_id AS uuid), :name, :display_name, :url,
                   CAST(:headers AS jsonb), :prompt, :instr)
            """),
            {
                "id": str(new_id),
                "tenant_id": str(tenant_id) if tenant_id is not None else None,
                "name": name,
                "display_name": name,
                "url": url,
                "headers": "{}",
                "prompt": chosen_block,
                "instr": instructions,
            },
        )
        bind.execute(
            sa.text("""
                UPDATE tools SET mcp_server_id = CAST(:sid AS uuid)
                 WHERE type = 'mcp'
                   AND mcp_server_url = :url
                   AND COALESCE(tenant_id::text, '00000000-0000-0000-0000-000000000000') =
                       COALESCE(CAST(:tenant_id AS text), '00000000-0000-0000-0000-000000000000')
            """),
            {"sid": str(new_id), "url": url, "tenant_id": str(tenant_id) if tenant_id is not None else None},
        )
        inserted += 1

    log.info("mcp_data_migration: inserted %d mcp_servers rows", inserted)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE tools SET mcp_server_id = NULL WHERE mcp_server_id IS NOT NULL"))
    bind.execute(sa.text("DELETE FROM mcp_servers"))
