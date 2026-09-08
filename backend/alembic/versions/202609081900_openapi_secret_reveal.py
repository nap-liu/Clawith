"""Persist application secrets for administrator viewing."""
from alembic import op
import sqlalchemy as sa

revision = "openapi_secret_reveal"
down_revision = "merge_openapi_bootstrap"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("openapi_applications", sa.Column("client_secret", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("openapi_applications", "client_secret")
