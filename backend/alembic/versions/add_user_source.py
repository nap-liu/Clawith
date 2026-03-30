"""Add source column to users table."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "add_user_source"
down_revision = "add_llm_max_output_tokens"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c["name"] for c in inspector.get_columns("users")]
    if "source" not in columns:
        op.add_column("users", sa.Column("source", sa.String(50), nullable=True, server_default="web"))
    indexes = [i["name"] for i in inspector.get_indexes("users")]
    if "ix_users_source" not in indexes:
        op.create_index("ix_users_source", "users", ["source"])


def downgrade():
    op.drop_index("ix_users_source", "users")
    op.drop_column("users", "source")
