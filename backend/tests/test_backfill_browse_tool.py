"""Pure row-computation for the browse-tool backfill."""
import uuid

from scripts.backfill_browse_tool import compute_browse_rows


def test_computes_rows_only_for_agents_without_existing_pair():
    a1, a2 = uuid.uuid4(), uuid.uuid4()
    tool_id = uuid.uuid4()
    existing = {(a1, tool_id)}  # a1 already has it
    rows = compute_browse_rows([a1, a2], tool_id, existing)
    assert rows == [(a2, tool_id)]


def test_empty_when_all_assigned():
    a1 = uuid.uuid4()
    tool_id = uuid.uuid4()
    assert compute_browse_rows([a1], tool_id, {(a1, tool_id)}) == []
