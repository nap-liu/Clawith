"""Canonical user/agent actors for sessions and messages.

Revision ID: canonical_chat_senders
Revises: identity_relationships_v1
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "canonical_chat_senders"
down_revision = "identity_relationships_v1"
branch_labels = None
depends_on = None


def _column_names(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def _index_names(inspector, table: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table)}


def _check_names(inspector, table: str) -> set[str]:
    return {constraint["name"] for constraint in inspector.get_check_constraints(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    message_columns = _column_names(inspector, "chat_messages")

    if "gateway_send_receipts" not in inspector.get_table_names():
        op.create_table(
            "gateway_send_receipts",
            sa.Column("id", postgresql.UUID(), nullable=False),
            sa.Column("source_agent_id", postgresql.UUID(), nullable=False),
            sa.Column("idempotency_key", sa.String(length=200), nullable=False),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("response_payload", postgresql.JSONB(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["source_agent_id"], ["agents.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source_agent_id",
                "idempotency_key",
                name="uq_gateway_send_receipts_source_key",
            ),
        )

    if not next(column for column in inspector.get_columns("chat_sessions") if column["name"] == "user_id")["nullable"]:
        op.alter_column("chat_sessions", "user_id", existing_type=postgresql.UUID(), nullable=True)
    if not next(column for column in inspector.get_columns("chat_messages") if column["name"] == "user_id")["nullable"]:
        op.alter_column("chat_messages", "user_id", existing_type=postgresql.UUID(), nullable=True)

    if "sender_user_id" not in message_columns:
        op.add_column("chat_messages", sa.Column("sender_user_id", postgresql.UUID(), nullable=True))
        op.create_foreign_key(
            "fk_chat_messages_sender_user_id_users",
            "chat_messages",
            "users",
            ["sender_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "sender_agent_id" not in message_columns:
        op.add_column("chat_messages", sa.Column("sender_agent_id", postgresql.UUID(), nullable=True))
        op.create_foreign_key(
            "fk_chat_messages_sender_agent_id_agents",
            "chat_messages",
            "agents",
            ["sender_agent_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # Participant is only a legacy read bridge.  Where it still contains an
    # exact User/Agent ref, copy that canonical ID before using role-based
    # fallback for older rows.
    op.execute(
        """
        UPDATE chat_messages AS message
        SET sender_user_id = participant.ref_id
        FROM participants AS participant
        WHERE message.participant_id = participant.id
          AND participant.type = 'user'
          AND message.sender_user_id IS NULL
          AND message.sender_agent_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE chat_messages AS message
        SET sender_agent_id = participant.ref_id
        FROM participants AS participant
        WHERE message.participant_id = participant.id
          AND participant.type = 'agent'
          AND message.sender_user_id IS NULL
          AND message.sender_agent_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE chat_messages AS message
        SET sender_user_id = message.user_id
        WHERE message.role = 'user'
          AND message.user_id IS NOT NULL
          AND message.sender_user_id IS NULL
          AND message.sender_agent_id IS NULL
          AND NOT EXISTS (
              SELECT 1
              FROM chat_sessions AS session
              WHERE session.id::text = message.conversation_id
                AND session.source_channel IN ('agent', 'trigger')
          )
        """
    )
    op.execute(
        """
        UPDATE chat_messages
        SET sender_agent_id = agent_id
        WHERE role IN ('assistant', 'tool_call')
          AND sender_user_id IS NULL
          AND sender_agent_id IS NULL
        """
    )

    # Preserve every legacy A2A message whose actor cannot be proven. Runtime
    # consumers see no canonical sender and therefore fail closed; operators
    # get the full row needed for deterministic repair.
    op.execute(
        """
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'chat_message', message.id, 'unresolved_canonical_sender',
               jsonb_build_object(
                   'message', to_jsonb(message),
                   'session', to_jsonb(session)
               )
        FROM chat_messages AS message
        JOIN chat_sessions AS session ON session.id::text = message.conversation_id
        WHERE session.source_channel = 'agent'
          AND message.role <> 'system'
          AND message.sender_user_id IS NULL
          AND message.sender_agent_id IS NULL
        """
    )

    # Remove creator-user placeholders from non-human sessions and their
    # messages after canonical sender IDs have been frozen.
    op.execute(
        """
        UPDATE chat_messages AS message
        SET user_id = NULL
        WHERE EXISTS (
            SELECT 1
            FROM chat_sessions AS session
            WHERE session.id::text = message.conversation_id
              AND (session.is_group = true OR session.source_channel IN ('agent', 'trigger'))
        )
        """
    )
    op.execute(
        """
        UPDATE chat_sessions
        SET user_id = NULL
        WHERE is_group = true OR source_channel IN ('agent', 'trigger')
        """
    )

    # Preserve malformed historical A2A edges for operator repair and prevent
    # every future cross-tenant peer link at the database boundary.
    op.execute(
        """
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'chat_session', session.id, 'cross_tenant_a2a_session',
               jsonb_build_object(
                   'session', to_jsonb(session),
                   'source_tenant_id', source.tenant_id,
                   'peer_tenant_id', peer.tenant_id
               )
        FROM chat_sessions AS session
        JOIN agents AS source ON source.id = session.agent_id
        JOIN agents AS peer ON peer.id = session.peer_agent_id
        WHERE session.source_channel = 'agent'
          AND source.tenant_id IS DISTINCT FROM peer.tenant_id
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'chat_session', session.id, 'cross_tenant_human_session',
               jsonb_build_object(
                   'session', to_jsonb(session),
                   'agent_tenant_id', source.tenant_id,
                   'user_tenant_id', human.tenant_id
               )
        FROM chat_sessions AS session
        JOIN agents AS source ON source.id = session.agent_id
        JOIN users AS human ON human.id = session.user_id
        WHERE session.is_group IS FALSE
          AND session.source_channel NOT IN ('agent', 'trigger')
          AND source.tenant_id IS DISTINCT FROM human.tenant_id
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_chat_session_agent_tenant()
        RETURNS trigger AS $$
        DECLARE
            source_tenant UUID;
            peer_tenant UUID;
            human_tenant UUID;
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
                SELECT tenant_id INTO peer_tenant FROM agents WHERE id = NEW.peer_agent_id;
                IF peer_tenant IS NULL OR source_tenant IS DISTINCT FROM peer_tenant THEN
                    RAISE EXCEPTION 'cross-tenant chat session agent edge';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.is_group IS TRUE OR NEW.source_channel = 'trigger' THEN
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
        BEFORE INSERT OR UPDATE OF agent_id, user_id, peer_agent_id,
            source_channel, is_group ON chat_sessions
        FOR EACH ROW EXECUTE FUNCTION enforce_chat_session_agent_tenant();
        """
    )

    op.execute(
        """
        INSERT INTO identity_relationship_migration_conflicts
            (entity_type, entity_id, conflict_type, details)
        SELECT 'chat_message', message.id, 'cross_tenant_chat_sender',
               jsonb_build_object(
                   'message', to_jsonb(message),
                   'agent_tenant_id', owner.tenant_id,
                   'sender_user_tenant_id', sender_user.tenant_id,
                   'sender_agent_tenant_id', sender_agent.tenant_id
               )
        FROM chat_messages AS message
        JOIN agents AS owner ON owner.id = message.agent_id
        LEFT JOIN users AS sender_user ON sender_user.id = message.sender_user_id
        LEFT JOIN agents AS sender_agent ON sender_agent.id = message.sender_agent_id
        WHERE (message.sender_user_id IS NOT NULL
               AND owner.tenant_id IS DISTINCT FROM sender_user.tenant_id)
           OR (message.sender_agent_id IS NOT NULL
               AND owner.tenant_id IS DISTINCT FROM sender_agent.tenant_id)
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_chat_message_sender_tenant()
        RETURNS trigger AS $$
        DECLARE
            owner_tenant UUID;
            sender_tenant UUID;
        BEGIN
            SELECT tenant_id INTO owner_tenant FROM agents WHERE id = NEW.agent_id;
            IF owner_tenant IS NULL THEN
                -- Legacy tenantless agents remain invisible to tenant-bound
                -- history APIs. Once assigned a tenant, every sender is
                -- enforced below.
                RETURN NEW;
            END IF;
            IF NEW.sender_user_id IS NOT NULL THEN
                SELECT tenant_id INTO sender_tenant FROM users WHERE id = NEW.sender_user_id;
            ELSIF NEW.sender_agent_id IS NOT NULL THEN
                SELECT tenant_id INTO sender_tenant FROM agents WHERE id = NEW.sender_agent_id;
            ELSE
                RETURN NEW;
            END IF;
            IF sender_tenant IS NULL OR owner_tenant IS DISTINCT FROM sender_tenant THEN
                RAISE EXCEPTION 'cross-tenant canonical chat message sender';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_chat_message_sender_tenant ON chat_messages")
    op.execute(
        """
        CREATE TRIGGER trg_chat_message_sender_tenant
        BEFORE INSERT OR UPDATE OF agent_id, sender_user_id, sender_agent_id
        ON chat_messages
        FOR EACH ROW EXECUTE FUNCTION enforce_chat_message_sender_tenant();
        """
    )

    # Close the time-of-check gap for legacy tenantless identities. A canonical
    # session/message may be preserved while its owner has no tenant, but a
    # later tenant assignment must be compatible with every frozen actor edge.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_related_user_tenant_move()
        RETURNS trigger AS $$
        BEGIN
            IF OLD.tenant_id IS DISTINCT FROM NEW.tenant_id
               AND (
                   EXISTS (SELECT 1 FROM agent_relationships WHERE user_id = NEW.id)
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
                RAISE EXCEPTION 'cannot change user tenant across canonical identity edges';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
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
                       LEFT JOIN users AS sender_user ON sender_user.id = message.sender_user_id
                       LEFT JOIN agents AS sender_agent ON sender_agent.id = message.sender_agent_id
                       WHERE message.agent_id = NEW.id
                         AND (
                             (message.sender_user_id IS NOT NULL
                              AND sender_user.tenant_id IS DISTINCT FROM NEW.tenant_id)
                             OR (message.sender_agent_id IS NOT NULL
                                 AND sender_agent.tenant_id IS DISTINCT FROM NEW.tenant_id)
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
                RAISE EXCEPTION 'cannot change agent tenant across canonical identity edges';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    inspector = inspect(bind)
    indexes = _index_names(inspector, "chat_messages")
    if "ix_chat_messages_sender_user_id" not in indexes:
        op.create_index("ix_chat_messages_sender_user_id", "chat_messages", ["sender_user_id"])
    if "ix_chat_messages_sender_agent_id" not in indexes:
        op.create_index("ix_chat_messages_sender_agent_id", "chat_messages", ["sender_agent_id"])
    if "ck_chat_messages_single_canonical_sender" not in _check_names(inspector, "chat_messages"):
        op.create_check_constraint(
            "ck_chat_messages_single_canonical_sender",
            "chat_messages",
            "NOT (sender_user_id IS NOT NULL AND sender_agent_id IS NOT NULL)",
        )

    # Historical A2A gateway writers populated both columns with the source
    # Agent creator and the actual source Agent. The digital employee is the
    # canonical actor, so remove only that synthetic human placeholder and
    # enforce the invariant for every future write path.
    if "gateway_messages" in inspect(bind).get_table_names():
        op.execute(
            """
            UPDATE gateway_messages
            SET sender_user_id = NULL
            WHERE sender_agent_id IS NOT NULL AND sender_user_id IS NOT NULL
            """
        )
        inspector = inspect(bind)
        if "ck_gateway_messages_single_canonical_sender" not in _check_names(
            inspector, "gateway_messages"
        ):
            op.create_check_constraint(
                "ck_gateway_messages_single_canonical_sender",
                "gateway_messages",
                "NOT (sender_agent_id IS NOT NULL AND sender_user_id IS NOT NULL)",
            )


def downgrade() -> None:
    # Restore legacy non-null placeholders before removing the canonical actor
    # columns.  This is intentionally compatibility-only; the forward migration
    # is the authoritative normalized state.
    op.execute("DROP TRIGGER IF EXISTS trg_chat_session_agent_tenant ON chat_sessions")
    op.execute("DROP FUNCTION IF EXISTS enforce_chat_session_agent_tenant()")
    op.execute("DROP TRIGGER IF EXISTS trg_chat_message_sender_tenant ON chat_messages")
    op.execute("DROP FUNCTION IF EXISTS enforce_chat_message_sender_tenant()")
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
    op.execute(
        """
        UPDATE chat_sessions AS session
        SET user_id = agent.creator_id
        FROM agents AS agent
        WHERE session.agent_id = agent.id AND session.user_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE chat_messages AS message
        SET user_id = agent.creator_id
        FROM agents AS agent
        WHERE message.agent_id = agent.id AND message.user_id IS NULL
        """
    )
    bind = op.get_bind()
    inspector = inspect(bind)
    if "gateway_send_receipts" in inspector.get_table_names():
        op.drop_table("gateway_send_receipts")
    if (
        "gateway_messages" in inspector.get_table_names()
        and "ck_gateway_messages_single_canonical_sender"
        in _check_names(inspector, "gateway_messages")
    ):
        op.drop_constraint(
            "ck_gateway_messages_single_canonical_sender",
            "gateway_messages",
            type_="check",
        )
    op.drop_constraint("ck_chat_messages_single_canonical_sender", "chat_messages", type_="check")
    op.drop_index("ix_chat_messages_sender_agent_id", table_name="chat_messages")
    op.drop_index("ix_chat_messages_sender_user_id", table_name="chat_messages")
    op.drop_constraint("fk_chat_messages_sender_agent_id_agents", "chat_messages", type_="foreignkey")
    op.drop_constraint("fk_chat_messages_sender_user_id_users", "chat_messages", type_="foreignkey")
    op.drop_column("chat_messages", "sender_agent_id")
    op.drop_column("chat_messages", "sender_user_id")
    op.alter_column("chat_messages", "user_id", existing_type=postgresql.UUID(), nullable=False)
    op.alter_column("chat_sessions", "user_id", existing_type=postgresql.UUID(), nullable=False)
