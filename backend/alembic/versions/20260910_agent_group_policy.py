"""Stable Agent group access, additive to existing conversations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "agent_group_policy"
down_revision = "additive_agent_permissions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agents", sa.Column("group_policy_mode", sa.String(16), nullable=False, server_default="off"))
    op.add_column("agents", sa.Column("group_policy_revision", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_agents_group_policy_mode", "agents", "group_policy_mode IN ('off','denylist','allowlist')")
    op.create_table(
        "agent_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("installation_scope", sa.String(255), nullable=False),
        sa.Column("external_group_id", sa.String(512), nullable=False),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("listed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("agent_id", "channel", "installation_scope", "external_group_id", name="uq_agent_groups_identity"),
    )
    op.create_index("ix_agent_groups_agent_id", "agent_groups", ["agent_id"])


def downgrade():
    # Old readers cannot enforce configured restrictions. Require an explicit
    # administrator reset before removing the enforcement schema.
    configured = op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM agents WHERE group_policy_mode <> 'off')"))
    if configured:
        raise RuntimeError("Disable all group policies before downgrading")
    op.drop_table("agent_groups")
    op.drop_constraint("ck_agents_group_policy_mode", "agents", type_="check")
    op.drop_column("agents", "group_policy_revision")
    op.drop_column("agents", "group_policy_mode")
