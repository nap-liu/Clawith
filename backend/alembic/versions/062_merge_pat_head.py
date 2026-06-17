"""Merge the personal-access-token head into the single integrated head.

company/main's feature/mcp-server-channel added ``add_personal_access_tokens``,
which chains off ``202606151000_mcp_stdio_transport`` — the same parent as our
``merge_company_stdio_heads`` (061) reconciliation. That makes the two parallel
heads. This empty DAG-merge migration joins them so ``alembic upgrade head``
(singular, used by the prod entrypoint) resolves to one head. No schema change.

Revision ID: merge_pat_head
Revises: merge_company_stdio_heads, add_personal_access_tokens
Create Date: 2026-06-17
"""

from typing import Sequence, Union


revision: str = "merge_pat_head"
down_revision: Union[str, Sequence[str], None] = (
    "merge_company_stdio_heads",
    "add_personal_access_tokens",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
