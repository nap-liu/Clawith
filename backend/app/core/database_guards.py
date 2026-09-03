"""Install PostgreSQL guards that SQLAlchemy metadata cannot represent."""

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.engine import Connection


@dataclass(frozen=True)
class DatabaseGuard:
    table: str
    trigger: str
    function_sql: str
    trigger_sql: str


CURRENT_DATABASE_GUARDS = (
    DatabaseGuard(
        table="agent_relationships",
        trigger="trg_agent_relationship_tenant",
        function_sql="""
            CREATE OR REPLACE FUNCTION enforce_agent_user_relationship_tenant()
            RETURNS trigger AS $$
            DECLARE source_tenant uuid;
            DECLARE target_tenant uuid;
            BEGIN
                SELECT tenant_id INTO source_tenant FROM agents WHERE id = NEW.agent_id;
                SELECT tenant_id INTO target_tenant FROM users WHERE id = NEW.user_id;
                IF source_tenant IS NULL OR target_tenant IS NULL
                   OR source_tenant <> target_tenant THEN
                    RAISE EXCEPTION 'agent_relationship tenant mismatch';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """,
        trigger_sql="""
            CREATE TRIGGER trg_agent_relationship_tenant
            BEFORE INSERT OR UPDATE OF agent_id, user_id ON agent_relationships
            FOR EACH ROW EXECUTE FUNCTION enforce_agent_user_relationship_tenant()
        """,
    ),
    DatabaseGuard(
        table="agent_agent_relationships",
        trigger="trg_agent_agent_relationship_tenant",
        function_sql="""
            CREATE OR REPLACE FUNCTION enforce_agent_agent_relationship_tenant()
            RETURNS trigger AS $$
            DECLARE source_tenant uuid;
            DECLARE target_tenant uuid;
            BEGIN
                SELECT tenant_id INTO source_tenant FROM agents WHERE id = NEW.agent_id;
                SELECT tenant_id INTO target_tenant FROM agents WHERE id = NEW.target_agent_id;
                IF source_tenant IS NULL OR target_tenant IS NULL
                   OR source_tenant <> target_tenant THEN
                    RAISE EXCEPTION 'agent_agent_relationship tenant mismatch';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """,
        trigger_sql="""
            CREATE TRIGGER trg_agent_agent_relationship_tenant
            BEFORE INSERT OR UPDATE OF agent_id, target_agent_id
            ON agent_agent_relationships
            FOR EACH ROW EXECUTE FUNCTION enforce_agent_agent_relationship_tenant()
        """,
    ),
    DatabaseGuard(
        table="users",
        trigger="trg_related_user_tenant_move",
        function_sql="""
            CREATE OR REPLACE FUNCTION prevent_related_user_tenant_move()
            RETURNS trigger AS $$
            BEGIN
                IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
                   AND (
                       EXISTS (
                           SELECT 1 FROM agent_relationships WHERE user_id = NEW.id
                       )
                       OR EXISTS (
                           SELECT 1
                           FROM chat_sessions AS session
                           JOIN agents AS owner ON owner.id = session.agent_id
                           WHERE session.user_id = NEW.id
                             AND owner.tenant_id IS DISTINCT FROM NEW.tenant_id
                       )
                       OR EXISTS (
                           SELECT 1
                           FROM chat_messages AS message
                           JOIN agents AS owner ON owner.id = message.agent_id
                           WHERE message.sender_user_id = NEW.id
                             AND owner.tenant_id IS DISTINCT FROM NEW.tenant_id
                       )
                   ) THEN
                    RAISE EXCEPTION
                        'cannot change user tenant across canonical identity edges';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """,
        trigger_sql="""
            CREATE TRIGGER trg_related_user_tenant_move
            BEFORE UPDATE OF tenant_id ON users
            FOR EACH ROW EXECUTE FUNCTION prevent_related_user_tenant_move()
        """,
    ),
    DatabaseGuard(
        table="agents",
        trigger="trg_related_agent_tenant_move",
        function_sql="""
            CREATE OR REPLACE FUNCTION prevent_related_agent_tenant_move()
            RETURNS trigger AS $$
            BEGIN
                IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
                   AND (
                       EXISTS (
                           SELECT 1 FROM agent_relationships WHERE agent_id = NEW.id
                       )
                       OR EXISTS (
                           SELECT 1 FROM agent_agent_relationships
                           WHERE agent_id = NEW.id OR target_agent_id = NEW.id
                       )
                       OR EXISTS (
                           SELECT 1
                           FROM chat_sessions AS session
                           LEFT JOIN users AS human ON human.id = session.user_id
                           LEFT JOIN agents AS peer ON peer.id = session.peer_agent_id
                           WHERE session.agent_id = NEW.id
                             AND (
                                 (session.user_id IS NOT NULL
                                  AND human.tenant_id IS DISTINCT FROM NEW.tenant_id)
                                 OR (session.peer_agent_id IS NOT NULL
                                     AND peer.tenant_id IS DISTINCT FROM NEW.tenant_id)
                             )
                       )
                       OR EXISTS (
                           SELECT 1
                           FROM chat_sessions AS session
                           JOIN agents AS owner ON owner.id = session.agent_id
                           WHERE session.peer_agent_id = NEW.id
                             AND owner.tenant_id IS DISTINCT FROM NEW.tenant_id
                       )
                       OR EXISTS (
                           SELECT 1
                           FROM chat_messages AS message
                           LEFT JOIN users AS sender_user
                             ON sender_user.id = message.sender_user_id
                           LEFT JOIN agents AS sender_agent
                             ON sender_agent.id = message.sender_agent_id
                           WHERE message.agent_id = NEW.id
                             AND (
                                 (message.sender_user_id IS NOT NULL
                                  AND sender_user.tenant_id
                                      IS DISTINCT FROM NEW.tenant_id)
                                 OR (message.sender_agent_id IS NOT NULL
                                     AND sender_agent.tenant_id
                                         IS DISTINCT FROM NEW.tenant_id)
                             )
                       )
                       OR EXISTS (
                           SELECT 1
                           FROM chat_messages AS message
                           JOIN agents AS owner ON owner.id = message.agent_id
                           WHERE message.sender_agent_id = NEW.id
                             AND owner.tenant_id IS DISTINCT FROM NEW.tenant_id
                       )
                   ) THEN
                    RAISE EXCEPTION
                        'cannot change agent tenant across canonical identity edges';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """,
        trigger_sql="""
            CREATE TRIGGER trg_related_agent_tenant_move
            BEFORE UPDATE OF tenant_id ON agents
            FOR EACH ROW EXECUTE FUNCTION prevent_related_agent_tenant_move()
        """,
    ),
    DatabaseGuard(
        table="chat_sessions",
        trigger="trg_chat_session_agent_tenant",
        function_sql="""
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
                    IF NEW.is_group IS TRUE OR NEW.user_id IS NOT NULL
                       OR NEW.peer_agent_id IS NULL THEN
                        RAISE EXCEPTION 'invalid canonical A2A chat session identity';
                    END IF;
                    SELECT tenant_id INTO peer_tenant
                    FROM agents WHERE id = NEW.peer_agent_id;
                    IF peer_tenant IS NULL
                       OR source_tenant IS DISTINCT FROM peer_tenant THEN
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
                    SELECT tenant_id INTO human_tenant
                    FROM users WHERE id = NEW.user_id;
                    IF human_tenant IS NULL
                       OR source_tenant IS DISTINCT FROM human_tenant THEN
                        RAISE EXCEPTION 'cross-tenant subagent human edge';
                    END IF;
                    RETURN NEW;
                END IF;
                IF NEW.is_group IS TRUE
                   OR NEW.source_channel IN ('trigger', 'project') THEN
                    IF NEW.user_id IS NOT NULL OR NEW.peer_agent_id IS NOT NULL THEN
                        RAISE EXCEPTION
                            'non-human chat session cannot carry a human or peer placeholder';
                    END IF;
                    RETURN NEW;
                END IF;
                IF NEW.peer_agent_id IS NOT NULL OR NEW.user_id IS NULL THEN
                    RAISE EXCEPTION
                        'human P2P chat session requires exactly one user_id';
                END IF;
                SELECT tenant_id INTO human_tenant FROM users WHERE id = NEW.user_id;
                IF human_tenant IS NULL
                   OR source_tenant IS DISTINCT FROM human_tenant THEN
                    RAISE EXCEPTION 'cross-tenant chat session human edge';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """,
        trigger_sql="""
            CREATE TRIGGER trg_chat_session_agent_tenant
            BEFORE INSERT OR UPDATE OF
                agent_id, user_id, peer_agent_id, source_channel, is_group
            ON chat_sessions
            FOR EACH ROW EXECUTE FUNCTION enforce_chat_session_agent_tenant()
        """,
    ),
    DatabaseGuard(
        table="chat_messages",
        trigger="trg_chat_message_sender_tenant",
        function_sql="""
            CREATE OR REPLACE FUNCTION enforce_chat_message_sender_tenant()
            RETURNS trigger AS $$
            DECLARE
                owner_tenant uuid;
                sender_tenant uuid;
            BEGIN
                SELECT tenant_id INTO owner_tenant FROM agents WHERE id = NEW.agent_id;
                IF owner_tenant IS NULL THEN
                    RETURN NEW;
                END IF;
                IF NEW.sender_user_id IS NOT NULL THEN
                    SELECT tenant_id INTO sender_tenant
                    FROM users WHERE id = NEW.sender_user_id;
                ELSIF NEW.sender_agent_id IS NOT NULL THEN
                    SELECT tenant_id INTO sender_tenant
                    FROM agents WHERE id = NEW.sender_agent_id;
                ELSE
                    RETURN NEW;
                END IF;
                IF sender_tenant IS NULL
                   OR owner_tenant IS DISTINCT FROM sender_tenant THEN
                    RAISE EXCEPTION 'cross-tenant canonical chat message sender';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
        """,
        trigger_sql="""
            CREATE TRIGGER trg_chat_message_sender_tenant
            BEFORE INSERT OR UPDATE OF agent_id, sender_user_id, sender_agent_id
            ON chat_messages
            FOR EACH ROW EXECUTE FUNCTION enforce_chat_message_sender_tenant()
        """,
    ),
    DatabaseGuard(
        table="tasks",
        trigger="trg_task_supervision_target_tenant",
        function_sql="""
            CREATE OR REPLACE FUNCTION enforce_task_supervision_target_tenant()
            RETURNS trigger AS $$
            DECLARE
                source_tenant uuid;
                target_tenant uuid;
            BEGIN
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
            $$ LANGUAGE plpgsql
        """,
        trigger_sql="""
            CREATE TRIGGER trg_task_supervision_target_tenant
            BEFORE INSERT OR UPDATE OF
                agent_id, type, supervision_target_user_id,
                supervision_target_agent_id
            ON tasks
            FOR EACH ROW EXECUTE FUNCTION enforce_task_supervision_target_tenant()
        """,
    ),
)


def ensure_current_database_guards(connection: Connection) -> None:
    """Idempotently repair non-metadata database guards after schema setup."""
    if connection.dialect.name != "postgresql":
        return

    for guard in CURRENT_DATABASE_GUARDS:
        connection.execute(sa.text(guard.function_sql))
        exists = connection.scalar(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_trigger "
                "WHERE tgname = :trigger_name "
                "AND tgrelid = to_regclass(:table_name) "
                "AND NOT tgisinternal)"
            ),
            {"trigger_name": guard.trigger, "table_name": guard.table},
        )
        if not exists:
            connection.execute(sa.text(guard.trigger_sql))
