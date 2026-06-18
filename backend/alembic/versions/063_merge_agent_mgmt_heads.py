"""Merge the agent-management (PAT scope + soft-delete) head into the single integrated head.

company/main's MCP agent-management feature added two migrations chained off
``add_personal_access_tokens``:

    add_personal_access_tokens -> 4f0612ed0e72 (PAT scope) -> ae580f30ed03 (agent soft-delete)

That makes ``ae580f30ed03`` a parallel head alongside our ``merge_pat_head`` (062),
since both descend from ``add_personal_access_tokens``. This empty DAG-merge
migration joins them so ``alembic upgrade head`` (singular, used by the prod
entrypoint) resolves to one head. No schema change.

Revision ID: merge_agent_mgmt_heads
Revises: merge_pat_head, ae580f30ed03
Create Date: 2026-06-18
"""

from typing import Sequence, Union


revision: str = "merge_agent_mgmt_heads"
down_revision: Union[str, Sequence[str], None] = (
    "merge_pat_head",
    "ae580f30ed03",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
