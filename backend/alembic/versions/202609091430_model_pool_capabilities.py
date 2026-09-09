"""Add model protocol and normalized capability metadata."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "model_pool_capabilities"
down_revision = "company_mcp_group_backfill"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("llm_models", sa.Column("api_protocol", sa.String(32), nullable=True))
    op.add_column("llm_models", sa.Column(
        "purposes", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'[\"conversation\"]'::jsonb"),
    ))
    op.add_column("llm_models", sa.Column(
        "input_modalities", postgresql.JSONB(), nullable=False,
        server_default=sa.text("'[\"text\"]'::jsonb"),
    ))
    op.execute("UPDATE llm_models SET input_modalities = '[\"text\",\"image\"]'::jsonb WHERE supports_vision IS TRUE")


def downgrade():
    op.drop_column("llm_models", "input_modalities")
    op.drop_column("llm_models", "purposes")
    op.drop_column("llm_models", "api_protocol")
