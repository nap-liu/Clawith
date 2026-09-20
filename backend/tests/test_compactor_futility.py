"""Observable compaction continuity, degradation, and stale-write behavior."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from app.services.llm import compactor
from app.services.llm.compactor import _do_compact
from compactor_futility_support import (
    _FakeDB,
    _FakeModel,
    _Row,
    _configure_local_storage,
    _fake_session_factory,
    _rows_with_span,
)
from tests.test_compactor_unit import _GOOD_SUMMARY


def _install_history(monkeypatch, db, rows, *, marker=(None, None, None)):
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=marker))


async def _compact(agent_id, session_id, model=None):
    return await _do_compact(
        agent_id=agent_id,
        session_id=session_id,
        conversation_id=session_id,
        model=model or _FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="post_round",
    )


@pytest.mark.asyncio
async def test_summary_receives_complete_replacement_and_retained_history(monkeypatch):
    db = _FakeDB()
    rows = _rows_with_span(4_000)
    summarize = AsyncMock(return_value=(_GOOD_SUMMARY, {"completion_tokens": 100}))
    _install_history(monkeypatch, db, rows)
    monkeypatch.setattr(compactor, "_summarize_via_llm", summarize)

    result = await _compact(uuid.uuid4(), "complete-history")

    assert result.triggered is True
    request = summarize.await_args.kwargs
    assert "x" * 1_000 in request["span_text"]
    assert "q0" in request["retained_history_text"]
    assert "q1" in request["retained_history_text"]
    assert "q2" in request["retained_history_text"]
    assert db.added[0].validation_failure_reason == "generation_mode=semantic"


@pytest.mark.asyncio
async def test_summary_is_not_rewritten_to_append_identifiers(monkeypatch):
    db = _FakeDB()
    rows = _rows_with_span(4_000)
    opaque_id = "5baa7373-b5c5-9eb4-8d10-d81aef980b1c"
    rows[0].content = f"Do not lose {opaque_id}. " + rows[0].content
    _install_history(monkeypatch, db, rows)
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(_GOOD_SUMMARY, {"completion_tokens": 100})),
    )

    result = await _compact(uuid.uuid4(), "no-identifier-append")

    assert result.triggered is True
    assert opaque_id not in db.added[0].summary_text
    assert db.added[0].summary_text.startswith(_GOOD_SUMMARY.strip())
    assert "### Lossless continuity archive" in db.added[0].summary_text
    assert "Full output saved to:" in db.added[0].summary_text


@pytest.mark.asyncio
async def test_provider_failure_archives_exact_source_and_marks_degraded(
    monkeypatch, tmp_path,
):
    db = _FakeDB()
    rows = _rows_with_span(4_000)
    agent_id = uuid.uuid4()
    source_fact = "MIDDLE-FACT-MUST-REMAIN-7d78878e"
    rows[2].content = rows[2].content[:2_000] + source_fact + rows[2].content[2_000:]
    _install_history(monkeypatch, db, rows)
    _configure_local_storage(monkeypatch, tmp_path)
    summarize = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(compactor, "_summarize_via_llm", summarize)

    result = await _compact(agent_id, "provider-fallback")

    assert result.triggered is True
    marker = db.added[0]
    assert marker.summary_validation_passed is True
    assert marker.validation_failure_reason.startswith("generation_mode=degraded;")
    assert "summary_llm_error:RuntimeError" in marker.validation_failure_reason
    assert "Degraded recovery" in marker.summary_text
    archive_dir = tmp_path / str(agent_id) / ".tool_results" / "provider-fallback"
    archived_source = next(archive_dir.iterdir()).read_text(encoding="utf-8")
    assert source_fact in archived_source


@pytest.mark.asyncio
async def test_invalid_draft_gets_one_repair_then_degrades(monkeypatch, tmp_path):
    db = _FakeDB()
    rows = _rows_with_span(4_000)
    agent_id = uuid.uuid4()
    _install_history(monkeypatch, db, rows)
    _configure_local_storage(monkeypatch, tmp_path)
    summarize = AsyncMock(
        side_effect=[
            ("invalid initial draft", {"completion_tokens": 10}),
            ("invalid repaired draft", {"completion_tokens": 10}),
        ]
    )
    monkeypatch.setattr(compactor, "_summarize_via_llm", summarize)

    result = await _compact(agent_id, "repair-fallback")

    assert result.triggered is True
    assert summarize.await_count == 2
    marker = db.added[0]
    assert marker.validation_failure_reason.startswith("generation_mode=degraded;")
    assert "validation_failed:" in marker.validation_failure_reason
    assert "repair_validation_failed:" in marker.validation_failure_reason


@pytest.mark.asyncio
async def test_successful_repair_records_machine_readable_mode(monkeypatch):
    db = _FakeDB()
    rows = _rows_with_span(4_000)
    _install_history(monkeypatch, db, rows)
    summarize = AsyncMock(
        side_effect=[
            ("invalid initial draft", {"completion_tokens": 10}),
            (_GOOD_SUMMARY, {"completion_tokens": 100}),
        ]
    )
    monkeypatch.setattr(compactor, "_summarize_via_llm", summarize)

    result = await _compact(uuid.uuid4(), "successful-repair")

    assert result.triggered is True
    marker = db.added[0]
    assert marker.summary_text.startswith(_GOOD_SUMMARY.strip())
    assert "### Lossless continuity archive" in marker.summary_text
    assert marker.validation_failure_reason.startswith("generation_mode=repaired;")


@pytest.mark.asyncio
async def test_reference_history_change_invalidates_candidate(monkeypatch):
    db = _FakeDB()
    rows = _rows_with_span(4_000)
    changed = list(rows)
    changed[-1] = _Row("assistant", "changed retained answer", offset=99)
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(
        compactor, "_load_active_rows", AsyncMock(side_effect=[rows, changed]),
    )
    monkeypatch.setattr(
        compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)),
    )
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(_GOOD_SUMMARY, {"completion_tokens": 100})),
    )

    result = await _compact(uuid.uuid4(), "stale-reference")

    assert result.triggered is False
    assert result.skipped_reason == "conversation_changed_during_compaction"
    assert db.added == []


@pytest.mark.asyncio
async def test_active_marker_change_invalidates_candidate(monkeypatch):
    db = _FakeDB()
    rows = _rows_with_span(4_000)
    old_id, new_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(
        compactor,
        "_load_active_marker",
        AsyncMock(side_effect=[("old", 1, old_id), ("new", 2, new_id)]),
    )
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(_GOOD_SUMMARY, {"completion_tokens": 100})),
    )

    result = await _compact(uuid.uuid4(), "stale-marker")

    assert result.triggered is False
    assert result.skipped_reason == "active_compaction_changed_during_compaction"
    assert db.added == []


@pytest.mark.asyncio
async def test_dryrun_does_not_write_loadable_marker(monkeypatch):
    db = _FakeDB()
    rows = _rows_with_span(4_000)
    _install_history(monkeypatch, db, rows)
    monkeypatch.setenv("CLAWITH_COMPACT_DRYRUN", "true")
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(_GOOD_SUMMARY, {"completion_tokens": 100})),
    )

    result = await _compact(uuid.uuid4(), "safe-dryrun")

    assert result.triggered is False
    assert result.skipped_reason == "dryrun"
    assert db.added == []
    assert db.rollback_count == 1
