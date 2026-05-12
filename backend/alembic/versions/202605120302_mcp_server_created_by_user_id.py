"""mcp_server_created_by_user_id

Revision ID: 20260512_mcp_creator
Revises: 20260509_mcp_data
Create Date: 2026-05-12 03:02:16.074227
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '20260512_mcp_creator'
down_revision: Union[str, None] = '20260509_mcp_data'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_mcp_servers_created_by_user_id",
        "mcp_servers",
        ["created_by_user_id"],
    )
    op.create_foreign_key(
        "fk_mcp_servers_created_by_user_id",
        "mcp_servers",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_mcp_servers_created_by_user_id", "mcp_servers", type_="foreignkey")
    op.drop_index("ix_mcp_servers_created_by_user_id", table_name="mcp_servers")
    op.drop_column("mcp_servers", "created_by_user_id")
