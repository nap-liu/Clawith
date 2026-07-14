"""Persist DingTalk provisioning welcome-message retries.

Revision ID: dingtalk_welcome_retry
Revises: baseline_legacy_onmessage
"""

from typing import Union

import sqlalchemy as sa
from alembic import op


revision: str = "dingtalk_welcome_retry"
down_revision: Union[str, None] = "baseline_legacy_onmessage"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    conn = op.get_bind()
    columns = {column["name"] for column in sa.inspect(conn).get_columns("dingtalk_channel_provisioning_sessions")}
    additions = (
        ("welcome_status", sa.Column("welcome_status", sa.String(length=32), nullable=True)),
        (
            "welcome_attempt_count",
            sa.Column("welcome_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        ),
        ("welcome_next_retry_at", sa.Column("welcome_next_retry_at", sa.DateTime(timezone=True), nullable=True)),
        ("welcome_last_error", sa.Column("welcome_last_error", sa.Text(), nullable=True)),
        ("welcome_sent_at", sa.Column("welcome_sent_at", sa.DateTime(timezone=True), nullable=True)),
    )
    for name, column in additions:
        if name not in columns:
            op.add_column("dingtalk_channel_provisioning_sessions", column)

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_dingtalk_welcome_retry_due "
        "ON dingtalk_channel_provisioning_sessions(welcome_status, welcome_next_retry_at)"
    )
    # Recover registrations that completed before this retry state existed.
    # These rows are already configured; only the one-shot welcome send lost a
    # race with DingTalk's automatically granted robot permission.
    op.execute(
        sa.text(
            """
            UPDATE dingtalk_channel_provisioning_sessions
            SET welcome_status = 'pending',
                welcome_attempt_count = GREATEST(welcome_attempt_count, 1),
                welcome_next_retry_at = CURRENT_TIMESTAMP,
                welcome_last_error = last_error
            WHERE status = 'configured'
              AND welcome_status IS NULL
              AND last_error LIKE '钉钉欢迎消息发送失败:%qyapi_robot_sendmsg%'
            """
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    columns = {column["name"] for column in sa.inspect(conn).get_columns("dingtalk_channel_provisioning_sessions")}
    op.execute("DROP INDEX IF EXISTS ix_dingtalk_welcome_retry_due")
    for name in (
        "welcome_sent_at",
        "welcome_last_error",
        "welcome_next_retry_at",
        "welcome_attempt_count",
        "welcome_status",
    ):
        if name in columns:
            op.drop_column("dingtalk_channel_provisioning_sessions", name)
