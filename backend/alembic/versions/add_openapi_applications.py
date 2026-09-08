"""Add standard system OpenAPI applications, bindings and revocable credentials."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "openapi_applications_v1"
down_revision = "company_mcp_group_backfill"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("openapi_applications",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("trust_user_identity", sa.Boolean(), nullable=False),
        sa.Column("scopes", pg.JSONB(), nullable=False),
        sa.Column("embed_origins", pg.JSONB(), nullable=False),
        sa.Column("redirect_origins", pg.JSONB(), nullable=False),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.create_table("openapi_user_bindings",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("application_id", pg.UUID(as_uuid=True), sa.ForeignKey("openapi_applications.id"), nullable=False),
        sa.Column("subject_hash", sa.String(64), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("application_id", "subject_hash"))
    op.create_table("openapi_credentials",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("application_id", pg.UUID(as_uuid=True), sa.ForeignKey("openapi_applications.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("scopes", pg.JSONB(), nullable=False),
        sa.Column("user_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("embed_origin", sa.String(500)),
        sa.Column("redirect_uri", sa.String(2048)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.create_index("ix_openapi_credentials_application_id", "openapi_credentials", ["application_id"])
    op.create_index("ix_openapi_credentials_expires_at", "openapi_credentials", ["expires_at"])


def downgrade():
    op.drop_table("openapi_credentials")
    op.drop_table("openapi_user_bindings")
    op.drop_table("openapi_applications")
