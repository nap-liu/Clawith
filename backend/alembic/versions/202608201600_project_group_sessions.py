"""Project-scoped Agent group and durable child sessions.

Revision ID: project_group_sessions
Revises: subagent_runs, ai_native_projects
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "project_group_sessions"
down_revision: str | Sequence[str] | None = ("subagent_runs", "ai_native_projects")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    chat_columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    subagent_columns = {column["name"] for column in inspector.get_columns("subagent_runs")}
    chat_foreign_columns = {
        tuple(item.get("constrained_columns") or [])
        for item in inspector.get_foreign_keys("chat_sessions")
    }
    subagent_foreign_columns = {
        tuple(item.get("constrained_columns") or [])
        for item in inspector.get_foreign_keys("subagent_runs")
    }
    subagent_unique_columns = {
        tuple(item.get("column_names") or [])
        for item in inspector.get_unique_constraints("subagent_runs")
    }
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("chat_sessions") as batch:
            if "project_id" not in chat_columns:
                batch.add_column(sa.Column("project_id", UUID, nullable=True))
            if ("project_id",) not in chat_foreign_columns:
                batch.create_foreign_key(
                    "fk_chat_sessions_project_id_projects",
                    "projects",
                    ["project_id"],
                    ["id"],
                    ondelete="CASCADE",
                )
        with op.batch_alter_table("subagent_runs") as batch:
            if "project_id" not in subagent_columns:
                batch.add_column(sa.Column("project_id", UUID, nullable=True))
            if "project_member_id" not in subagent_columns:
                batch.add_column(sa.Column("project_member_id", UUID, nullable=True))
            if ("project_id",) not in subagent_foreign_columns:
                batch.create_foreign_key(
                    "fk_subagent_runs_project_id_projects",
                    "projects",
                    ["project_id"],
                    ["id"],
                    ondelete="CASCADE",
                )
            if ("project_member_id",) not in subagent_foreign_columns:
                batch.create_foreign_key(
                    "fk_subagent_runs_project_member_id_project_member_snapshots",
                    "project_member_snapshots",
                    ["project_member_id"],
                    ["id"],
                    ondelete="CASCADE",
                )
            if ("parent_session_id", "project_member_id") not in subagent_unique_columns:
                batch.create_unique_constraint(
                    "uq_subagent_runs_project_group_member",
                    ["parent_session_id", "project_member_id"],
                )
    else:
        if "project_id" not in chat_columns:
            op.add_column("chat_sessions", sa.Column("project_id", UUID, nullable=True))
        if ("project_id",) not in chat_foreign_columns:
            op.create_foreign_key(
                "fk_chat_sessions_project_id_projects",
                "chat_sessions",
                "projects",
                ["project_id"],
                ["id"],
                ondelete="CASCADE",
            )
        if "project_id" not in subagent_columns:
            op.add_column("subagent_runs", sa.Column("project_id", UUID, nullable=True))
        if "project_member_id" not in subagent_columns:
            op.add_column("subagent_runs", sa.Column("project_member_id", UUID, nullable=True))
        if ("project_id",) not in subagent_foreign_columns:
            op.create_foreign_key(
                "fk_subagent_runs_project_id_projects",
                "subagent_runs",
                "projects",
                ["project_id"],
                ["id"],
                ondelete="CASCADE",
            )
        if ("project_member_id",) not in subagent_foreign_columns:
            op.create_foreign_key(
                "fk_subagent_runs_project_member_id_project_member_snapshots",
                "subagent_runs",
                "project_member_snapshots",
                ["project_member_id"],
                ["id"],
                ondelete="CASCADE",
            )
        if ("parent_session_id", "project_member_id") not in subagent_unique_columns:
            op.create_unique_constraint(
                "uq_subagent_runs_project_group_member",
                "subagent_runs",
                ["parent_session_id", "project_member_id"],
            )
    existing_indexes = {
        item["name"] for table in ("chat_sessions", "subagent_runs")
        for item in sa.inspect(bind).get_indexes(table)
    }
    if "ix_chat_sessions_project_id" not in existing_indexes:
        op.create_index("ix_chat_sessions_project_id", "chat_sessions", ["project_id"])
    if "ix_subagent_runs_project_id" not in existing_indexes:
        op.create_index("ix_subagent_runs_project_id", "subagent_runs", ["project_id"])
    if "ix_subagent_runs_project_member_id" not in existing_indexes:
        op.create_index("ix_subagent_runs_project_member_id", "subagent_runs", ["project_member_id"])


def downgrade() -> None:
    op.drop_index("ix_subagent_runs_project_member_id", table_name="subagent_runs")
    op.drop_index("ix_subagent_runs_project_id", table_name="subagent_runs")
    op.drop_index("ix_chat_sessions_project_id", table_name="chat_sessions")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("subagent_runs") as batch:
            batch.drop_constraint("uq_subagent_runs_project_group_member", type_="unique")
            batch.drop_constraint(
                "fk_subagent_runs_project_member_id_project_member_snapshots",
                type_="foreignkey",
            )
            batch.drop_constraint("fk_subagent_runs_project_id_projects", type_="foreignkey")
            batch.drop_column("project_member_id")
            batch.drop_column("project_id")
        with op.batch_alter_table("chat_sessions") as batch:
            batch.drop_constraint("fk_chat_sessions_project_id_projects", type_="foreignkey")
            batch.drop_column("project_id")
    else:
        op.drop_constraint("uq_subagent_runs_project_group_member", "subagent_runs", type_="unique")
        op.drop_constraint(
            "fk_subagent_runs_project_member_id_project_member_snapshots",
            "subagent_runs",
            type_="foreignkey",
        )
        op.drop_constraint("fk_subagent_runs_project_id_projects", "subagent_runs", type_="foreignkey")
        op.drop_column("subagent_runs", "project_member_id")
        op.drop_column("subagent_runs", "project_id")
        op.drop_constraint("fk_chat_sessions_project_id_projects", "chat_sessions", type_="foreignkey")
        op.drop_column("chat_sessions", "project_id")
