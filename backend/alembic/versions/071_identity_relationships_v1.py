"""Canonical channel identities and relationship targets.

Revision ID: identity_relationships_v1
Revises: speech_recognition_configs
Create Date: 2026-07-15

The migration deliberately records rows it cannot normalize safely.  Ambiguous
channel subjects are not bound, and relationships without a tenant-scoped human
are removed instead of granting access through a guessed identity.
"""

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "identity_relationships_v1"
down_revision: Union[str, None] = "speech_recognition_configs"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def _constraint_names(inspector: sa.Inspector, table: str, kind: str) -> set[str]:
    if kind == "unique":
        return {item["name"] for item in inspector.get_unique_constraints(table) if item.get("name")}
    if kind == "foreignkey":
        return {item["name"] for item in inspector.get_foreign_keys(table) if item.get("name")}
    raise ValueError(f"unsupported constraint kind: {kind}")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Historical directory columns were VARCHAR(100), which could reject or
    # truncate a provider subject before it reached the canonical binding.
    for column_name in ("open_id", "unionid", "external_id"):
        op.alter_column(
            "org_members",
            column_name,
            existing_type=sa.String(length=100),
            type_=sa.Text(),
            existing_nullable=True,
        )

    if not inspector.has_table("identity_relationship_migration_conflicts"):
        op.create_table(
            "identity_relationship_migration_conflicts",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("entity_type", sa.String(length=50), nullable=False),
            sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("conflict_type", sa.String(length=100), nullable=False),
            sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index(
            "ix_identity_relationship_conflicts_type",
            "identity_relationship_migration_conflicts",
            ["entity_type", "conflict_type"],
        )

    if not inspector.has_table("channel_user_bindings"):
        op.create_table(
            "channel_user_bindings",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("installation_scope", sa.String(length=255), nullable=False),
            sa.Column("channel_type", sa.String(length=50), nullable=False),
            sa.Column("id_type", sa.String(length=50), nullable=False),
            sa.Column("subject", sa.Text(), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["provider_id"], ["identity_providers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "tenant_id",
                "installation_scope",
                "id_type",
                "subject",
                name="uq_channel_user_binding_subject",
            ),
        )
        op.create_index("ix_channel_user_bindings_tenant_id", "channel_user_bindings", ["tenant_id"])
        op.create_index("ix_channel_user_bindings_provider_id", "channel_user_bindings", ["provider_id"])
        op.create_index("ix_channel_user_bindings_user_id", "channel_user_bindings", ["user_id"])

    if not inspector.has_table("relationship_suppressions"):
        op.create_table(
            "relationship_suppressions",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("target_type", sa.String(length=20), nullable=False),
            sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "target_type IN ('user', 'agent')",
                name="ck_relationship_suppression_target_type",
            ),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "agent_id", "target_type", "target_id", name="uq_relationship_suppression"
            ),
        )

    relationship_columns = {column["name"] for column in inspector.get_columns("agent_relationships")}
    if "user_id" not in relationship_columns:
        op.add_column(
            "agent_relationships",
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        )

    # Relationships whose directory row has no tenant cannot be assigned a
    # canonical tenant User.  Record and quarantine them before backfill.
    op.execute(
        """
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'agent_relationship', ar.id, 'missing_member_tenant',
               jsonb_build_object(
                   'relationship', to_jsonb(ar),
                   'member', CASE WHEN om.id IS NULL THEN NULL ELSE to_jsonb(om) END
               )
        FROM agent_relationships AS ar
        LEFT JOIN org_members AS om ON om.id = ar.member_id
        WHERE om.id IS NULL OR om.tenant_id IS NULL
        """
    )
    op.execute(
        """
        DELETE FROM agent_relationships AS ar
        WHERE NOT EXISTS (
            SELECT 1 FROM org_members AS om
            WHERE om.id = ar.member_id AND om.tenant_id IS NOT NULL
        )
        """
    )

    # Invalid soft links are rebound to a deterministic external-only User.
    # No Identity row or password is created, so channel presence cannot become
    # an accidental login credential.
    op.execute(
        """
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'org_member', om.id, 'invalid_user_link_rebound',
               jsonb_build_object('old_user_id', om.user_id, 'tenant_id', om.tenant_id)
        FROM org_members AS om
        LEFT JOIN users AS linked ON linked.id = om.user_id
        WHERE om.user_id IS NOT NULL
          AND (linked.id IS NULL OR linked.tenant_id IS DISTINCT FROM om.tenant_id)
          AND (
              EXISTS (SELECT 1 FROM agent_relationships AS ar WHERE ar.member_id = om.id)
              OR (
                  om.status = 'active'
                  AND om.provider_id IS NOT NULL
                  AND COALESCE(om.unionid, om.open_id, om.external_id) IS NOT NULL
              )
          )
        """
    )
    op.execute(
        """
        WITH needs_user AS (
            SELECT DISTINCT om.id, om.tenant_id, om.name, om.avatar_url, om.title
            FROM org_members AS om
            LEFT JOIN users AS linked ON linked.id = om.user_id
            WHERE om.tenant_id IS NOT NULL
              AND (linked.id IS NULL OR linked.tenant_id IS DISTINCT FROM om.tenant_id)
              AND (
                  EXISTS (SELECT 1 FROM agent_relationships AS ar WHERE ar.member_id = om.id)
                  OR (
                      om.status = 'active'
                      AND om.provider_id IS NOT NULL
                      AND COALESCE(om.unionid, om.open_id, om.external_id) IS NOT NULL
                  )
              )
        )
        INSERT INTO users
            (id, identity_id, tenant_id, display_name, avatar_url, title, role,
             source, is_active, registration_source,
             quota_message_limit, quota_message_period, quota_messages_used,
             quota_max_agents, quota_agent_ttl_hours, created_at, updated_at)
        SELECT md5('clawith-external-user:' || id::text)::uuid,
               NULL,
               tenant_id,
               COALESCE(NULLIF(name, ''), 'External channel user'),
               avatar_url,
               title,
               'member',
               'external_channel',
               true,
               'identity_relationship_migration',
               50,
               'permanent',
               0,
               2,
               0,
               CURRENT_TIMESTAMP,
               CURRENT_TIMESTAMP
        FROM needs_user
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE org_members AS om
        SET user_id = md5('clawith-external-user:' || om.id::text)::uuid
        WHERE om.tenant_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM users AS linked
              WHERE linked.id = om.user_id
                AND linked.tenant_id IS NOT DISTINCT FROM om.tenant_id
          )
          AND (
              EXISTS (SELECT 1 FROM agent_relationships AS ar WHERE ar.member_id = om.id)
              OR (
                  om.status = 'active'
                  AND om.provider_id IS NOT NULL
                  AND COALESCE(om.unionid, om.open_id, om.external_id) IS NOT NULL
              )
          )
        """
    )
    op.execute(
        """
        UPDATE agent_relationships AS ar
        SET user_id = om.user_id
        FROM org_members AS om
        JOIN users AS canonical ON canonical.id = om.user_id
        WHERE ar.member_id = om.id
          AND canonical.tenant_id IS NOT DISTINCT FROM om.tenant_id
        """
    )

    op.execute(
        """
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'agent_relationship', id, 'unresolved_user',
               jsonb_build_object('relationship', to_jsonb(agent_relationships))
        FROM agent_relationships
        WHERE user_id IS NULL
        """
    )
    op.execute("DELETE FROM agent_relationships WHERE user_id IS NULL")

    # Keep one stable row per canonical relationship and report every loser.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, agent_id, user_id,
                   first_value(id) OVER (
                       PARTITION BY agent_id, user_id
                       ORDER BY created_at NULLS LAST, id
                   ) AS kept_id,
                   row_number() OVER (
                       PARTITION BY agent_id, user_id
                       ORDER BY created_at NULLS LAST, id
                   ) AS rn
            FROM agent_relationships
        )
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'agent_relationship', ranked.id, 'duplicate_canonical_target',
               jsonb_build_object(
                   'kept_id', ranked.kept_id,
                   'relationship', to_jsonb(original)
               )
        FROM ranked
        JOIN agent_relationships AS original ON original.id = ranked.id
        WHERE ranked.rn > 1
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY agent_id, user_id
                ORDER BY created_at NULLS LAST, id
            ) AS rn
            FROM agent_relationships
        )
        DELETE FROM agent_relationships AS ar
        USING ranked
        WHERE ar.id = ranked.id AND ranked.rn > 1
        """
    )

    # Do the same for agent-to-agent edges before adding their database guard.
    op.execute(
        """
        WITH ranked AS (
            SELECT id, agent_id, target_agent_id,
                   first_value(id) OVER (
                       PARTITION BY agent_id, target_agent_id
                       ORDER BY created_at NULLS LAST, id
                   ) AS kept_id,
                   row_number() OVER (
                       PARTITION BY agent_id, target_agent_id
                       ORDER BY created_at NULLS LAST, id
                   ) AS rn
            FROM agent_agent_relationships
        )
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'agent_agent_relationship', ranked.id, 'duplicate_canonical_target',
               jsonb_build_object(
                   'kept_id', ranked.kept_id,
                   'relationship', to_jsonb(original)
               )
        FROM ranked
        JOIN agent_agent_relationships AS original ON original.id = ranked.id
        WHERE ranked.rn > 1
        """
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (
                PARTITION BY agent_id, target_agent_id
                ORDER BY created_at NULLS LAST, id
            ) AS rn
            FROM agent_agent_relationships
        )
        DELETE FROM agent_agent_relationships AS edge
        USING ranked
        WHERE edge.id = ranked.id AND ranked.rn > 1
        """
    )

    # Quarantine cross-tenant grant edges before installing the database guard.
    op.execute(
        """
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'agent_relationship', relationship.id, 'cross_tenant_target',
               jsonb_build_object('relationship', to_jsonb(relationship))
        FROM agent_relationships AS relationship
        JOIN agents AS source ON source.id = relationship.agent_id
        JOIN users AS target ON target.id = relationship.user_id
        WHERE source.tenant_id IS NULL
           OR target.tenant_id IS NULL
           OR source.tenant_id <> target.tenant_id
        """
    )
    op.execute(
        """
        DELETE FROM agent_relationships AS relationship
        USING agents AS source, users AS target
        WHERE source.id = relationship.agent_id
          AND target.id = relationship.user_id
          AND (
              source.tenant_id IS NULL
              OR target.tenant_id IS NULL
              OR source.tenant_id <> target.tenant_id
          )
        """
    )
    op.execute(
        """
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'agent_agent_relationship', relationship.id, 'cross_tenant_target',
               jsonb_build_object('relationship', to_jsonb(relationship))
        FROM agent_agent_relationships AS relationship
        JOIN agents AS source ON source.id = relationship.agent_id
        JOIN agents AS target ON target.id = relationship.target_agent_id
        WHERE source.tenant_id IS NULL
           OR target.tenant_id IS NULL
           OR source.tenant_id <> target.tenant_id
        """
    )
    op.execute(
        """
        DELETE FROM agent_agent_relationships AS relationship
        USING agents AS source, agents AS target
        WHERE source.id = relationship.agent_id
          AND target.id = relationship.target_agent_id
          AND (
              source.tenant_id IS NULL
              OR target.tenant_id IS NULL
              OR source.tenant_id <> target.tenant_id
          )
        """
    )

    inspector = sa.inspect(bind)
    relationship_fks = inspector.get_foreign_keys("agent_relationships")
    for fk in relationship_fks:
        if fk.get("constrained_columns") == ["member_id"] and fk.get("name"):
            op.drop_constraint(fk["name"], "agent_relationships", type_="foreignkey")
    op.alter_column(
        "agent_relationships",
        "member_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.create_foreign_key(
        "fk_agent_relationships_member_id_org_members",
        "agent_relationships",
        "org_members",
        ["member_id"],
        ["id"],
        ondelete="SET NULL",
    )

    relationship_fks = _constraint_names(sa.inspect(bind), "agent_relationships", "foreignkey")
    if "fk_agent_relationships_user_id_users" not in relationship_fks:
        op.create_foreign_key(
            "fk_agent_relationships_user_id_users",
            "agent_relationships",
            "users",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.alter_column(
        "agent_relationships",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    if "uq_agent_relationship_user" not in _constraint_names(sa.inspect(bind), "agent_relationships", "unique"):
        op.create_unique_constraint(
            "uq_agent_relationship_user",
            "agent_relationships",
            ["agent_id", "user_id"],
        )
    if "uq_agent_agent_relationship_target" not in _constraint_names(
        sa.inspect(bind), "agent_agent_relationships", "unique"
    ):
        op.create_unique_constraint(
            "uq_agent_agent_relationship_target",
            "agent_agent_relationships",
            ["agent_id", "target_agent_id"],
        )

    # Cross-table tenant equality cannot be expressed by a local CHECK. These
    # triggers guard every writer, including migrations and raw SQL.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_agent_user_relationship_tenant()
        RETURNS trigger AS $$
        DECLARE source_tenant uuid;
        DECLARE target_tenant uuid;
        BEGIN
            SELECT tenant_id INTO source_tenant FROM agents WHERE id = NEW.agent_id;
            SELECT tenant_id INTO target_tenant FROM users WHERE id = NEW.user_id;
            IF source_tenant IS NULL OR target_tenant IS NULL OR source_tenant <> target_tenant THEN
                RAISE EXCEPTION 'agent_relationship tenant mismatch';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_agent_relationship_tenant ON agent_relationships")
    op.execute(
        """
        CREATE TRIGGER trg_agent_relationship_tenant
        BEFORE INSERT OR UPDATE OF agent_id, user_id ON agent_relationships
        FOR EACH ROW EXECUTE FUNCTION enforce_agent_user_relationship_tenant()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_agent_agent_relationship_tenant()
        RETURNS trigger AS $$
        DECLARE source_tenant uuid;
        DECLARE target_tenant uuid;
        BEGIN
            SELECT tenant_id INTO source_tenant FROM agents WHERE id = NEW.agent_id;
            SELECT tenant_id INTO target_tenant FROM agents WHERE id = NEW.target_agent_id;
            IF source_tenant IS NULL OR target_tenant IS NULL OR source_tenant <> target_tenant THEN
                RAISE EXCEPTION 'agent_agent_relationship tenant mismatch';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_agent_agent_relationship_tenant ON agent_agent_relationships")
    op.execute(
        """
        CREATE TRIGGER trg_agent_agent_relationship_tenant
        BEFORE INSERT OR UPDATE OF agent_id, target_agent_id ON agent_agent_relationships
        FOR EACH ROW EXECUTE FUNCTION enforce_agent_agent_relationship_tenant()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_related_user_tenant_move()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
               AND EXISTS (SELECT 1 FROM agent_relationships WHERE user_id = NEW.id) THEN
                RAISE EXCEPTION 'cannot change tenant of a related user';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_related_user_tenant_move ON users")
    op.execute(
        """
        CREATE TRIGGER trg_related_user_tenant_move
        BEFORE UPDATE OF tenant_id ON users
        FOR EACH ROW EXECUTE FUNCTION prevent_related_user_tenant_move()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_related_agent_tenant_move()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
               AND (
                   EXISTS (SELECT 1 FROM agent_relationships WHERE agent_id = NEW.id)
                   OR EXISTS (
                       SELECT 1 FROM agent_agent_relationships
                       WHERE agent_id = NEW.id OR target_agent_id = NEW.id
                   )
               ) THEN
                RAISE EXCEPTION 'cannot change tenant of a related agent';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_related_agent_tenant_move ON agents")
    op.execute(
        """
        CREATE TRIGGER trg_related_agent_tenant_move
        BEFORE UPDATE OF tenant_id ON agents
        FOR EACH ROW EXECUTE FUNCTION prevent_related_agent_tenant_move()
        """
    )

    # Generate candidates from complete historical identifiers.  Ambiguous
    # subjects are reported and skipped rather than arbitrarily assigned.
    binding_candidates = """
        WITH channel_installations AS (
            SELECT agent.tenant_id,
                   config.id AS config_id,
                   config.agent_id,
                   CASE config.channel_type::text
                       WHEN 'microsoft_teams' THEN 'teams'
                       ELSE config.channel_type::text
                   END AS channel_type,
                   COALESCE(
                       NULLIF(config.app_id, ''),
                       NULLIF(config.extra_config->>'app_id', ''),
                       NULLIF(config.extra_config->>'workspace_id', ''),
                       NULLIF(config.extra_config->>'team_id', ''),
                       NULLIF(config.extra_config->>'corp_id', ''),
                       NULLIF(config.extra_config->>'robot_code', ''),
                       NULLIF(config.extra_config->>'phone_number_id', '')
                   ) AS issuer,
                   count(*) OVER (
                       PARTITION BY agent.tenant_id,
                       CASE config.channel_type::text
                           WHEN 'microsoft_teams' THEN 'teams'
                           ELSE config.channel_type::text
                       END
                   ) AS installation_count
            FROM channel_configs AS config
            JOIN agents AS agent ON agent.id = config.agent_id
        )
        SELECT om.id AS member_id,
               om.tenant_id,
               om.provider_id,
               CASE
                   WHEN installation.issuer IS NOT NULL THEN
                       'agent:' || installation.agent_id::text || ':channel:' ||
                       installation.channel_type || ':issuer:' ||
                       left(encode(sha256(convert_to(installation.issuer, 'UTF8')), 'hex'), 32)
                   ELSE 'channel-config:' || installation.config_id::text
               END AS installation_scope,
               installation.channel_type,
               ids.id_type,
               ids.subject,
               om.user_id
        FROM org_members AS om
        JOIN identity_providers AS ip ON ip.id = om.provider_id
        JOIN users AS usr ON usr.id = om.user_id AND usr.tenant_id IS NOT DISTINCT FROM om.tenant_id
        JOIN channel_installations AS installation
          ON installation.tenant_id IS NOT DISTINCT FROM om.tenant_id
         AND installation.channel_type = CASE ip.provider_type
             WHEN 'microsoft_teams' THEN 'teams'
             ELSE ip.provider_type
         END
         AND installation.installation_count = 1
        CROSS JOIN LATERAL (
            VALUES
                ('union_id'::text, om.unionid::text),
                ('open_id'::text, om.open_id::text),
                (
                    CASE ip.provider_type
                        WHEN 'dingtalk' THEN 'staff_id'
                        WHEN 'wecom' THEN 'user_id'
                        ELSE 'external_id'
                    END,
                    om.external_id::text
                )
        ) AS ids(id_type, subject)
        WHERE om.tenant_id IS NOT NULL
          AND om.user_id IS NOT NULL
          AND om.status = 'active'
          AND ids.subject IS NOT NULL
          AND ids.subject <> ''
    """
    op.execute(
        """
        WITH installation_counts AS (
            SELECT agent.tenant_id,
                   CASE config.channel_type::text
                       WHEN 'microsoft_teams' THEN 'teams'
                       ELSE config.channel_type::text
                   END AS channel_type,
                   count(*) AS installation_count
            FROM channel_configs AS config
            JOIN agents AS agent ON agent.id = config.agent_id
            GROUP BY agent.tenant_id,
                     CASE config.channel_type::text
                         WHEN 'microsoft_teams' THEN 'teams'
                         ELSE config.channel_type::text
                     END
        )
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'org_member', member.id, 'unresolved_installation_scope',
               jsonb_build_object(
                   'tenant_id', member.tenant_id,
                   'user_id', member.user_id,
                   'provider_id', provider.id,
                   'provider_type', provider.provider_type,
                   'installation_count', COALESCE(counts.installation_count, 0)
               )
        FROM org_members AS member
        JOIN identity_providers AS provider ON provider.id = member.provider_id
        LEFT JOIN installation_counts AS counts
          ON counts.tenant_id IS NOT DISTINCT FROM member.tenant_id
         AND counts.channel_type = CASE provider.provider_type
             WHEN 'microsoft_teams' THEN 'teams'
             ELSE provider.provider_type
         END
        WHERE member.status = 'active'
          AND member.tenant_id IS NOT NULL
          AND member.user_id IS NOT NULL
          AND provider.provider_type IN (
              'feishu', 'dingtalk', 'wecom', 'slack', 'discord',
              'whatsapp', 'microsoft_teams', 'teams', 'telegram'
          )
          AND COALESCE(member.unionid, member.open_id, member.external_id) IS NOT NULL
          AND COALESCE(counts.installation_count, 0) <> 1
        """
    )
    op.execute(
        f"""
        WITH candidates AS ({binding_candidates}),
        ambiguous AS (
            SELECT tenant_id, installation_scope, id_type, subject
            FROM candidates
            GROUP BY tenant_id, installation_scope, id_type, subject
            HAVING count(DISTINCT user_id) > 1
        )
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'org_member', candidate.member_id, 'ambiguous_channel_subject',
               jsonb_build_object(
                   'tenant_id', candidate.tenant_id,
                   'provider_id', candidate.provider_id,
                   'installation_scope', candidate.installation_scope,
                   'id_type', candidate.id_type,
                   'subject', candidate.subject,
                   'user_id', candidate.user_id
               )
        FROM candidates AS candidate
        JOIN ambiguous USING (tenant_id, installation_scope, id_type, subject)
        """
    )
    op.execute(
        """
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'org_member', om.id, 'missing_binding_tenant',
               jsonb_build_object('provider_id', om.provider_id)
        FROM org_members AS om
        WHERE om.status = 'active'
          AND om.tenant_id IS NULL
          AND COALESCE(om.unionid, om.open_id, om.external_id) IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'org_member', om.id, 'missing_provider_scope',
               jsonb_build_object('provider_id', om.provider_id, 'tenant_id', om.tenant_id)
        FROM org_members AS om
        LEFT JOIN identity_providers AS ip ON ip.id = om.provider_id
        WHERE ip.id IS NULL
          AND om.tenant_id IS NOT NULL
          AND om.user_id IS NOT NULL
          AND COALESCE(om.unionid, om.open_id, om.external_id) IS NOT NULL
        """
    )
    op.execute(
        f"""
        WITH candidates AS ({binding_candidates}),
        safe AS (
            SELECT DISTINCT ON (tenant_id, installation_scope, id_type, subject)
                   tenant_id, provider_id, installation_scope, channel_type,
                   id_type, subject, user_id
            FROM candidates AS candidate
            WHERE NOT EXISTS (
                SELECT 1
                FROM candidates AS other
                WHERE other.tenant_id = candidate.tenant_id
                  AND other.installation_scope = candidate.installation_scope
                  AND other.id_type = candidate.id_type
                  AND other.subject = candidate.subject
                  AND other.user_id <> candidate.user_id
            )
            ORDER BY tenant_id, installation_scope, id_type, subject, member_id
        )
        INSERT INTO channel_user_bindings
            (id, tenant_id, provider_id, installation_scope, channel_type,
             id_type, subject, user_id, created_at)
        SELECT md5(
                   'clawith-binding:' || tenant_id::text || ':' || installation_scope || ':' ||
                   id_type || ':' || subject
               )::uuid,
               tenant_id, provider_id, installation_scope, channel_type,
               id_type, subject, user_id, CURRENT_TIMESTAMP
        FROM safe
        ON CONFLICT (tenant_id, installation_scope, id_type, subject) DO NOTHING
        """
    )


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
