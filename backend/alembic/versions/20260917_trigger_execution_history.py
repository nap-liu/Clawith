"""Preserve trigger execution history after definition deletion."""

from alembic import op
import sqlalchemy as sa


revision = "trigger_execution_history"
down_revision = "skill_management"
branch_labels = None
depends_on = None


def _trigger_foreign_key():
    for foreign_key in sa.inspect(op.get_bind()).get_foreign_keys("trigger_executions"):
        if foreign_key.get("constrained_columns") == ["trigger_id"]:
            return foreign_key.get("name")
    return None


def _create_compatibility_triggers():
    op.execute(
        sa.text(
            """
            CREATE FUNCTION snapshot_trigger_execution_source()
            RETURNS trigger AS $$
            DECLARE
                source_name varchar(100);
            BEGIN
                SELECT name INTO source_name
                FROM agent_triggers
                WHERE id = NEW.trigger_id
                FOR KEY SHARE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'Trigger no longer exists'
                        USING ERRCODE = 'foreign_key_violation';
                END IF;
                IF NEW.trigger_name = '' THEN
                    NEW.trigger_name = source_name;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER snapshot_trigger_execution_source
            BEFORE INSERT OR UPDATE OF trigger_id ON trigger_executions
            FOR EACH ROW EXECUTE FUNCTION snapshot_trigger_execution_source()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION protect_trigger_definition_delete()
            RETURNS trigger AS $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM agents WHERE id = OLD.agent_id) THEN
                    RETURN OLD;
                END IF;
                IF OLD.is_system THEN
                    RAISE EXCEPTION 'System triggers cannot be deleted'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF OLD.is_enabled THEN
                    RAISE EXCEPTION 'Cancel the trigger before deleting it'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM trigger_executions
                    WHERE trigger_id = OLD.id
                      AND status IN ('pending', 'processing')
                ) THEN
                    RAISE EXCEPTION 'Wait for the current run to finish before deleting it'
                        USING ERRCODE = 'check_violation';
                END IF;
                UPDATE trigger_executions
                SET trigger_name = OLD.name
                WHERE trigger_id = OLD.id AND trigger_name = '';
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER protect_trigger_definition_delete
            BEFORE DELETE ON agent_triggers
            FOR EACH ROW EXECUTE FUNCTION protect_trigger_definition_delete()
            """
        )
    )


def upgrade():
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '120s'"))
    op.add_column(
        "trigger_executions",
        sa.Column(
            "trigger_name",
            sa.String(length=100),
            nullable=True,
            server_default=sa.text("''"),
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE trigger_executions AS execution
            SET trigger_name = trigger.name
            FROM agent_triggers AS trigger
            WHERE trigger.id = execution.trigger_id
            """
        )
    )
    missing = op.get_bind().execute(
        sa.text("SELECT count(*) FROM trigger_executions WHERE trigger_name IS NULL")
    ).scalar_one()
    if missing:
        raise RuntimeError("Cannot snapshot trigger execution names: orphan rows exist")
    op.alter_column(
        "trigger_executions",
        "trigger_name",
        nullable=False,
        server_default=sa.text("''"),
    )
    _create_compatibility_triggers()

    foreign_key = _trigger_foreign_key()
    if foreign_key:
        op.drop_constraint(foreign_key, "trigger_executions", type_="foreignkey")


def downgrade():
    orphan_count = op.get_bind().execute(
        sa.text(
            """
            SELECT count(*)
            FROM trigger_executions AS execution
            LEFT JOIN agent_triggers AS trigger ON trigger.id = execution.trigger_id
            WHERE trigger.id IS NULL
            """
        )
    ).scalar_one()
    if orphan_count:
        raise RuntimeError(
            "Cannot restore the trigger foreign key while preserved execution history references deleted triggers"
        )
    op.execute("DROP TRIGGER IF EXISTS protect_trigger_definition_delete ON agent_triggers")
    op.execute("DROP FUNCTION IF EXISTS protect_trigger_definition_delete()")
    op.execute("DROP TRIGGER IF EXISTS snapshot_trigger_execution_source ON trigger_executions")
    op.execute("DROP FUNCTION IF EXISTS snapshot_trigger_execution_source()")
    op.create_foreign_key(
        "trigger_executions_trigger_id_fkey",
        "trigger_executions",
        "agent_triggers",
        ["trigger_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_column("trigger_executions", "trigger_name")
