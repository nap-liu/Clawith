"""Reusable operations for the identity-relationships v1 migration."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

def _constraint_names(inspector: sa.Inspector, table: str, kind: str) -> set[str]:
    if kind == "unique":
        return {item["name"] for item in inspector.get_unique_constraints(table) if item.get("name")}
    if kind == "foreignkey":
        return {item["name"] for item in inspector.get_foreign_keys(table) if item.get("name")}
    raise ValueError(f"unsupported constraint kind: {kind}")

def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    op.execute("DROP TRIGGER IF EXISTS trg_agent_relationship_tenant ON agent_relationships")
    op.execute("DROP TRIGGER IF EXISTS trg_agent_agent_relationship_tenant ON agent_agent_relationships")
    op.execute("DROP TRIGGER IF EXISTS trg_related_user_tenant_move ON users")
    op.execute("DROP TRIGGER IF EXISTS trg_related_agent_tenant_move ON agents")
    op.execute("DROP FUNCTION IF EXISTS enforce_agent_user_relationship_tenant()")
    op.execute("DROP FUNCTION IF EXISTS enforce_agent_agent_relationship_tenant()")
    op.execute("DROP FUNCTION IF EXISTS prevent_related_user_tenant_move()")
    op.execute("DROP FUNCTION IF EXISTS prevent_related_agent_tenant_move()")

    if "uq_agent_agent_relationship_target" in _constraint_names(
        inspector, "agent_agent_relationships", "unique"
    ):
        op.drop_constraint(
            "uq_agent_agent_relationship_target",
            "agent_agent_relationships",
            type_="unique",
        )
    if "uq_agent_relationship_user" in _constraint_names(inspector, "agent_relationships", "unique"):
        op.drop_constraint("uq_agent_relationship_user", "agent_relationships", type_="unique")

    op.execute("DELETE FROM agent_relationships WHERE member_id IS NULL")
    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys("agent_relationships"):
        if fk.get("constrained_columns") == ["member_id"] and fk.get("name"):
            op.drop_constraint(fk["name"], "agent_relationships", type_="foreignkey")
    op.alter_column(
        "agent_relationships",
        "member_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_foreign_key(
        "agent_relationships_member_id_fkey",
        "agent_relationships",
        "org_members",
        ["member_id"],
        ["id"],
    )

    inspector = sa.inspect(bind)
    for fk in inspector.get_foreign_keys("agent_relationships"):
        if fk.get("constrained_columns") == ["user_id"] and fk.get("name"):
            op.drop_constraint(fk["name"], "agent_relationships", type_="foreignkey")
    if "user_id" in {column["name"] for column in inspector.get_columns("agent_relationships")}:
        op.drop_column("agent_relationships", "user_id")

    if sa.inspect(bind).has_table("channel_user_bindings"):
        op.drop_table("channel_user_bindings")
    if sa.inspect(bind).has_table("relationship_suppressions"):
        op.drop_table("relationship_suppressions")
    if sa.inspect(bind).has_table("identity_relationship_migration_conflicts"):
        op.drop_table("identity_relationship_migration_conflicts")

    # Keep org_members identifiers as TEXT. Shrinking them back to VARCHAR(100)
    # would silently destroy full provider subjects created after this revision.
