"""Allow internal single-use login credentials without an OAuth application."""

from alembic import op
import sqlalchemy as sa

revision = "agent_login_links"
down_revision = "merge_tool_multimodal"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("openapi_credentials", "application_id", nullable=True)
    op.create_check_constraint(
        "ck_openapi_credential_issuer", "openapi_credentials",
        "(kind = 'agent_login' AND application_id IS NULL AND user_id IS NOT NULL) "
        "OR (kind <> 'agent_login' AND application_id IS NOT NULL)",
    )


def downgrade():
    # An operator must explicitly retire outstanding internal credentials first.
    if op.get_bind().scalar(sa.text(
        "SELECT count(*) FROM openapi_credentials WHERE application_id IS NULL"
    )):
        raise RuntimeError("Retire internal login credentials before schema downgrade")
    op.drop_constraint("ck_openapi_credential_issuer", "openapi_credentials", type_="check")
    op.alter_column("openapi_credentials", "application_id", nullable=False)
