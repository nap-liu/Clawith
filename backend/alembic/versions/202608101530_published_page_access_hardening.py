"""Harden SSO ordering for published-page login flows.

Revision ID: page_access_hardening
Revises: published_page_access
Create Date: 2026-08-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "page_access_hardening"
down_revision: Union[str, Sequence[str], None] = "published_page_access"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("identity_providers")}
    if "sso_enabled_at" not in columns:
        op.add_column(
            "identity_providers",
            sa.Column("sso_enabled_at", sa.DateTime(timezone=True), nullable=True),
        )
    op.execute(
        sa.text(
            "UPDATE identity_providers "
            "SET sso_enabled_at = updated_at "
            "WHERE sso_login_enabled = true AND sso_enabled_at IS NULL"
        )
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("identity_providers")}
    if "sso_enabled_at" in columns:
        op.drop_column("identity_providers", "sso_enabled_at")
