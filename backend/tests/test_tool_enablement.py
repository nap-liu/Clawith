import uuid
from types import SimpleNamespace

from app.services.tool_enablement import (
    agent_tool_enabled,
    default_tool_ids_to_seed,
    compute_backfill_rows,
)


def _at(enabled):
    return SimpleNamespace(enabled=enabled)


def _tool(is_default, tid=None):
    return SimpleNamespace(id=tid or uuid.uuid4(), is_default=is_default)


def test_enabled_is_false_when_no_assignment():
    assert agent_tool_enabled(None) is False


def test_enabled_is_false_when_assignment_disabled():
    assert agent_tool_enabled(_at(False)) is False


def test_enabled_is_true_only_when_assignment_enabled():
    assert agent_tool_enabled(_at(True)) is True


def test_seed_picks_only_default_tools_without_existing_row():
    t_on = _tool(True)
    t_off = _tool(False)
    t_existing = _tool(True)
    ids = default_tool_ids_to_seed(
        default_tools=[t_on, t_off, t_existing],
        existing_tool_ids={t_existing.id},
    )
    assert ids == [t_on.id]


def test_backfill_computes_missing_default_rows_per_agent():
    a1 = SimpleNamespace(id=uuid.uuid4())
    a2 = SimpleNamespace(id=uuid.uuid4())
    t1 = _tool(True)
    t2 = _tool(True)
    rows = compute_backfill_rows(
        agents=[a1, a2],
        default_tools=[t1, t2],
        existing_pairs={(a1.id, t1.id)},
    )
    assert (a1.id, t1.id) not in rows
    assert (a1.id, t2.id) in rows
    assert (a2.id, t1.id) in rows
    assert (a2.id, t2.id) in rows
    assert len(rows) == 3


def test_resolution_ignores_is_default_attribute():
    # Even if the assignment object carries is_default=True, only the explicit
    # enabled flag decides. This is the EXPLICIT-ONLY regression guard.
    assert agent_tool_enabled(SimpleNamespace(enabled=False, is_default=True)) is False
    assert agent_tool_enabled(SimpleNamespace(enabled=True, is_default=False)) is True


def test_new_agent_seeds_all_default_tools():
    t1 = _tool(True)
    t2 = _tool(True)
    t3 = _tool(False)
    ids = default_tool_ids_to_seed([t1, t2, t3], existing_tool_ids=set())
    assert set(ids) == {t1.id, t2.id}
