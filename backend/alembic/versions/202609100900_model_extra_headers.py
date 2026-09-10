"""Add encrypted optional model request headers without rewriting model rows."""

from alembic import op
import sqlalchemy as sa

revision = "model_extra_headers"
down_revision = "merge_media_model_runtime"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("llm_models", sa.Column("extra_headers_encrypted", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("llm_models", "extra_headers_encrypted")
