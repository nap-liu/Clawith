"""Compatibility coverage for agent activity action labels."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.activity_log import AgentActivityLog
from tests.test_bootstrap_db import empty_database  # noqa: F401


def test_activity_action_enum_returns_unknown_database_labels_as_strings():
    action_type = AgentActivityLog.__table__.c.action_type.type.dialect_impl(postgresql.dialect())
    process = action_type.result_processor(postgresql.dialect(), None)
    assert process("agent_file_sent") == "agent_file_sent"
    assert process("future_project_file_action") == "future_project_file_action"


async def test_activity_action_migration_publishes_immediately_usable_labels(empty_database):  # noqa: F811
    path = Path(__file__).parents[1] / "alembic/versions/202608220900_add_agent_file_activity_actions.py"
    spec = importlib.util.spec_from_file_location("activity_action_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def upgrade_and_use(connection):
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            migration.upgrade()
            # New enum labels must be usable before the surrounding transaction ends.
            connection.execute(text(
                "INSERT INTO events VALUES ('agent_file_sent'), ('agent_file_received')"
            ))

    engine = create_async_engine(empty_database)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("CREATE TYPE activity_action_enum AS ENUM ('old_action')"))
            await connection.execute(text("CREATE TABLE events (action activity_action_enum)"))
            await connection.commit()
            await connection.run_sync(upgrade_and_use)
            await connection.run_sync(upgrade_and_use)
            rows = (await connection.execute(text(
                "SELECT action::text, count(*) FROM events GROUP BY action ORDER BY action::text"
            ))).all()
            assert rows == [("agent_file_received", 2), ("agent_file_sent", 2)]
    finally:
        await engine.dispose()
