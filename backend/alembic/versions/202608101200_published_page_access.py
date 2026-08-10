"""Add access control and visitor statistics to published pages.

Revision ID: published_page_access
Revises: scene_drafts
Create Date: 2026-08-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "published_page_access"
down_revision: Union[str, Sequence[str], None] = "scene_drafts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("published_pages")}
    if "access_mode" not in columns:
        op.add_column(
            "published_pages",
            sa.Column("access_mode", sa.String(length=20), server_default="public", nullable=False),
        )
    if "updated_at" not in columns:
        op.add_column(
            "published_pages",
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    checks = {constraint["name"] for constraint in inspector.get_check_constraints("published_pages")}
    if "ck_published_pages_access_mode" not in checks:
        op.create_check_constraint(
            "ck_published_pages_access_mode",
            "published_pages",
            "access_mode IN ('public', 'authenticated', 'restricted')",
        )

    tables = set(inspector.get_table_names())
    if "published_page_access" not in tables:
        op.create_table(
            "published_page_access",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_published_page_access_status"),
            sa.ForeignKeyConstraint(["page_id"], ["published_pages.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("page_id", "user_id", name="uq_published_page_access_page_user"),
        )
        op.create_index("ix_published_page_access_page_id", "published_page_access", ["page_id"])
        op.create_index("ix_published_page_access_user_id", "published_page_access", ["user_id"])

    if "published_page_visitors" not in tables:
        op.create_table(
            "published_page_visitors",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("view_count", sa.Integer(), server_default="1", nullable=False),
            sa.Column("first_viewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("last_viewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["page_id"], ["published_pages.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("page_id", "user_id", name="uq_published_page_visitors_page_user"),
        )
        op.create_index("ix_published_page_visitors_page_id", "published_page_visitors", ["page_id"])
        op.create_index("ix_published_page_visitors_user_id", "published_page_visitors", ["user_id"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "published_page_visitors" in tables:
        op.drop_table("published_page_visitors")
    if "published_page_access" in tables:
        op.drop_table("published_page_access")
    columns = {column["name"] for column in inspector.get_columns("published_pages")}
    checks = {constraint["name"] for constraint in inspector.get_check_constraints("published_pages")}
    if "ck_published_pages_access_mode" in checks:
        op.drop_constraint("ck_published_pages_access_mode", "published_pages", type_="check")
    if "updated_at" in columns:
        op.drop_column("published_pages", "updated_at")
    if "access_mode" in columns:
        op.drop_column("published_pages", "access_mode")
