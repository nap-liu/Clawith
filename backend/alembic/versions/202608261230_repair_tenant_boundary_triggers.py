"""Repair tenant-boundary triggers on schemas initialized outside Alembic.

Revision ID: repair_tenant_boundary_triggers
Revises: project_webhook_heads
"""

from alembic import op

revision = "repair_tenant_boundary_triggers"
down_revision = "project_webhook_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
        CREATE OR REPLACE FUNCTION enforce_chat_session_agent_tenant()
        RETURNS trigger AS $$
        DECLARE
            source_tenant uuid;
            peer_tenant uuid;
            human_tenant uuid;
        BEGIN
            SELECT tenant_id INTO source_tenant FROM agents WHERE id = NEW.agent_id;
            IF source_tenant IS NULL THEN
                RAISE EXCEPTION 'chat session source agent has no tenant';
            END IF;
            IF NEW.source_channel = 'agent' THEN
                IF NEW.is_group IS TRUE OR NEW.user_id IS NOT NULL OR NEW.peer_agent_id IS NULL THEN
                    RAISE EXCEPTION 'invalid canonical A2A chat session identity';
                END IF;
                SELECT tenant_id INTO peer_tenant FROM agents WHERE id = NEW.peer_agent_id;
                IF peer_tenant IS NULL OR source_tenant IS DISTINCT FROM peer_tenant THEN
                    RAISE EXCEPTION 'cross-tenant chat session agent edge';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.source_channel = 'subagent' THEN
                IF NEW.is_group IS TRUE OR NEW.peer_agent_id IS NOT NULL THEN
                    RAISE EXCEPTION 'invalid subagent chat session identity';
                END IF;
                IF NEW.user_id IS NULL THEN
                    RETURN NEW;
                END IF;
                SELECT tenant_id INTO human_tenant FROM users WHERE id = NEW.user_id;
                IF human_tenant IS NULL OR source_tenant IS DISTINCT FROM human_tenant THEN
                    RAISE EXCEPTION 'cross-tenant subagent human edge';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.is_group IS TRUE OR NEW.source_channel IN ('trigger', 'project') THEN
                IF NEW.user_id IS NOT NULL OR NEW.peer_agent_id IS NOT NULL THEN
                    RAISE EXCEPTION 'non-human chat session cannot carry a human or peer placeholder';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.peer_agent_id IS NOT NULL OR NEW.user_id IS NULL THEN
                RAISE EXCEPTION 'human P2P chat session requires exactly one user_id';
            END IF;
            SELECT tenant_id INTO human_tenant FROM users WHERE id = NEW.user_id;
            IF human_tenant IS NULL OR source_tenant IS DISTINCT FROM human_tenant THEN
                RAISE EXCEPTION 'cross-tenant chat session human edge';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_chat_session_agent_tenant ON chat_sessions")
    op.execute(
        """
        CREATE TRIGGER trg_chat_session_agent_tenant
        BEFORE INSERT OR UPDATE OF agent_id, user_id, peer_agent_id, source_channel, is_group ON chat_sessions
        FOR EACH ROW EXECUTE FUNCTION enforce_chat_session_agent_tenant()
        """
    )


def downgrade() -> None:
    # These guards are part of the pre-project tenant boundary and are safe for
    # the older service. Keep them in place when only the project release is
    # rolled back.
    pass
