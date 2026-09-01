"""Add normalized Agent and execution model overrides.

Revision ID: agent_runtime_model_overrides
Revises: context_governance_controls
"""

import sqlalchemy as sa

from alembic import op

revision = "agent_runtime_model_overrides"
down_revision = "context_governance_controls"
branch_labels = None
depends_on = None


_RESOURCE_TABLES = ("tasks", "agent_schedules", "agent_triggers", "subagent_runs")


def _add_check_not_valid(table: str, name: str, expression: str) -> None:
    op.execute(
        sa.text(
            f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" '
            f"CHECK ({expression}) NOT VALID"
        )
    )


def _add_model_fk_not_valid(table: str) -> None:
    name = f"fk_{table}_model_id_llm_models"
    op.execute(
        sa.text(
            f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" '
            'FOREIGN KEY ("model_id") REFERENCES "llm_models" ("id") '
            "ON DELETE SET NULL NOT VALID"
        )
    )


def upgrade() -> None:
    # Keep the previous application online while this revision runs. New rows
    # are still checked immediately; existing rows are validated afterward by
    # the bounded online validation command.
    op.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '60s'"))

    op.alter_column(
        "agents",
        "daily_memory_load_days",
        existing_type=sa.Integer(),
        server_default=sa.text("0"),
        existing_nullable=False,
    )
    op.add_column("agents", sa.Column("temperature", sa.Float(), nullable=True))
    _add_check_not_valid(
        "agents",
        "ck_agents_temperature",
        "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
    )

    for table in _RESOURCE_TABLES:
        op.add_column(table, sa.Column("model_id", sa.UUID(), nullable=True))
        op.add_column(table, sa.Column("temperature", sa.Float(), nullable=True))
        if table != "subagent_runs":
            op.add_column(table, sa.Column("soul", sa.Boolean(), nullable=False, server_default=sa.true()))
            op.add_column(table, sa.Column("memory", sa.Boolean(), nullable=False, server_default=sa.true()))
        _add_model_fk_not_valid(table)
        _add_check_not_valid(
            table,
            f"ck_{table}_temperature",
            "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)",
        )


def downgrade() -> None:
    for table in reversed(_RESOURCE_TABLES):
        op.drop_constraint(f"ck_{table}_temperature", table, type_="check")
        op.drop_constraint(f"fk_{table}_model_id_llm_models", table, type_="foreignkey")
        op.drop_column(table, "temperature")
        op.drop_column(table, "model_id")
        if table != "subagent_runs":
            op.drop_column(table, "memory")
            op.drop_column(table, "soul")

    op.drop_constraint("ck_agents_temperature", "agents", type_="check")
    op.drop_column("agents", "temperature")
    op.alter_column(
        "agents",
        "daily_memory_load_days",
        existing_type=sa.Integer(),
        server_default=sa.text("2"),
        existing_nullable=False,
    )
