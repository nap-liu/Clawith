"""Pure-function + idempotency tests for the RPA tool backfill."""
import uuid

from scripts.backfill_browser_rpa_tools import RPA_TOOL_NAMES, compute_rpa_rows


def test_rpa_tool_names_are_the_four_web_tools():
    assert set(RPA_TOOL_NAMES) == {"web_open", "web_eval", "web_cdp", "web_screenshot"}


def test_compute_rows_cartesian_minus_existing():
    a1, a2 = uuid.uuid4(), uuid.uuid4()
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    rows = compute_rpa_rows([a1, a2], [t1, t2], existing_pairs=set())
    assert set(rows) == {(a1, t1), (a1, t2), (a2, t1), (a2, t2)}


def test_compute_rows_is_idempotent():
    a1 = uuid.uuid4()
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    existing = {(a1, t1)}
    rows = compute_rpa_rows([a1], [t1, t2], existing_pairs=existing)
    assert rows == [(a1, t2)]
