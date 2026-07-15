"""Add tenant-scoped speech-recognition service configurations.

Revision ID: speech_recognition_configs
Revises: dingtalk_welcome_retry

This table is intentionally independent from llm_models and has no backfill.
Each tenant administrator must configure a speech provider and API key.
"""

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "speech_recognition_configs"
down_revision: Union[str, None] = "dingtalk_welcome_retry"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "speech_recognition_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("api_key_encrypted", sa.String(length=2048), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_speech_recognition_configs_tenant_id", "speech_recognition_configs", ["tenant_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_speech_recognition_configs_tenant_id", table_name="speech_recognition_configs")
    op.drop_table("speech_recognition_configs")
