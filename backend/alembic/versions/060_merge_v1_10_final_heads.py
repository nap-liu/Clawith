"""Merge the company private merge head with the upstream v1.10 focus-title head.

After CP12 the DAG had two heads again:

  * ``merge_v1_10_company_heads`` (057, ours) — reconciles our private
    ``72382a243406`` (add_webhook_queue_max) chain with ``add_user_tenant_onboarding``.
  * ``add_title_to_agent_focus_items`` (059, upstream) — chains off
    ``merge_heads_20260521`` (058), which reconciles
    ``add_user_tenant_onboarding`` + ``perf_indexes``.

They are parallel because 057 does not include ``perf_indexes`` and the upstream
058 chain does not include our private ``72382a243406``. This is a pure DAG merge
(no schema change) so ``alembic upgrade head`` (singular — used by the prod
entrypoint) resolves to a single head again. The ``add_user_tenant_onboarding``
diamond (reachable via both 057 and 058) is valid; the 056 migration is idempotent
(has_table guard) so it never runs twice.

Revision ID: merge_v1_10_final_heads
Revises: merge_v1_10_company_heads, add_title_to_agent_focus_items
Create Date: 2026-06-15
"""

from typing import Sequence, Union


revision: str = "merge_v1_10_final_heads"
down_revision: Union[str, Sequence[str], None] = ("merge_v1_10_company_heads", "add_title_to_agent_focus_items")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
