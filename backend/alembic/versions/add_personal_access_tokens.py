"""Add personal_access_tokens table for MCP server PAT authentication.

Revision ID: add_personal_access_tokens
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "add_personal_access_tokens"
down_revision = "202606151000_mcp_stdio_transport"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    if not conn.dialect.has_table(conn, "personal_access_tokens"):
        op.create_table(
            "personal_access_tokens",
            sa.Column(
                "id",
                UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("token_hash", sa.String(128), nullable=False),
            sa.Column("token_prefix", sa.String(16), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        )

        op.create_unique_constraint(
            "uq_personal_access_tokens_token_hash",
            "personal_access_tokens",
            ["token_hash"],
        )
        op.create_index(
            "ix_personal_access_tokens_token_hash",
            "personal_access_tokens",
            ["token_hash"],
        )


def downgrade():
    op.drop_index("ix_personal_access_tokens_token_hash", table_name="personal_access_tokens")
    op.drop_table("personal_access_tokens")
