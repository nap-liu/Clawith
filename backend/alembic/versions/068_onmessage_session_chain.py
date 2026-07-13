"""Harden on_message session correlation and channel event idempotency.

Revision ID: onmessage_session_chain
Revises: h5_primary_platform_sessions
Create Date: 2026-07-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "onmessage_session_chain"
down_revision: Union[str, Sequence[str], None] = "h5_primary_platform_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    message_columns = {column["name"] for column in inspector.get_columns("chat_messages")}
    if "external_event_key" not in message_columns:
        op.add_column("chat_messages", sa.Column("external_event_key", sa.String(length=500), nullable=True))
    if "message_meta" not in message_columns:
        op.add_column(
            "chat_messages",
            sa.Column(
                "message_meta",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
        )

    index_names = {index["name"] for index in inspector.get_indexes("chat_messages")}
    if "uq_chat_messages_external_event_key" not in index_names:
        op.create_index(
            "uq_chat_messages_external_event_key",
            "chat_messages",
            ["external_event_key"],
            unique=True,
            postgresql_where=sa.text("external_event_key IS NOT NULL"),
        )

    session_constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("chat_sessions")}
    if "uq_chat_sessions_agent_ext_conv" in session_constraints:
        op.drop_constraint("uq_chat_sessions_agent_ext_conv", "chat_sessions", type_="unique")
    if "uq_chat_sessions_agent_channel_ext_conv" not in session_constraints:
        op.create_unique_constraint(
            "uq_chat_sessions_agent_channel_ext_conv",
            "chat_sessions",
            ["agent_id", "source_channel", "external_conv_id"],
        )

def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    session_constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("chat_sessions")}
    if "uq_chat_sessions_agent_channel_ext_conv" in session_constraints:
        op.drop_constraint("uq_chat_sessions_agent_channel_ext_conv", "chat_sessions", type_="unique")
    if "uq_chat_sessions_agent_ext_conv" not in session_constraints:
        op.create_unique_constraint(
            "uq_chat_sessions_agent_ext_conv",
            "chat_sessions",
            ["agent_id", "external_conv_id"],
        )

    index_names = {index["name"] for index in inspector.get_indexes("chat_messages")}
    if "uq_chat_messages_external_event_key" in index_names:
        op.drop_index("uq_chat_messages_external_event_key", table_name="chat_messages")

    message_columns = {column["name"] for column in inspector.get_columns("chat_messages")}
    if "message_meta" in message_columns:
        op.drop_column("chat_messages", "message_meta")
    if "external_event_key" in message_columns:
        op.drop_column("chat_messages", "external_event_key")
