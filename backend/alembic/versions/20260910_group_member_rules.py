"""Replace Agent-wide group rosters with group-owned participant rules."""

import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "group_member_rules"
down_revision = "agent_group_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_groups", sa.Column("rules", postgresql.JSONB(), nullable=False, server_default="[]"))
    op.add_column("agent_groups", sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
    op.create_table(
        "agent_group_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("agent_groups.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject_type", sa.String(20), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("group_id", "subject_type", "subject", name="uq_agent_group_member_subject"),
    )
    op.create_index("ix_agent_group_members_group_id", "agent_group_members", ["group_id"])
    # The superseded, unpublished candidate's roster cannot express member
    # rules. Archive it; retain denial for every already-known restricted group.
    # Newly discovered groups follow the new product default (no member rules).
    connection = op.get_bind()
    rows = connection.execute(sa.text("""
        SELECT a.id, a.tenant_id, a.group_policy_mode AS mode,
               COALESCE(jsonb_agg(g.id) FILTER (WHERE g.listed), '[]') AS listed
        FROM agents a LEFT JOIN agent_groups g ON g.agent_id = a.id
        WHERE a.group_policy_mode <> 'off'
        GROUP BY a.id
    """)).mappings()
    audit = sa.table("audit_logs", sa.column("id", postgresql.UUID), sa.column("agent_id", postgresql.UUID),
                     sa.column("action", sa.String), sa.column("details", postgresql.JSONB))
    for row in rows:
        connection.execute(audit.insert().values(id=uuid.uuid4(), agent_id=row["id"],
            action="group_policy_model_migrated", details={"tenant_id": str(row["tenant_id"]),
                "mode": row["mode"], "listed": row["listed"]}))
    groups = connection.execute(sa.text("""
        SELECT g.id FROM agent_groups g JOIN agents a ON a.id = g.agent_id
        WHERE (a.group_policy_mode = 'denylist' AND g.listed)
           OR (a.group_policy_mode = 'allowlist' AND NOT g.listed)
    """)).scalars().all()
    table = sa.table("agent_groups", sa.column("id", postgresql.UUID), sa.column("rules", postgresql.JSONB))
    for group_id in groups:
        connection.execute(table.update().where(table.c.id == group_id).values(rules=[{
            "id": str(uuid.uuid4()), "name": "", "effect": "deny", "enabled": True,
            "all_members": True, "member_ids": [],
        }]))
    op.drop_column("agent_groups", "listed")
    op.drop_constraint("ck_agents_group_policy_mode", "agents", type_="check")
    op.drop_column("agents", "group_policy_mode")
    op.drop_column("agents", "group_policy_revision")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM agent_groups WHERE rules <> '[]'::jsonb)")):
        raise RuntimeError("Clear member policies before downgrading; old readers cannot enforce them")
    op.add_column("agents", sa.Column("group_policy_mode", sa.String(16), nullable=False, server_default="off"))
    op.add_column("agents", sa.Column("group_policy_revision", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_agents_group_policy_mode", "agents", "group_policy_mode IN ('off','denylist','allowlist')")
    op.add_column("agent_groups", sa.Column("listed", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.drop_table("agent_group_members")
    op.drop_column("agent_groups", "rules")
    op.drop_column("agent_groups", "revision")
