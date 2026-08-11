"""Track repeat anonymous visitors for public published pages.

Revision ID: page_anon_visitors
Revises: page_access_hardening
Create Date: 2026-08-10
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "page_anon_visitors"
down_revision: Union[str, Sequence[str], None] = "page_access_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "published_page_anonymous_visitors" in set(inspector.get_table_names()):
        return
    op.create_table(
        "published_page_anonymous_visitors",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("visitor_key", sa.String(length=64), nullable=False),
        sa.Column("view_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("first_viewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["page_id"], ["published_pages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("page_id", "visitor_key", name="uq_published_page_anonymous_visitor"),
    )
    op.create_index(
        "ix_published_page_anonymous_visitors_page_id",
        "published_page_anonymous_visitors",
        ["page_id"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "published_page_anonymous_visitors" in set(inspector.get_table_names()):
        op.drop_table("published_page_anonymous_visitors")
