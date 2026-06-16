"""Unit tests for scripts/backfill_tool_assignments.py.

Uses a FakeDB that dispatches execute() results by the SQLAlchemy entity the
select targets, so each call can return the right preconfigured list without
call-order coupling.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.tool import Tool, AgentTool
from app.models.system_settings import SystemSetting
import scripts.backfill_tool_assignments as backfill_mod


# ─────────────────────────────────────────────────────────────────────────────
# Helpers: stub makers
# ─────────────────────────────────────────────────────────────────────────────


def _agent(agent_id=None):
    return SimpleNamespace(id=agent_id or uuid.uuid4())


def _tool(tool_id=None, is_default=True):
    return SimpleNamespace(id=tool_id or uuid.uuid4(), is_default=is_default)


def _agent_tool(agent_id, tool_id):
    return SimpleNamespace(agent_id=agent_id, tool_id=tool_id)


def _flag_row():
    return SimpleNamespace(key=backfill_mod.FLAG_KEY, value={"inserted": 0})


# ─────────────────────────────────────────────────────────────────────────────
# FakeDB
# ─────────────────────────────────────────────────────────────────────────────


class FakeDB:
    """Minimal async-session stand-in that dispatches execute() by entity type.

    Provide preconfigured lists via the constructor:
      - agents: rows to return when the select targets Agent
      - default_tools: rows to return when the select targets Tool
      - agent_tools: rows to return when the select targets AgentTool
      - flag_rows: rows to return when the select targets SystemSetting
        (controls whether the flag appears to be set)
    """

    def __init__(
        self,
        *,
        agents: list | None = None,
        default_tools: list | None = None,
        agent_tools: list | None = None,
        flag_rows: list | None = None,
    ):
        self._data: dict[type, list] = {
            Agent: agents or [],
            Tool: default_tools or [],
            AgentTool: agent_tools or [],
            SystemSetting: flag_rows or [],
        }
        self.added: list = []
        self.committed = False

    # ── context manager ──────────────────────────────────────────────────────

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    # ── session methods ──────────────────────────────────────────────────────

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.committed = True

    async def execute(self, stmt):
        # Resolve which entity class the outermost select targets.
        entity = None
        try:
            # SQLAlchemy compiled selects expose column_descriptions on the
            # ClauseElement via .column_descriptions; for ORM selects the first
            # entry has "entity".
            descs = stmt.column_descriptions
            if descs:
                entity = descs[0].get("entity")
        except AttributeError:
            pass

        # Fallback: inspect the froms (handles WHERE-filtered selects).
        if entity is None:
            try:
                for frm in stmt.froms:
                    mapped = getattr(frm, "entity_zero", None)
                    if mapped is not None:
                        entity = mapped.class_
                        break
            except AttributeError:
                pass

        rows = self._data.get(entity, [])

        class _Scalars:
            def __init__(self, _rows):
                self._rows = _rows

            def all(self):
                return list(self._rows)

            def scalar_one_or_none(self):
                return self._rows[0] if self._rows else None

        class _Result:
            def __init__(self, _rows):
                self._rows = _rows

            def scalars(self):
                return _Scalars(self._rows)

            def scalar_one_or_none(self):
                return self._rows[0] if self._rows else None

        return _Result(rows)


def _make_session_factory(db: FakeDB):
    """Return a callable that behaves like async_session (a context manager)."""

    class _Factory:
        def __call__(self):
            return db

    return _Factory()


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_normal_run_inserts_rows_and_flag():
    """Flag NOT set, not dry-run: inserts AgentTool rows + SystemSetting flag."""
    a1 = _agent()
    t1 = _tool()
    t2 = _tool(is_default=False)  # not default — should NOT be inserted

    db = FakeDB(
        agents=[a1],
        default_tools=[t1, t2],
        agent_tools=[],  # no existing rows
        flag_rows=[],    # flag not set
    )

    with patch.object(backfill_mod, "async_session", _make_session_factory(db)):
        result = await backfill_mod.run(dry_run=False, force=False)

    assert result == 0

    agent_tool_adds = [o for o in db.added if isinstance(o, AgentTool)]
    flag_adds = [o for o in db.added if isinstance(o, SystemSetting)]

    assert len(agent_tool_adds) == 1
    assert agent_tool_adds[0].agent_id == a1.id
    assert agent_tool_adds[0].tool_id == t1.id
    assert agent_tool_adds[0].enabled is True

    assert len(flag_adds) == 1
    assert flag_adds[0].key == backfill_mod.FLAG_KEY
    assert flag_adds[0].value["inserted"] == 1

    assert db.committed is True


@pytest.mark.asyncio
async def test_dry_run_makes_no_writes():
    """--dry-run=True: no db.add calls at all."""
    a1 = _agent()
    t1 = _tool()

    db = FakeDB(
        agents=[a1],
        default_tools=[t1],
        agent_tools=[],
        flag_rows=[],
    )

    with patch.object(backfill_mod, "async_session", _make_session_factory(db)):
        result = await backfill_mod.run(dry_run=True, force=False)

    assert result == 0
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_flag_already_set_skips_without_force():
    """Flag set, force=False: returns early, no inserts."""
    a1 = _agent()
    t1 = _tool()

    db = FakeDB(
        agents=[a1],
        default_tools=[t1],
        agent_tools=[],
        flag_rows=[_flag_row()],  # flag IS set
    )

    with patch.object(backfill_mod, "async_session", _make_session_factory(db)):
        result = await backfill_mod.run(dry_run=False, force=False)

    assert result == 0
    assert db.added == []
    assert db.committed is False


@pytest.mark.asyncio
async def test_flag_already_set_but_force_proceeds():
    """Flag set but --force=True: proceeds and inserts rows."""
    a1 = _agent()
    t1 = _tool()

    db = FakeDB(
        agents=[a1],
        default_tools=[t1],
        agent_tools=[],
        flag_rows=[_flag_row()],  # flag IS set — but force overrides
    )

    with patch.object(backfill_mod, "async_session", _make_session_factory(db)):
        result = await backfill_mod.run(dry_run=False, force=True)

    assert result == 0

    agent_tool_adds = [o for o in db.added if isinstance(o, AgentTool)]
    assert len(agent_tool_adds) == 1
    assert agent_tool_adds[0].agent_id == a1.id
    assert agent_tool_adds[0].tool_id == t1.id
    assert db.committed is True

    # force bypasses the early-exit guard but does NOT re-insert the flag row
    # (the inner `if not await _flag_set(db):` guard still holds)
    flag_adds = [o for o in db.added if isinstance(o, SystemSetting)]
    assert len(flag_adds) == 0


@pytest.mark.asyncio
async def test_existing_pair_not_duplicated():
    """An (agent, tool) pair that already has an AgentTool row is not re-inserted."""
    a1 = _agent()
    t1 = _tool()

    db = FakeDB(
        agents=[a1],
        default_tools=[t1],
        agent_tools=[_agent_tool(a1.id, t1.id)],  # already exists
        flag_rows=[],
    )

    with patch.object(backfill_mod, "async_session", _make_session_factory(db)):
        result = await backfill_mod.run(dry_run=False, force=False)

    assert result == 0
    agent_tool_adds = [o for o in db.added if isinstance(o, AgentTool)]
    assert len(agent_tool_adds) == 0

    # script still commits and sets the flag even when zero rows are inserted
    assert db.committed is True
    flag_adds = [o for o in db.added if isinstance(o, SystemSetting)]
    assert len(flag_adds) == 1
