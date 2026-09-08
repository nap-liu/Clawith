"""Join OpenAPI schema and the company bootstrap index repair."""

revision = "merge_openapi_bootstrap"
down_revision = ("openapi_applications_v1", "repair_bootstrap_indexes")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
