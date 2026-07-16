"""Prevent duplicate Agent access grants under concurrent writes.

Revision ID: unique_agent_permissions
Revises: canonical_supervision_targets
Create Date: 2026-07-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "unique_agent_permissions"
down_revision: Union[str, Sequence[str], None] = "canonical_supervision_targets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("agent_permissions"):
        return

    # Keep exactly one row per logical grant before adding the database guard.
    # When historical duplicates disagree, retain a manage grant over a use grant.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                row_number() OVER (
                    PARTITION BY agent_id, scope_type, scope_id
                    ORDER BY (access_level = 'manage') DESC, id ASC
                ) AS rn
            FROM agent_permissions
        )
        DELETE FROM agent_permissions AS permission
        USING ranked
        WHERE permission.id = ranked.id
          AND ranked.rn > 1
        """
    )

    index_names = {index["name"] for index in inspector.get_indexes("agent_permissions")}
    if "uq_agent_permissions_subject" not in index_names:
        op.create_index(
            "uq_agent_permissions_subject",
            "agent_permissions",
            ["agent_id", "scope_type", "scope_id"],
            unique=True,
            postgresql_where=sa.text("scope_id IS NOT NULL"),
        )
    if "uq_agent_permissions_null_scope" not in index_names:
        op.create_index(
            "uq_agent_permissions_null_scope",
            "agent_permissions",
            ["agent_id", "scope_type"],
            unique=True,
            postgresql_where=sa.text("scope_id IS NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("agent_permissions"):
        return

    index_names = {index["name"] for index in inspector.get_indexes("agent_permissions")}
    if "uq_agent_permissions_null_scope" in index_names:
        op.drop_index("uq_agent_permissions_null_scope", table_name="agent_permissions")
    if "uq_agent_permissions_subject" in index_names:
        op.drop_index("uq_agent_permissions_subject", table_name="agent_permissions")
