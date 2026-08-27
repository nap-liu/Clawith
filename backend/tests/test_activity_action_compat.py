"""Compatibility coverage for agent activity action labels."""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.models.activity_log import AgentActivityLog, ForwardCompatibleEnum


def _load_activity_migration() -> ModuleType:
    migration_path = (
        Path(__file__).parents[1] / "alembic" / "versions" / "202608220900_add_agent_file_activity_actions.py"
    )
    spec = importlib.util.spec_from_file_location("activity_action_migration", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_activity_action_enum_returns_unknown_database_labels_as_strings():
    action_type = AgentActivityLog.__table__.c.action_type.type
    dialect = postgresql.dialect()
    dialect_type = action_type.dialect_impl(dialect)

    assert isinstance(action_type, ForwardCompatibleEnum)
    assert isinstance(dialect_type, ForwardCompatibleEnum)
    result_processor = dialect_type.result_processor(dialect, None)
    assert result_processor is not None
    assert result_processor("agent_file_sent") == "agent_file_sent"
    assert result_processor("future_project_file_action") == "future_project_file_action"


def test_activity_action_migration_publishes_labels_in_autocommit_block(monkeypatch):
    migration = _load_activity_migration()
    state = {"inside_autocommit": False}
    statements: list[tuple[str, bool]] = []

    @contextmanager
    def autocommit_block():
        state["inside_autocommit"] = True
        try:
            yield
        finally:
            state["inside_autocommit"] = False

    monkeypatch.setattr(
        migration.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    monkeypatch.setattr(
        migration.op,
        "get_context",
        lambda: SimpleNamespace(autocommit_block=autocommit_block),
    )
    monkeypatch.setattr(
        migration.op,
        "execute",
        lambda statement: statements.append((statement, state["inside_autocommit"])),
    )

    migration.upgrade()

    assert statements == [
        ("ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS 'agent_file_sent'", True),
        ("ALTER TYPE activity_action_enum ADD VALUE IF NOT EXISTS 'agent_file_received'", True),
    ]


def test_activity_action_migration_is_a_noop_outside_postgresql(monkeypatch):
    migration = _load_activity_migration()
    statements: list[str] = []

    monkeypatch.setattr(
        migration.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
    )
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert statements == []
