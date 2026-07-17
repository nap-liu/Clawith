"""Preserve provider nicknames separately from directory names.

Revision ID: org_member_nickname
Revises: unique_agent_permissions
Create Date: 2026-07-17
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "org_member_nickname"
down_revision: Union[str, Sequence[str], None] = "unique_agent_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("org_members"):
        return

    columns = {column["name"] for column in inspector.get_columns("org_members")}
    if "nickname" not in columns:
        op.add_column("org_members", sa.Column("nickname", sa.String(length=100), nullable=True))

    # Before this change, DingTalk's inbound senderNick could overwrite the
    # canonical User display name. Preserve the current visible value so a
    # subsequent directory refresh can safely restore OrgMember.name and
    # User.display_name from the authoritative DingTalk directory.
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            UPDATE org_members AS member
            SET nickname = COALESCE(
                NULLIF(BTRIM((SELECT tenant_user.display_name FROM users AS tenant_user WHERE tenant_user.id = member.user_id)), ''),
                member.name
            )
            FROM identity_providers AS provider
            WHERE member.provider_id = provider.id
              AND LOWER(CAST(provider.provider_type AS TEXT)) = 'dingtalk'
              AND member.nickname IS NULL
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("org_members"):
        return
    columns = {column["name"] for column in inspector.get_columns("org_members")}
    if "nickname" in columns:
        op.drop_column("org_members", "nickname")
