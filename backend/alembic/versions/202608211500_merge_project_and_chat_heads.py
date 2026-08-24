"""Merge project-management and chat lookup migration heads.

Revision ID: project_chat_heads
Revises: project_member_generations, chat_message_conversation_index
Create Date: 2026-08-21 15:00:00
"""

from collections.abc import Sequence


revision: str = "project_chat_heads"
down_revision: str | Sequence[str] | None = (
    "project_member_generations",
    "chat_message_conversation_index",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join both migration branches without changing the schema."""


def downgrade() -> None:
    """Split back to both parent revisions without changing the schema."""
