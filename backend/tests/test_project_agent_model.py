"""Model invariants for project-native Agent derivatives."""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint

from app.models.agent import Agent


def _constraint_sql(name: str) -> str:
    constraint = next(
        item for item in Agent.__table__.constraints if isinstance(item, CheckConstraint) and item.name == name
    )
    return str(constraint.sqltext)


def test_project_agent_columns_and_default() -> None:
    table = Agent.__table__

    assert table.c.scope.default.arg == "standard"
    assert table.c.scope.server_default.arg == "standard"
    assert table.c.scope.nullable is False
    assert table.c.project_id.nullable is True
    assert table.c.source_agent_id.nullable is True
    assert table.c.agent_dir.nullable is True


def test_project_agent_foreign_keys_preserve_lifecycle_and_lineage() -> None:
    table = Agent.__table__
    project_fk = next(iter(table.c.project_id.foreign_keys))
    source_fk = next(iter(table.c.source_agent_id.foreign_keys))

    assert project_fk.target_fullname == "projects.id"
    assert project_fk.ondelete == "CASCADE"
    assert source_fk.target_fullname == "agents.id"
    assert source_fk.ondelete == "SET NULL"


def test_project_agent_scope_constraints_are_explicit() -> None:
    scope_sql = _constraint_sql("ck_agents_scope")
    project_scope_sql = _constraint_sql("ck_agents_project_scope")

    assert "'standard', 'project'" in scope_sql
    assert "scope = 'standard' AND project_id IS NULL" in project_scope_sql
    assert "scope = 'project' AND project_id IS NOT NULL" in project_scope_sql
    assert "agent_dir IS NOT NULL" in project_scope_sql


def test_project_agent_directory_is_derived_from_agent_id() -> None:
    agent_id = uuid.uuid4()

    assert Agent.project_agent_dir(agent_id) == f".agents/{agent_id}"
