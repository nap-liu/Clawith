"""Link trigger executions to their canonical conversation.

Revision ID: trigger_execution_conversation
Revises: page_tenant_backfill
Create Date: 2026-08-11
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "trigger_execution_conversation"
down_revision: Union[str, Sequence[str], None] = "page_tenant_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("trigger_executions"):
        return
    columns = {column["name"] for column in inspector.get_columns("trigger_executions")}
    indexes = {index["name"] for index in inspector.get_indexes("trigger_executions")}
    foreign_keys = inspector.get_foreign_keys("trigger_executions")
    if "conversation_id" not in columns:
        op.add_column(
            "trigger_executions",
            sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        )
    if not any(foreign_key.get("constrained_columns") == ["conversation_id"] for foreign_key in foreign_keys):
        op.create_foreign_key(
            "fk_trigger_executions_conversation_id_chat_sessions",
            "trigger_executions",
            "chat_sessions",
            ["conversation_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "ix_trigger_executions_conversation_id" not in indexes:
        op.create_index(
            "ix_trigger_executions_conversation_id",
            "trigger_executions",
            ["conversation_id"],
        )
    if "ix_trigger_executions_agent_scheduled" not in indexes:
        op.create_index(
            "ix_trigger_executions_agent_scheduled",
            "trigger_executions",
            ["agent_id", sa.text("scheduled_at DESC")],
        )
    if "ix_trigger_executions_conversation_scheduled" not in indexes:
        op.create_index(
            "ix_trigger_executions_conversation_scheduled",
            "trigger_executions",
            ["conversation_id", sa.text("scheduled_at DESC")],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("trigger_executions"):
        return
    columns = {column["name"] for column in inspector.get_columns("trigger_executions")}
    indexes = {index["name"] for index in inspector.get_indexes("trigger_executions")}
    foreign_keys = inspector.get_foreign_keys("trigger_executions")
    if "ix_trigger_executions_conversation_scheduled" in indexes:
        op.drop_index("ix_trigger_executions_conversation_scheduled", table_name="trigger_executions")
    if "ix_trigger_executions_agent_scheduled" in indexes:
        op.drop_index("ix_trigger_executions_agent_scheduled", table_name="trigger_executions")
    if "conversation_id" in columns:
        if "ix_trigger_executions_conversation_id" in indexes:
            op.drop_index("ix_trigger_executions_conversation_id", table_name="trigger_executions")
        for foreign_key in foreign_keys:
            if foreign_key.get("constrained_columns") == ["conversation_id"] and foreign_key.get("name"):
                op.drop_constraint(
                    foreign_key["name"],
                    "trigger_executions",
                    type_="foreignkey",
                )
        op.drop_column("trigger_executions", "conversation_id")
