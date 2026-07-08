"""Add DingTalk channel provisioning sessions.

Revision ID: dingtalk_provisioning_sessions
Revises: merge_agent_mgmt_heads
Create Date: 2026-07-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "dingtalk_provisioning_sessions"
down_revision: Union[str, Sequence[str], None] = "merge_agent_mgmt_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    if not conn.dialect.has_table(conn, "dingtalk_channel_provisioning_sessions"):
        op.create_table(
            "dingtalk_channel_provisioning_sessions",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id"), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=True),
            sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("channel_type", sa.String(length=40), nullable=False, server_default="dingtalk"),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="waiting_for_authorization"),
            sa.Column("device_code", sa.String(length=255), nullable=False),
            sa.Column("authorization_url", sa.Text(), nullable=False),
            sa.Column("verification_uri", sa.Text(), nullable=True),
            sa.Column("registration_source", sa.String(length=80), nullable=False, server_default="openClaw"),
            sa.Column("registration_base_url", sa.String(length=255), nullable=False, server_default="https://oapi.dingtalk.com"),
            sa.Column("registration_result", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("poll_interval_seconds", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("poll_attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_poll_attempts", sa.Integer(), nullable=False, server_default="180"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dingtalk_provisioning_agent_id "
        "ON dingtalk_channel_provisioning_sessions(agent_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dingtalk_provisioning_tenant_id "
        "ON dingtalk_channel_provisioning_sessions(tenant_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dingtalk_provisioning_requested_by_user_id "
        "ON dingtalk_channel_provisioning_sessions(requested_by_user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dingtalk_provisioning_status "
        "ON dingtalk_channel_provisioning_sessions(status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dingtalk_provisioning_device_code "
        "ON dingtalk_channel_provisioning_sessions(device_code)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dingtalk_provisioning_expires_at "
        "ON dingtalk_channel_provisioning_sessions(expires_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dingtalk_provisioning_next_poll_at "
        "ON dingtalk_channel_provisioning_sessions(next_poll_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dingtalk_provisioning_due "
        "ON dingtalk_channel_provisioning_sessions(status, next_poll_at, expires_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_dingtalk_provisioning_due")
    op.execute("DROP INDEX IF EXISTS ix_dingtalk_provisioning_next_poll_at")
    op.execute("DROP INDEX IF EXISTS ix_dingtalk_provisioning_expires_at")
    op.execute("DROP INDEX IF EXISTS ix_dingtalk_provisioning_device_code")
    op.execute("DROP INDEX IF EXISTS ix_dingtalk_provisioning_status")
    op.execute("DROP INDEX IF EXISTS ix_dingtalk_provisioning_requested_by_user_id")
    op.execute("DROP INDEX IF EXISTS ix_dingtalk_provisioning_tenant_id")
    op.execute("DROP INDEX IF EXISTS ix_dingtalk_provisioning_agent_id")
    op.execute("DROP TABLE IF EXISTS dingtalk_channel_provisioning_sessions")
