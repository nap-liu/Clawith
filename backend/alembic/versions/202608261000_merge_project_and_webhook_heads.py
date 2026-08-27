"""Merge project execution user and webhook event sequence heads.

Revision ID: project_webhook_heads
Revises: project_execution_users, webhook_event_sequence
Create Date: 2026-08-26 10:00:00
"""

from collections.abc import Sequence


revision: str = "project_webhook_heads"
down_revision: str | Sequence[str] | None = (
    "project_execution_users",
    "webhook_event_sequence",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
