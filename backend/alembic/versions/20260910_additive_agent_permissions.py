"""Normalize effective legacy permissions before enabling additive evaluation."""

from alembic import context, op
from sqlalchemy import text

revision = "additive_agent_permissions"
down_revision = "merge_media_model_runtime"
branch_labels = None
depends_on = None


def upgrade():
    if context.get_x_argument(as_dictionary=True).get("agent_permissions_cutover") != "offline":
        raise RuntimeError(
            "Agent permission normalization requires an approved offline cutover. "
            "Stop all legacy API/worker/connector writers and keep them stopped; "
            "then run alembic -x agent_permissions_cutover=offline upgrade heads. "
            "After success start only the candidate release."
        )
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '60s'")
    op.execute("LOCK TABLE agents, agent_permissions IN EXCLUSIVE MODE")
    # Preserve the previous representation for audit only. Never restore an old
    # roster over newer decisions. The operator must keep legacy writers stopped.
    op.execute("""
        INSERT INTO audit_logs (id, agent_id, action, details)
        SELECT gen_random_uuid(), a.id, 'agent_permissions_normalized',
            json_build_object(
                'access_mode', a.access_mode,
                'company_access_level', a.company_access_level,
                'permissions', COALESCE((
                    SELECT json_agg(row_to_json(p)) FROM agent_permissions p
                    WHERE p.agent_id = a.id
                ), '[]'::json)
            )
        FROM agents a
    """)
    op.execute("""
        DELETE FROM agent_permissions p USING agents a
        WHERE p.agent_id = a.id AND (
            a.access_mode NOT IN ('company', 'custom')
            OR (a.access_mode = 'company' AND p.scope_type <> 'company')
            OR (a.access_mode = 'custom' AND p.scope_type = 'company')
        )
    """)
    op.execute("""
        INSERT INTO agent_permissions (id, agent_id, scope_type, scope_id, access_level)
        SELECT gen_random_uuid(), a.id, 'company', NULL,
            CASE WHEN COALESCE(NULLIF(a.company_access_level, ''), (
                SELECT p.access_level FROM agent_permissions p
                WHERE p.agent_id = a.id AND p.scope_type = 'company'
            ), 'use') = 'manage' THEN 'manage' ELSE 'use' END
        FROM agents a WHERE a.access_mode = 'company'
        ON CONFLICT (agent_id, scope_type) WHERE scope_id IS NULL
        DO UPDATE SET access_level = excluded.access_level
    """)
    op.execute("""
        UPDATE agent_permissions SET access_level = 'use'
        WHERE access_level NOT IN ('use', 'manage') OR access_level IS NULL
    """)


def downgrade():
    # An empty schema has no authorization decisions to undo. Populated databases
    # must never infer restore safety from audit timestamps or roster shape.
    if op.get_bind().scalar(text("""
        SELECT EXISTS (SELECT 1 FROM agents) OR EXISTS (
            SELECT 1 FROM audit_logs WHERE action = 'agent_permissions_normalized'
        )
    """)):
        raise RuntimeError(
            "Agent permission normalization cannot be downgraded safely. "
            "Keep current grants and repair forward; an audited pre-cutover backup "
            "restore requires a separate data-owner decision."
        )
