"""Add A2A file-delivery activity actions.

Revision ID: agent_file_activity_actions
Revises: project_chat_heads
Create Date: 2026-08-22 09:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "agent_file_activity_actions"
down_revision: str | Sequence[str] | None = "project_chat_heads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Extend the PostgreSQL enum used by ``agent_activity_logs``."""
    if op.get_bind().dialect.name != "postgresql":
        return
    # PostgreSQL does not allow a newly-added enum label to be used until the
    # transaction that added it commits. Publish both labels in Alembic's
    # explicit autocommit block so a newly deployed application process can
    # use them immediately after this revision completes. IF NOT EXISTS keeps
    # recovery from a partially applied deployment idempotent.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS 'agent_file_sent'")
        op.execute("ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS 'agent_file_received'")


def downgrade() -> None:
    """PostgreSQL enum values are intentionally retained on downgrade."""
