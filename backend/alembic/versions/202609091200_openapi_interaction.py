"""Associate optional OpenAPI context with ordinary chat sessions and messages."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "openapi_interaction"
down_revision = "openapi_secret_reveal"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("openapi_credentials", sa.Column("launcher", postgresql.JSONB(), nullable=True))
    op.create_table(
        "openapi_interactions",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("application_id", sa.UUID(), sa.ForeignKey("openapi_applications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("employee_id", sa.UUID(), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("instance_ref", sa.Text()),
        sa.Column("payload", postgresql.JSONB()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("session_id", sa.UUID(), sa.ForeignKey("chat_sessions.id", ondelete="SET NULL")),
        sa.Column("first_message_id", sa.UUID(), sa.ForeignKey("chat_messages.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("application_id", "user_id", "request_id", name="uq_openapi_interaction_request"),
    )
    op.create_index("ix_openapi_interactions_expires_at", "openapi_interactions", ["expires_at"])


def downgrade():
    op.drop_table("openapi_interactions")
    op.drop_column("openapi_credentials", "launcher")
