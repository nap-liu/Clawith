"""Backfill tenant ownership for legacy published pages.

Revision ID: page_tenant_backfill
Revises: page_anon_visitors
Create Date: 2026-08-10
"""

from typing import Sequence, Union

from alembic import op


revision: str = "page_tenant_backfill"
down_revision: Union[str, Sequence[str], None] = "page_anon_visitors"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Legacy agents may also predate tenant_id. Prefer the agent tenant, then
    # the original publisher's tenant, then the agent creator's tenant.
    op.execute(
        """
        WITH candidates AS (
            SELECT
                page.id,
                COALESCE(agent.tenant_id, publisher.tenant_id, creator.tenant_id) AS tenant_id
            FROM published_pages AS page
            JOIN agents AS agent ON agent.id = page.agent_id
            LEFT JOIN users AS publisher ON publisher.id = page.user_id
            LEFT JOIN users AS creator ON creator.id = agent.creator_id
            WHERE page.tenant_id IS NULL
        )
        UPDATE published_pages AS page
        SET tenant_id = candidates.tenant_id
        FROM candidates
        WHERE page.id = candidates.id
          AND candidates.tenant_id IS NOT NULL
        """
    )


def downgrade() -> None:
    # This is a deterministic ownership repair; reverting it would make legacy
    # pages unmanageable again and cannot distinguish pre-existing values.
    pass
