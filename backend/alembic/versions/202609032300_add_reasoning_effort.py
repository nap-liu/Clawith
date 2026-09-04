"""Add normalized reasoning effort defaults and runtime overrides.

Revision ID: reasoning_effort
Revises: cleanup_dingtalk_root
"""

import sqlalchemy as sa
from alembic import op


revision = "reasoning_effort"
down_revision = "cleanup_dingtalk_root"
branch_labels = None
depends_on = None


_TABLES = (
    "llm_models",
    "agents",
    "tasks",
    "agent_schedules",
    "agent_triggers",
    "subagent_runs",
)

_CHECK_TABLES = {
    "llm_models": "ck_llm_models_reasoning_effort",
    "agents": "ck_agents_reasoning_effort",
    "tasks": "ck_tasks_reasoning_effort",
    "agent_schedules": "ck_agent_schedules_reasoning_effort",
    "agent_triggers": "ck_agent_triggers_reasoning_effort",
    "subagent_runs": "ck_subagent_runs_reasoning_effort",
}


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("reasoning_effort", sa.String(length=16), nullable=True))
    condition = (
        "reasoning_effort IS NULL OR reasoning_effort IN "
        "('none','minimal','low','medium','high','xhigh','max')"
    )
    for table, name in _CHECK_TABLES.items():
        op.create_check_constraint(name, table, condition)


def downgrade() -> None:
    for table, name in reversed(tuple(_CHECK_TABLES.items())):
        op.drop_constraint(name, table, type_="check")
    for table in reversed(_TABLES):
        op.drop_column(table, "reasoning_effort")
