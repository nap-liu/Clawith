"""Concurrency fences for active-turn compaction."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest

from app.services.llm import compactor
from app.services.llm.compactor import _do_compact
from compactor_futility_support import (
    _FakeDB,
    _FakeModel,
    _Row,
    _fake_session_factory,
)


@pytest.mark.asyncio
async def test_current_turn_stale_boundary_rejects_new_closed_round(monkeypatch):
    db = _FakeDB()
    anchor = _Row("user", "active objective", offset=0)

    def tool_row(offset, *, round_id, status):
        row = _Row(
            "tool_call",
            json.dumps({
                "name": "read_file",
                "call_id": round_id,
                "round_id": round_id,
                "status": status,
            }),
            offset=offset,
        )
        row.message_meta = {"turn_anchor_id": str(anchor.id)}
        return row

    first = [
        tool_row(1, round_id="round-1", status="running"),
        tool_row(2, round_id="round-1", status="done"),
    ]
    changed_done = tool_row(2, round_id="round-1", status="done")
    changed_done.id = first[1].id
    changed_payload = json.loads(changed_done.content)
    changed_payload["result"] = "rewritten during summary"
    changed_done.content = json.dumps(changed_payload)
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(
        compactor,
        "_load_active_rows",
        AsyncMock(side_effect=[[anchor, *first], [anchor, first[0], changed_done]]),
    )
    monkeypatch.setattr(
        compactor,
        "_load_active_marker",
        AsyncMock(return_value=(None, None, None)),
    )
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=("## Summary of earlier conversation\n- first round", None)),
    )
    monkeypatch.setattr(compactor, "validate_summary", lambda **_kw: (True, None, 1.0))

    result = await _do_compact(
        agent_id=uuid.uuid4(),
        session_id="current-turn-stale",
        conversation_id="current-turn-stale",
        model=_FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="provider_hard_limit",
        current_anchor_id=anchor.id,
    )

    assert result.triggered is False
    assert result.skipped_reason == "compactable_span_changed_during_compaction"
    assert db.rollback_count == 1
