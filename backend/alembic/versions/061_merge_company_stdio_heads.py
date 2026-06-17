"""Merge the v1.10 final head with company/main's stdio-MCP transport head.

Integrating company/main (40 commits) brought in its stdio-MCP migration
``202606151000_mcp_stdio_transport``, which chains off our private
``72382a243406`` (add_webhook_queue_max). That makes it a second head parallel
to ``merge_v1_10_final_heads`` (060), because the v1.10 reconciliation chain
reaches ``72382a243406`` only as an ancestor — the stdio migration was authored
on the private base, not on the merged head. This empty DAG-merge migration
reconciles the two so ``alembic upgrade head`` (singular — used by the prod
entrypoint) resolves to a single head again. No schema change.

Revision ID: merge_company_stdio_heads
Revises: merge_v1_10_final_heads, 202606151000_mcp_stdio_transport
Create Date: 2026-06-17
"""

from typing import Sequence, Union


revision: str = "merge_company_stdio_heads"
down_revision: Union[str, Sequence[str], None] = (
    "merge_v1_10_final_heads",
    "202606151000_mcp_stdio_transport",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
