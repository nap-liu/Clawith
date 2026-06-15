"""Merge the v1.10 upstream chain head with the company private migration head.

The v1.10 upstream merge renumbered upstream migrations to numeric prefixes (001-056)
and brought in 056 ``add_user_tenant_onboarding`` as a head off 055 ``add_agent_focus_items``.
Our private migration chain (…→ ``dab5b3231188`` → ``72382a243406`` add_webhook_queue_max)
is a second head off the same ancestor. This is a pure DAG merge (no schema change) so
``alembic upgrade head`` (singular — used by the prod entrypoint) resolves to one head again.

Revision ID: merge_v1_10_company_heads
Revises: 72382a243406, add_user_tenant_onboarding
Create Date: 2026-06-15
"""

from typing import Sequence, Union


revision: str = "merge_v1_10_company_heads"
down_revision: Union[str, Sequence[str], None] = ("72382a243406", "add_user_tenant_onboarding")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
