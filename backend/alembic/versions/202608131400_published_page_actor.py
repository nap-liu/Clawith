"""Track the user and time of the most recent page publication.

Revision ID: published_page_actor
Revises: execution_identity_bigint
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "published_page_actor"
down_revision: str | Sequence[str] | None = "execution_identity_bigint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("published_pages")}
    if "last_published_by_user_id" not in columns:
        op.add_column(
            "published_pages",
            sa.Column("last_published_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if "last_published_at" not in columns:
        op.add_column(
            "published_pages",
            sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
        )

    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys("published_pages")
    if not any(fk.get("constrained_columns") == ["last_published_by_user_id"] for fk in foreign_keys):
        op.create_foreign_key(
            "fk_published_pages_last_published_by_user_id_users",
            "published_pages",
            "users",
            ["last_published_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # Historical rows do not contain enough evidence to identify the most
    # recent publisher or publication time. Leave both fields NULL instead of
    # presenting the creator or generic updated_at timestamp as known facts.


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("published_pages")}
    if "last_published_by_user_id" not in columns and "last_published_at" not in columns:
        return

    foreign_keys = inspector.get_foreign_keys("published_pages")
    for foreign_key in foreign_keys:
        if foreign_key.get("constrained_columns") == ["last_published_by_user_id"] and foreign_key.get("name"):
            op.drop_constraint(foreign_key["name"], "published_pages", type_="foreignkey")
            break
    if "last_published_at" in columns:
        op.drop_column("published_pages", "last_published_at")
    if "last_published_by_user_id" in columns:
        op.drop_column("published_pages", "last_published_by_user_id")
