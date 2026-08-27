"""Add AI-native project management domain.

Revision ID: ai_native_projects
Revises: published_page_actor
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "ai_native_projects"
down_revision: str | Sequence[str] | None = "published_page_actor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    # The repository's baseline migration creates current Base.metadata for a
    # fresh database. In that path these tables already exist; historical
    # databases reaching this revision still need the explicit DDL below.
    if sa.inspect(op.get_bind()).has_table("projects"):
        return
    op.create_table(
        "project_templates",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("category", sa.String(80), server_default="general", nullable=False),
        sa.Column("version", sa.String(40), server_default="1.0.0", nullable=False),
        sa.Column("is_published", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("definition", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_project_templates_tenant_id", "project_templates", ["tenant_id"])
    op.create_index("ix_project_templates_market", "project_templates", ["tenant_id", "is_published", "category"])

    op.create_table(
        "projects",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("template_id", UUID, sa.ForeignKey("project_templates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("goal", sa.Text(), server_default="", nullable=False),
        sa.Column("success_criteria", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("visibility", sa.String(20), server_default="private", nullable=False),
        sa.Column("status", sa.String(20), server_default="initializing", nullable=False),
        sa.Column("settings", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_projects_tenant_id", "projects", ["tenant_id"])
    op.create_index("ix_projects_owner_user_id", "projects", ["owner_user_id"])
    op.create_index("ix_projects_tenant_owner_status", "projects", ["tenant_id", "owner_user_id", "status"])

    op.create_table(
        "project_access_grants",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), server_default="view", nullable=False),
        sa.Column("created_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_access_user"),
    )
    op.create_index("ix_project_access_grants_tenant_id", "project_access_grants", ["tenant_id"])
    op.create_index("ix_project_access_grants_project_id", "project_access_grants", ["project_id"])
    op.create_index("ix_project_access_grants_user_id", "project_access_grants", ["user_id"])
    op.create_index("ix_project_access_tenant_user", "project_access_grants", ["tenant_id", "user_id"])

    op.create_table(
        "project_member_snapshots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", UUID, sa.ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name_snapshot", sa.String(100), nullable=False),
        sa.Column("role_snapshot", sa.String(500), server_default="", nullable=False),
        sa.Column("config_snapshot", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_leader", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("project_id", "agent_id", name="uq_project_member_agent"),
    )
    op.create_index("ix_project_member_snapshots_tenant_id", "project_member_snapshots", ["tenant_id"])
    op.create_index("ix_project_member_snapshots_project_id", "project_member_snapshots", ["project_id"])
    op.create_index("ix_project_member_snapshots_agent_id", "project_member_snapshots", ["agent_id"])
    op.create_index("ix_project_members_tenant_project", "project_member_snapshots", ["tenant_id", "project_id"])

    op.create_table(
        "project_capability_bindings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capability_type", sa.String(20), nullable=False),
        sa.Column("capability_id", UUID, nullable=True),
        sa.Column("capability_name", sa.String(200), nullable=False),
        sa.Column("source", sa.String(20), server_default="shared", nullable=False),
        sa.Column("inherited_from_agent_id", UUID, sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=True),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("scope", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("config", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_project_capability_bindings_tenant_id", "project_capability_bindings", ["tenant_id"])
    op.create_index("ix_project_capability_bindings_project_id", "project_capability_bindings", ["project_id"])
    op.create_index(
        "ix_project_capabilities_tenant_project", "project_capability_bindings", ["tenant_id", "project_id"]
    )

    op.create_table(
        "project_work_items",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", UUID, sa.ForeignKey("project_work_items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("assignee_agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("status", sa.String(30), server_default="backlog", nullable=False),
        sa.Column("priority", sa.String(20), server_default="medium", nullable=False),
        sa.Column("acceptance_criteria", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("dependency_ids", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_project_work_items_tenant_id", "project_work_items", ["tenant_id"])
    op.create_index("ix_project_work_items_project_id", "project_work_items", ["project_id"])
    op.create_index("ix_project_work_items_assignee_agent_id", "project_work_items", ["assignee_agent_id"])
    op.create_index("ix_project_work_items_board", "project_work_items", ["tenant_id", "project_id", "status"])

    op.create_table(
        "project_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("work_item_id", UUID, sa.ForeignKey("project_work_items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("initiated_by_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(30), server_default="queued", nullable=False),
        sa.Column("trigger_type", sa.String(30), server_default="manual", nullable=False),
        sa.Column("input", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("output", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_project_runs_tenant_id", "project_runs", ["tenant_id"])
    op.create_index("ix_project_runs_project_id", "project_runs", ["project_id"])
    op.create_index("ix_project_runs_history", "project_runs", ["tenant_id", "project_id", "created_at"])

    op.create_table(
        "project_run_member_snapshots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", UUID, sa.ForeignKey("project_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "project_member_id", UUID, sa.ForeignKey("project_member_snapshots.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("agent_id", UUID, sa.ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("is_leader", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("member_config_snapshot", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("capability_snapshot", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("run_id", "project_member_id", name="uq_project_run_member_snapshot"),
    )
    op.create_index("ix_project_run_member_snapshots_tenant_id", "project_run_member_snapshots", ["tenant_id"])
    op.create_index("ix_project_run_member_snapshots_run_id", "project_run_member_snapshots", ["run_id"])
    op.create_index("ix_project_run_member_tenant_run", "project_run_member_snapshots", ["tenant_id", "run_id"])

    op.create_table(
        "project_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("work_item_id", UUID, sa.ForeignKey("project_work_items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("run_id", UUID, sa.ForeignKey("project_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("from_agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("to_agent_id", UUID, sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("summary", sa.String(500), server_default="", nullable=False),
        sa.Column("event_metadata", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_project_events_tenant_id", "project_events", ["tenant_id"])
    op.create_index("ix_project_events_project_id", "project_events", ["project_id"])
    op.create_index("ix_project_events_created_at", "project_events", ["created_at"])
    op.create_index("ix_project_events_timeline", "project_events", ["tenant_id", "project_id", "created_at"])


def downgrade() -> None:
    for table in [
        "project_events",
        "project_run_member_snapshots",
        "project_runs",
        "project_work_items",
        "project_capability_bindings",
        "project_member_snapshots",
        "project_access_grants",
        "projects",
        "project_templates",
    ]:
        op.drop_table(table)
