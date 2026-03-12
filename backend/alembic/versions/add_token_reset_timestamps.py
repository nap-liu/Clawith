"""Add last_daily_reset, last_monthly_reset and tokens_used_total to agents table.

Revision ID: add_token_reset_timestamps
"""

from alembic import op

revision = "add_token_reset_timestamps"
down_revision = "add_quota_fields"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_daily_reset TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_monthly_reset TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS tokens_used_total INTEGER DEFAULT 0 NOT NULL")

def downgrade() -> None:
    pass
