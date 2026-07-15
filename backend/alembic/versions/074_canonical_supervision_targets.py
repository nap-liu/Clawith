"""Freeze supervision task targets as canonical user_id or agent_id.

Revision ID: canonical_supervision_targets
Revises: canonical_okr_owners
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "canonical_supervision_targets"
down_revision = "canonical_okr_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    target_user_fk = next(
        fk
        for fk in inspector.get_foreign_keys("tasks")
        if fk.get("constrained_columns") == ["supervision_target_user_id"]
    )
    op.drop_constraint(target_user_fk["name"], "tasks", type_="foreignkey")
    op.create_foreign_key(
        "fk_tasks_supervision_target_user_id",
        "tasks",
        "users",
        ["supervision_target_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "tasks",
        sa.Column("supervision_target_agent_id", postgresql.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_tasks_supervision_target_agent_id",
        "tasks",
        "agents",
        ["supervision_target_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Legacy names may be converted only when exactly one active same-tenant
    # relationship candidate exists across humans and agents. A name is never
    # used by runtime execution after this one-time migration.
    op.execute(
        """
        WITH candidate AS (
            SELECT task.id AS task_id, 'user'::text AS target_type,
                   target_user.id AS target_id
            FROM tasks AS task
            JOIN agents AS source ON source.id = task.agent_id
            JOIN agent_relationships AS relationship
              ON relationship.agent_id = source.id
            JOIN users AS target_user
              ON target_user.id = relationship.user_id
             AND target_user.tenant_id = source.tenant_id
             AND target_user.is_active IS TRUE
            WHERE task.type = 'supervision'
              AND task.supervision_target_user_id IS NULL
              AND task.supervision_target_name IS NOT NULL
              AND target_user.display_name = task.supervision_target_name
              AND NOT EXISTS (
                  SELECT 1 FROM relationship_suppressions AS suppression
                  WHERE suppression.agent_id = source.id
                    AND suppression.target_type = 'user'
                    AND suppression.target_id = target_user.id
              )
            UNION ALL
            SELECT task.id AS task_id, 'agent'::text AS target_type,
                   target_agent.id AS target_id
            FROM tasks AS task
            JOIN agents AS source ON source.id = task.agent_id
            JOIN agent_agent_relationships AS relationship
              ON relationship.agent_id = source.id
            JOIN agents AS target_agent
              ON target_agent.id = relationship.target_agent_id
             AND target_agent.tenant_id = source.tenant_id
             AND target_agent.is_deleted IS FALSE
             AND target_agent.status NOT IN ('stopped', 'error')
             AND target_agent.is_expired IS FALSE
             AND (target_agent.expires_at IS NULL OR target_agent.expires_at > now())
             AND (
                 (
                     relationship.created_by_user_id IS NOT NULL
                     AND EXISTS (
                         SELECT 1
                         FROM users AS relationship_creator
                         WHERE relationship_creator.id = relationship.created_by_user_id
                           AND relationship_creator.is_active IS TRUE
                           AND relationship_creator.tenant_id = source.tenant_id
                           AND (
                               source.creator_id = relationship_creator.id
                               OR relationship_creator.role = 'platform_admin'
                               OR (
                                   relationship_creator.role = 'org_admin'
                                   AND source.access_mode <> 'private'
                               )
                               OR (
                                   source.access_mode = 'company'
                                   AND source.company_access_level = 'manage'
                               )
                               OR (
                                   source.access_mode = 'custom'
                                   AND EXISTS (
                                       SELECT 1 FROM agent_permissions AS source_permission
                                       WHERE source_permission.agent_id = source.id
                                         AND source_permission.scope_type = 'user'
                                         AND source_permission.scope_id = relationship_creator.id
                                         AND source_permission.access_level = 'manage'
                                   )
                               )
                           )
                           AND (
                               target_agent.creator_id = relationship_creator.id
                               OR relationship_creator.role = 'platform_admin'
                               OR (
                                   relationship_creator.role = 'org_admin'
                                   AND target_agent.access_mode <> 'private'
                               )
                               OR target_agent.access_mode = 'company'
                               OR (
                                   target_agent.access_mode = 'custom'
                                   AND EXISTS (
                                       SELECT 1 FROM agent_permissions AS target_permission
                                       WHERE target_permission.agent_id = target_agent.id
                                         AND target_permission.scope_type = 'user'
                                         AND target_permission.scope_id = relationship_creator.id
                                   )
                               )
                           )
                     )
                 )
                 OR (
                     relationship.created_by_user_id IS NULL
                     AND (
                         target_agent.access_mode = 'company'
                         OR EXISTS (
                             SELECT 1
                             FROM users AS source_creator
                             WHERE source_creator.id = source.creator_id
                               AND source_creator.is_active IS TRUE
                               AND source_creator.tenant_id = source.tenant_id
                               AND (
                                   target_agent.creator_id = source_creator.id
                                   OR source_creator.role = 'platform_admin'
                                   OR (
                                       source_creator.role = 'org_admin'
                                       AND target_agent.access_mode <> 'private'
                                   )
                                   OR target_agent.access_mode = 'company'
                                   OR (
                                       target_agent.access_mode = 'custom'
                                       AND EXISTS (
                                           SELECT 1 FROM agent_permissions AS target_permission
                                           WHERE target_permission.agent_id = target_agent.id
                                             AND target_permission.scope_type = 'user'
                                             AND target_permission.scope_id = source_creator.id
                                       )
                                   )
                               )
                         )
                     )
                 )
             )
            WHERE task.type = 'supervision'
              AND task.supervision_target_user_id IS NULL
              AND task.supervision_target_name IS NOT NULL
              AND target_agent.name = task.supervision_target_name
              AND NOT EXISTS (
                  SELECT 1 FROM relationship_suppressions AS suppression
                  WHERE suppression.agent_id = source.id
                    AND suppression.target_type = 'agent'
                    AND suppression.target_id = target_agent.id
              )
        ), unique_candidate AS (
            SELECT task_id, min(target_type) AS target_type,
                   min(target_id::text)::uuid AS target_id
            FROM candidate
            GROUP BY task_id
            HAVING count(*) = 1
        )
        UPDATE tasks AS task
        SET supervision_target_user_id = unique_candidate.target_id
        FROM unique_candidate
        WHERE task.id = unique_candidate.task_id
          AND unique_candidate.target_type = 'user'
        """
    )
    op.execute(
        """
        WITH candidate AS (
            SELECT task.id AS task_id, target_agent.id AS target_id
            FROM tasks AS task
            JOIN agents AS source ON source.id = task.agent_id
            JOIN agent_agent_relationships AS relationship
              ON relationship.agent_id = source.id
            JOIN agents AS target_agent
              ON target_agent.id = relationship.target_agent_id
             AND target_agent.tenant_id = source.tenant_id
             AND target_agent.is_deleted IS FALSE
             AND target_agent.status NOT IN ('stopped', 'error')
             AND target_agent.is_expired IS FALSE
             AND (target_agent.expires_at IS NULL OR target_agent.expires_at > now())
             AND (
                 (
                     relationship.created_by_user_id IS NOT NULL
                     AND EXISTS (
                         SELECT 1
                         FROM users AS relationship_creator
                         WHERE relationship_creator.id = relationship.created_by_user_id
                           AND relationship_creator.is_active IS TRUE
                           AND relationship_creator.tenant_id = source.tenant_id
                           AND (
                               source.creator_id = relationship_creator.id
                               OR relationship_creator.role = 'platform_admin'
                               OR (
                                   relationship_creator.role = 'org_admin'
                                   AND source.access_mode <> 'private'
                               )
                               OR (
                                   source.access_mode = 'company'
                                   AND source.company_access_level = 'manage'
                               )
                               OR (
                                   source.access_mode = 'custom'
                                   AND EXISTS (
                                       SELECT 1 FROM agent_permissions AS source_permission
                                       WHERE source_permission.agent_id = source.id
                                         AND source_permission.scope_type = 'user'
                                         AND source_permission.scope_id = relationship_creator.id
                                         AND source_permission.access_level = 'manage'
                                   )
                               )
                           )
                           AND (
                               target_agent.creator_id = relationship_creator.id
                               OR relationship_creator.role = 'platform_admin'
                               OR (
                                   relationship_creator.role = 'org_admin'
                                   AND target_agent.access_mode <> 'private'
                               )
                               OR target_agent.access_mode = 'company'
                               OR (
                                   target_agent.access_mode = 'custom'
                                   AND EXISTS (
                                       SELECT 1 FROM agent_permissions AS target_permission
                                       WHERE target_permission.agent_id = target_agent.id
                                         AND target_permission.scope_type = 'user'
                                         AND target_permission.scope_id = relationship_creator.id
                                   )
                               )
                           )
                     )
                 )
                 OR (
                     relationship.created_by_user_id IS NULL
                     AND (
                         target_agent.access_mode = 'company'
                         OR EXISTS (
                             SELECT 1
                             FROM users AS source_creator
                             WHERE source_creator.id = source.creator_id
                               AND source_creator.is_active IS TRUE
                               AND source_creator.tenant_id = source.tenant_id
                               AND (
                                   target_agent.creator_id = source_creator.id
                                   OR source_creator.role = 'platform_admin'
                                   OR (
                                       source_creator.role = 'org_admin'
                                       AND target_agent.access_mode <> 'private'
                                   )
                                   OR target_agent.access_mode = 'company'
                                   OR (
                                       target_agent.access_mode = 'custom'
                                       AND EXISTS (
                                           SELECT 1 FROM agent_permissions AS target_permission
                                           WHERE target_permission.agent_id = target_agent.id
                                             AND target_permission.scope_type = 'user'
                                             AND target_permission.scope_id = source_creator.id
                                       )
                                   )
                               )
                         )
                     )
                 )
             )
            WHERE task.type = 'supervision'
              AND task.supervision_target_user_id IS NULL
              AND task.supervision_target_name IS NOT NULL
              AND target_agent.name = task.supervision_target_name
              AND NOT EXISTS (
                  SELECT 1 FROM relationship_suppressions AS suppression
                  WHERE suppression.agent_id = source.id
                    AND suppression.target_type = 'agent'
                    AND suppression.target_id = target_agent.id
              )
        ), all_candidate AS (
            SELECT task.id AS task_id, candidate.target_id
            FROM tasks AS task
            JOIN candidate ON candidate.task_id = task.id
            WHERE NOT EXISTS (
                SELECT 1
                FROM agent_relationships AS human_relationship
                JOIN users AS target_user ON target_user.id = human_relationship.user_id
                JOIN agents AS source ON source.id = task.agent_id
                WHERE human_relationship.agent_id = task.agent_id
                  AND target_user.tenant_id = source.tenant_id
                  AND target_user.is_active IS TRUE
                  AND target_user.display_name = task.supervision_target_name
                  AND NOT EXISTS (
                      SELECT 1 FROM relationship_suppressions AS suppression
                      WHERE suppression.agent_id = source.id
                        AND suppression.target_type = 'user'
                        AND suppression.target_id = target_user.id
                  )
            )
        ), unique_candidate AS (
            SELECT task_id, min(target_id::text)::uuid AS target_id
            FROM all_candidate
            GROUP BY task_id
            HAVING count(*) = 1
        )
        UPDATE tasks AS task
        SET supervision_target_agent_id = unique_candidate.target_id
        FROM unique_candidate
        WHERE task.id = unique_candidate.task_id
        """
    )

    op.execute(
        """
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'task', task.id, 'unresolved_supervision_target',
               jsonb_build_object(
                   'agent_id', task.agent_id,
                   'display_name', task.supervision_target_name,
                   'reason', 'canonical target was not exactly one active authorized same-tenant relationship'
               )
        FROM tasks AS task
        WHERE task.type = 'supervision'
          AND task.supervision_target_user_id IS NULL
          AND task.supervision_target_agent_id IS NULL
        """
    )

    op.create_index(
        "ix_tasks_supervision_target_user_id",
        "tasks",
        ["supervision_target_user_id"],
    )
    op.create_index(
        "ix_tasks_supervision_target_agent_id",
        "tasks",
        ["supervision_target_agent_id"],
    )
    op.create_check_constraint(
        "ck_task_single_supervision_target",
        "tasks",
        "NOT (supervision_target_user_id IS NOT NULL AND supervision_target_agent_id IS NOT NULL)",
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_task_supervision_target_tenant()
        RETURNS trigger AS $$
        DECLARE
            source_tenant UUID;
            target_tenant UUID;
        BEGIN
            -- Runtime/API writers enforce exactly one target. The database
            -- deliberately permits zero so ON DELETE SET NULL can quarantine
            -- a task for migration instead of blocking Agent deletion.
            IF NEW.supervision_target_user_id IS NULL
               AND NEW.supervision_target_agent_id IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT tenant_id INTO source_tenant FROM agents WHERE id = NEW.agent_id;
            IF NEW.supervision_target_user_id IS NOT NULL THEN
                SELECT tenant_id INTO target_tenant
                FROM users WHERE id = NEW.supervision_target_user_id;
            ELSE
                SELECT tenant_id INTO target_tenant
                FROM agents WHERE id = NEW.supervision_target_agent_id;
            END IF;
            IF source_tenant IS NULL OR target_tenant IS NULL
               OR source_tenant IS DISTINCT FROM target_tenant THEN
                RAISE EXCEPTION 'cross-tenant supervision target';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_task_supervision_target_tenant
        BEFORE INSERT OR UPDATE OF agent_id, type, supervision_target_user_id,
            supervision_target_agent_id ON tasks
        FOR EACH ROW EXECUTE FUNCTION enforce_task_supervision_target_tenant();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_task_supervision_target_tenant ON tasks")
    op.execute("DROP FUNCTION IF EXISTS enforce_task_supervision_target_tenant()")
    op.drop_constraint("ck_task_single_supervision_target", "tasks", type_="check")
    op.drop_index("ix_tasks_supervision_target_agent_id", table_name="tasks")
    op.drop_index("ix_tasks_supervision_target_user_id", table_name="tasks")
    op.drop_constraint(
        "fk_tasks_supervision_target_agent_id", "tasks", type_="foreignkey"
    )
    op.drop_column("tasks", "supervision_target_agent_id")
    inspector = inspect(op.get_bind())
    target_user_fk = next(
        fk
        for fk in inspector.get_foreign_keys("tasks")
        if fk.get("constrained_columns") == ["supervision_target_user_id"]
    )
    op.drop_constraint(target_user_fk["name"], "tasks", type_="foreignkey")
    op.create_foreign_key(
        "tasks_supervision_target_user_id_fkey",
        "tasks",
        "users",
        ["supervision_target_user_id"],
        ["id"],
    )
