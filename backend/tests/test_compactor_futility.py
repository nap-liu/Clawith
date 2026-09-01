"""Compactor futility floor + real savings accounting.

Locks in the fix for the "re-compacts every round" burn: when a session's
prompt is dominated by non-compressible content (tool schemas / system
prompt), the selected span carries almost no token mass — compacting it
cannot lower the trigger ratio, so _do_compact must skip BEFORE the
summary LLM call instead of blocking the turn for tens of seconds each
round. Also locks the savings figure to what actually leaves the prompt
(span + superseded prior summary - new summary), not trigger_prompt_tokens.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.services.llm import compactor
from app.services.llm import tool_output_store
from app.services.llm.compactor import _do_compact
from app.services.storage import LocalStorageBackend
from compactor_futility_support import (
    _FakeDB,
    _FakeModel,
    _Row,
    _configure_local_storage,
    _fake_session_factory,
    _rows_with_span,
)


@pytest.mark.asyncio
async def test_tiny_span_still_uses_required_compaction(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=_rows_with_span(50)))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))

    summarize = AsyncMock(return_value=("## Summary of earlier conversation\n- facts", None))
    monkeypatch.setattr(compactor, "_summarize_via_llm", summarize)
    monkeypatch.setattr(compactor, "validate_summary", lambda **_kw: (True, None, 1.0))

    result = await _do_compact(
        agent_id=uuid.uuid4(),
        session_id="s1",
        conversation_id="s1",
        model=_FakeModel(),
        trigger_prompt_tokens=68000,
        trigger_ratio=2.1,
        trigger_reason="post_round",
    )

    assert result.triggered is True
    assert db.committed is True
    summarize.assert_awaited_once()


@pytest.mark.asyncio
async def test_savings_reflect_span_mass_not_trigger_prompt(monkeypatch):
    db = _FakeDB()
    rows = _rows_with_span(5000)  # span ≈ 6×5000 chars ≈ 12000 est tokens — above floor
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    summary_mock = AsyncMock(
        return_value=("## Summary of earlier conversation\n- facts", {"completion_tokens": 100})
    )
    monkeypatch.setattr(compactor, "_summarize_via_llm", summary_mock)
    monkeypatch.setattr(compactor, "validate_summary", lambda **_kw: (True, None, 1.0))

    result = await _do_compact(
        agent_id=uuid.uuid4(),
        session_id="s2",
        conversation_id="s2",
        model=_FakeModel(),
        trigger_prompt_tokens=200_000,
        trigger_ratio=6.25,
        trigger_reason="post_round",
    )

    assert result.triggered is True
    assert db.committed is True
    assert summary_mock.await_args.kwargs["span_text"]
    assert "tokens" not in result.progress_notice


@pytest.mark.asyncio
async def test_large_span_still_compacts(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=_rows_with_span(4000)))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=("old summary " * 30, 1, uuid.uuid4())))
    monkeypatch.setattr(
        compactor, "_summarize_via_llm",
        AsyncMock(return_value=("## Summary of earlier conversation\n- facts", {"completion_tokens": 50})),
    )
    monkeypatch.setattr(compactor, "validate_summary", lambda **_kw: (True, None, 1.0))

    result = await _do_compact(
        agent_id=uuid.uuid4(),
        session_id="s3",
        conversation_id="s3",
        model=_FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="post_round",
    )

    assert result.triggered is True
    assert result.epoch == 2


@pytest.mark.asyncio
async def test_compaction_applies_after_mechanically_preserving_missing_identifiers(monkeypatch):
    db = _FakeDB()
    opaque_id = "5baa7373-b5c5-9eb4-8d10-d81aef980b1c"
    rows = _rows_with_span(4000)
    rows[0].content = f"Use /new, dataset {opaque_id}. " + rows[0].content
    summary = (
        "## Summary of earlier conversation\n\n"
        "### Key facts\n"
        "- The earlier conversation discussed a dataset and recovery procedure.\n"
        "- The user expects the agent to continue from a compact summary.\n\n"
        "### Open items\n"
        "- Continue the pending work after compaction is complete.\n"
    )
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(summary, {"completion_tokens": 100})),
    )

    result = await _do_compact(
        agent_id=uuid.uuid4(),
        session_id="s4",
        conversation_id="s4",
        model=_FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="pre_flight",
    )

    assert result.triggered is True
    assert db.committed is True
    assert db.added[0].summary_validation_passed is True
    assert "/new" in db.added[0].summary_text
    assert opaque_id in db.added[0].summary_text


@pytest.mark.asyncio
async def test_identifiers_in_middle_of_prefiltered_assistant_body_are_preserved(
    monkeypatch,
    tmp_path,
):
    db = _FakeDB()
    rows = _rows_with_span(4000)
    agent_id = uuid.uuid4()
    opaque_id = "123e4567-e89b-12d3-a456-426614174000"
    critical_path = "/workspace/critical/release-plan.md"
    rows[1].content = (
        "assistant-head-"
        + ("h" * 7_000)
        + " "
        + opaque_id
        + " "
        + critical_path
        + " "
        + ("t" * 7_000)
        + "-assistant-tail"
    )
    summary = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and progress\n- Continue the compacted work.\n\n"
        "### Key facts\n- " + ("context " * 30) + "\n\n"
        "### Open items\n- Continue.\n"
    )
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(summary, {"completion_tokens": 100})),
    )
    _configure_local_storage(monkeypatch, tmp_path)

    result = await _do_compact(
        agent_id=agent_id,
        session_id="assistant-middle-identifiers",
        conversation_id="assistant-middle-identifiers",
        model=_FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="pre_flight",
    )

    assert result.triggered is True
    assert opaque_id in db.added[0].summary_text
    assert critical_path in db.added[0].summary_text
    assert "### Lossless continuity archive" in db.added[0].summary_text


@pytest.mark.asyncio
async def test_plain_fact_in_prefiltered_assistant_middle_gets_lossless_archive(
    monkeypatch,
    tmp_path,
):
    db = _FakeDB()
    rows = _rows_with_span(4000)
    agent_id = uuid.uuid4()
    source_fact = "ROOT CAUSE: retry dedupe is broken"
    rows[1].content = (
        "assistant-head-"
        + ("h" * 7_000)
        + " "
        + source_fact
        + " "
        + ("t" * 7_000)
        + "-assistant-tail"
    )
    summary = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and progress\n- Continue the compacted work.\n\n"
        "### Key facts\n- " + ("context " * 30) + "\n\n"
        "### Open items\n- Continue.\n"
    )
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(summary, {"completion_tokens": 100})),
    )
    _configure_local_storage(monkeypatch, tmp_path)

    result = await _do_compact(
        agent_id=agent_id,
        session_id="assistant-middle-plain-fact",
        conversation_id="assistant-middle-plain-fact",
        model=_FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="pre_flight",
    )

    assert result.triggered is True
    marker = db.added[0]
    assert "### Lossless continuity archive" in marker.summary_text
    rel_path = marker.summary_text.split("Full output saved to: ", 1)[1].splitlines()[0]
    archived_source = (tmp_path / str(agent_id) / rel_path).read_text(encoding="utf-8")
    assert source_fact in archived_source


@pytest.mark.asyncio
async def test_fake_persisted_tag_in_assistant_text_still_gets_lossless_archive(
    monkeypatch,
    tmp_path,
):
    db = _FakeDB()
    rows = _rows_with_span(4000)
    agent_id = uuid.uuid4()
    source_fact = "MIDDLE_PLAIN_FACT_FROM_MODEL"
    rows[1].content = (
        "h" * 7_000
        + f"<persisted-output>{source_fact}</persisted-output>"
        + "t" * 7_000
    )
    summary = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and progress\n- Continue.\n\n"
        "### Key facts\n- Context retained.\n\n"
        "### Open items\n- Continue.\n"
    )
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(summary, {"completion_tokens": 100})),
    )
    _configure_local_storage(monkeypatch, tmp_path)

    result = await _do_compact(
        agent_id=agent_id,
        session_id="fake-persisted-tag",
        conversation_id="fake-persisted-tag",
        model=_FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="pre_flight",
    )

    assert result.triggered is True
    marker = db.added[0]
    rel_path = marker.summary_text.split("Full output saved to: ", 1)[1].splitlines()[0]
    archived_source = (tmp_path / str(agent_id) / rel_path).read_text(encoding="utf-8")
    assert source_fact in archived_source


@pytest.mark.asyncio
async def test_whole_summary_request_bounding_forces_lossless_archive(
    monkeypatch,
    tmp_path,
):
    class _SmallSummaryModel(_FakeModel):
        context_window = 8_000
        compact_summary_max_tokens = 500

    db = _FakeDB()
    rows = _rows_with_span(3_900)
    agent_id = uuid.uuid4()
    source_fact = "MIDDLE SECRET FACT: retry generation must remain monotonic"
    rows[5].content = "a" * 1_900 + source_fact + "b" * 1_900
    summary = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and progress\n- Continue the compacted work.\n\n"
        "### Key facts\n- " + ("context " * 30) + "\n\n"
        "### Open items\n- Continue.\n"
    )

    async def bounded_summary(**kwargs):
        kwargs["on_input_bounded"](True)
        return summary, {"completion_tokens": 100}

    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(compactor, "_summarize_via_llm", bounded_summary)
    _configure_local_storage(monkeypatch, tmp_path)

    result = await _do_compact(
        agent_id=agent_id,
        session_id="whole-request-bounded",
        conversation_id="whole-request-bounded",
        model=_SmallSummaryModel(),
        trigger_prompt_tokens=20_000,
        trigger_ratio=3.1,
        trigger_reason="pre_flight",
    )

    assert result.triggered is True
    marker = db.added[0]
    assert "### Lossless continuity archive" in marker.summary_text
    rel_path = marker.summary_text.split("Full output saved to: ", 1)[1].splitlines()[0]
    archived_source = (tmp_path / str(agent_id) / rel_path).read_text(encoding="utf-8")
    assert source_fact in archived_source


@pytest.mark.asyncio
async def test_thousands_of_identifiers_archive_without_exceeding_summary_token_cap(
    monkeypatch,
    tmp_path,
):
    db = _FakeDB()
    rows = _rows_with_span(4000)
    agent_id = uuid.uuid4()
    identifiers = [str(uuid.uuid4()) for _ in range(5_000)]
    rows[1].content = " ".join(identifiers)
    summary = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and progress\n- Continue the compacted work.\n\n"
        "### Key facts\n- " + ("context " * 30) + "\n\n"
        "### Open items\n- Continue.\n"
    )
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(summary, {"completion_tokens": 100})),
    )
    _configure_local_storage(monkeypatch, tmp_path)

    result = await _do_compact(
        agent_id=agent_id,
        session_id="identifier-heavy-summary",
        conversation_id="identifier-heavy-summary",
        model=_FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="pre_flight",
    )

    assert result.triggered is True
    marker = db.added[0]
    # No provider has tokenized the exact persisted summary yet.  Unknown is
    # represented as NULL instead of a character-derived token claim.
    assert marker.summary_tokens is None
    assert "### Lossless continuity archive" in marker.summary_text
    rel_path = marker.summary_text.split("Full output saved to: ", 1)[1].splitlines()[0]
    archived_source = (tmp_path / str(agent_id) / rel_path).read_text(encoding="utf-8")
    assert identifiers[0] in archived_source
    assert identifiers[len(identifiers) // 2] in archived_source
    assert identifiers[-1] in archived_source


@pytest.mark.asyncio
async def test_model_summary_failures_use_lossless_archived_fallback(monkeypatch, tmp_path):
    db = _FakeDB()
    rows = _rows_with_span(4000)
    agent_id = uuid.uuid4()
    source_fact = "MIDDLE-FACT-MUST-REMAIN-7d78878e"
    rows[2].content = rows[2].content[:2000] + source_fact + rows[2].content[2000:]
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    _configure_local_storage(monkeypatch, tmp_path)
    summarize = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(compactor, "_summarize_via_llm", summarize)

    result = await _do_compact(
        agent_id=agent_id,
        session_id="summary-fallback",
        conversation_id="summary-fallback",
        model=_FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="pre_flight",
    )

    assert result.triggered is True
    assert summarize.await_count == 1
    assert db.committed is True
    marker = db.added[0]
    assert marker.summary_validation_passed is True
    assert "summary_llm_error:RuntimeError" in marker.validation_failure_reason
    assert "repair_llm_error" not in marker.validation_failure_reason
    assert "### Current objective and progress" in marker.summary_text
    assert "### Open items" in marker.summary_text
    assert "<persisted-output>" in marker.summary_text
    rel_path = marker.summary_text.split("Full output saved to: ", 1)[1].splitlines()[0]
    archived_source = (tmp_path / str(agent_id) / rel_path).read_text(encoding="utf-8")
    assert source_fact in archived_source


@pytest.mark.asyncio
async def test_successful_summary_still_archives_aged_out_incomplete_turn(
    monkeypatch,
    tmp_path,
):
    db = _FakeDB()
    agent_id = uuid.uuid4()
    abandoned_evidence = "ABANDONED-RUN-EVIDENCE-" + ("x" * 9_000)
    rows = [
        _Row("user", "old interrupted request", offset=0),
        _Row("tool_call", "{" + abandoned_evidence, offset=1),
    ]
    for turn in range(5):
        rows.extend(
            [
                _Row("user", f"later request {turn}", offset=2 + turn * 2),
                _Row("assistant", f"later answer {turn}", offset=3 + turn * 2),
            ]
        )
    summary = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and progress\n- Continue safely.\n\n"
        "### Key facts\n- Older work was interrupted.\n\n"
        "### Open items\n- Continue.\n"
    )
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(
        compactor,
        "_load_active_marker",
        AsyncMock(return_value=(None, None, None)),
    )
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(summary, {"completion_tokens": 100})),
    )
    monkeypatch.setattr(compactor, "validate_summary", lambda **_kw: (True, None, 1.0))
    _configure_local_storage(monkeypatch, tmp_path)

    result = await _do_compact(
        agent_id=agent_id,
        session_id="aged-out-incomplete",
        conversation_id="aged-out-incomplete",
        model=_FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="pre_flight",
    )

    assert result.triggered is True
    marker = db.added[0]
    assert "lossless_archive:abandoned_incomplete_turn" in marker.validation_failure_reason
    rel_path = marker.summary_text.split("Full output saved to: ", 1)[1].splitlines()[0]
    archived_source = (tmp_path / str(agent_id) / rel_path).read_text(encoding="utf-8")
    assert abandoned_evidence in archived_source


@pytest.mark.asyncio
async def test_invalid_model_summary_and_invalid_repair_still_apply_lossless_fallback(
    monkeypatch,
    tmp_path,
):
    """Successful provider calls with unusable text cannot disable compaction."""
    db = _FakeDB()
    rows = _rows_with_span(4000)
    agent_id = uuid.uuid4()
    sender_id = uuid.uuid4()
    source_fact = "VALIDATION-FALLBACK-FACT-3f586db1"
    rows[2].content = rows[2].content[:2000] + source_fact + rows[2].content[2000:]
    rows[2].sender_user_id = sender_id
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(
        compactor,
        "_load_summary_sender_attribution",
        AsyncMock(return_value=(True, {sender_id: "Alice & Bob"})),
    )
    _configure_local_storage(monkeypatch, tmp_path)
    summarize = AsyncMock(
        side_effect=[
            ("The provider returned text without the required sections.", {"completion_tokens": 20}),
            ("The repair call also returned invalid text.", {"completion_tokens": 20}),
        ]
    )
    monkeypatch.setattr(compactor, "_summarize_via_llm", summarize)

    result = await _do_compact(
        agent_id=agent_id,
        session_id="invalid-summary-fallback",
        conversation_id="invalid-summary-fallback",
        model=_FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="pre_flight",
    )

    assert result.triggered is True
    assert summarize.await_count == 2
    sender_tag = f'<sender id="{sender_id}">Alice &amp; Bob</sender>'
    assert sender_tag in summarize.await_args_list[0].kwargs["span_text"]
    assert sender_tag in summarize.await_args_list[1].kwargs["span_text"]
    assert db.committed is True
    marker = db.added[0]
    assert marker.summary_validation_passed is True
    assert len(marker.summary_text) <= compactor.DETERMINISTIC_SUMMARY_MAX_CHARS
    assert "validation_failed:" in marker.validation_failure_reason
    assert "repair_validation_failed:" in marker.validation_failure_reason
    assert "### Current objective and progress" in marker.summary_text
    assert "### Open items" in marker.summary_text
    rel_path = marker.summary_text.split("Full output saved to: ", 1)[1].splitlines()[0]
    archived_source = (tmp_path / str(agent_id) / rel_path).read_text(encoding="utf-8")
    assert source_fact in archived_source
    assert sender_tag in archived_source


@pytest.mark.asyncio
async def test_valid_summary_archives_unverifiable_middle_of_huge_user_input(
    monkeypatch,
    tmp_path,
):
    db = _FakeDB()
    rows = _rows_with_span(4000)
    agent_id = uuid.uuid4()
    middle_requirement = "MIDDLE-ONLY-REQUIREMENT-DO-NOT-LOSE-684d83"
    rows[0].content = "HEAD-" + ("h" * 7_000) + middle_requirement + ("t" * 7_000) + "-TAIL"
    model_summary = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and progress\n"
        "- Provider draft objective.\n\n"
        "### Goal ledger\n"
        "- Active: Continue the requested work.\n"
        "- Achieved: Earlier context has been reviewed.\n"
        "- Not achieved / blocked: The requested work is still pending.\n\n"
        "### Related task handoff\n"
        "- Task/project/focus item: huge-objective.\n"
        "- Owner: current agent.\n"
        "- Status: active.\n"
        "- Completed evidence: provider draft exists.\n"
        "- Remaining steps: continue the request.\n"
        "- Blockers: none evidenced.\n"
        "- Next action: resume.\n"
        "- Archive/file paths: archived source below.\n\n"
        "### Key facts\n"
        "- The conversation remains active after compaction. " + ("context " * 20) + "\n\n"
        "### Open items\n"
        "- Continue the requested work.\n"
    )
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(model_summary, {"completion_tokens": 100})),
    )
    _configure_local_storage(monkeypatch, tmp_path)
    original_materialize = tool_output_store.materialize_tool_output_strict

    async def _assert_connection_released_before_archive(*args, **kwargs):
        assert db.commit_count == 1
        assert db.execute_count == 0
        return await original_materialize(*args, **kwargs)

    monkeypatch.setattr(
        tool_output_store,
        "materialize_tool_output_strict",
        _assert_connection_released_before_archive,
    )

    result = await _do_compact(
        agent_id=agent_id,
        session_id="huge-objective",
        conversation_id="huge-objective",
        model=_FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="pre_flight",
    )

    assert result.triggered is True
    assert db.commit_count == 2
    marker = db.added[0]
    assert marker.validation_failure_reason is None
    assert "### Lossless continuity archive" in marker.summary_text
    assert "complete, unabridged source" in marker.summary_text
    rel_path = marker.summary_text.split("Full output saved to: ", 1)[1].splitlines()[0]
    archived_source = (tmp_path / str(agent_id) / rel_path).read_text(encoding="utf-8")
    assert middle_requirement in archived_source


@pytest.mark.asyncio
async def test_stale_boundary_preserves_stable_archive_for_safe_retry(monkeypatch, tmp_path):
    db = _FakeDB()
    rows = _rows_with_span(4000)
    rows[0].content = "head" * 3_000 + "middle" + "tail" * 3_000
    changed_rows = list(rows)
    changed_rows[0] = _Row("user", "changed" * 2_000, offset=0)
    agent_id = uuid.uuid4()
    summary = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and progress\n- placeholder\n\n"
        "### Key facts\n- " + ("context " * 30) + "\n\n"
        "### Open items\n- continue\n"
    )
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(
        compactor,
        "_load_active_rows",
        AsyncMock(side_effect=[rows, changed_rows]),
    )
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(summary, {"completion_tokens": 100})),
    )
    _configure_local_storage(monkeypatch, tmp_path)

    result = await _do_compact(
        agent_id=agent_id,
        session_id="stale-archive",
        conversation_id="stale-archive",
        model=_FakeModel(),
        trigger_prompt_tokens=100_000,
        trigger_ratio=3.1,
        trigger_reason="pre_flight",
    )

    assert result.triggered is False
    assert result.skipped_reason == "compactable_span_changed_during_compaction"
    assert db.rollback_count == 1
    archive_dir = tmp_path / str(agent_id) / ".tool_results" / "stale-archive"
    assert len(list(archive_dir.iterdir())) == 1


@pytest.mark.asyncio
async def test_cancel_while_acquiring_advisory_lock_preserves_stable_archive(
    monkeypatch,
    tmp_path,
):
    lock_wait_started = asyncio.Event()

    class _BlockingLockDB(_FakeDB):
        async def execute(self, *_a, **_k):
            self.execute_count += 1
            lock_wait_started.set()
            await asyncio.Event().wait()

    db = _BlockingLockDB()
    rows = _rows_with_span(4000)
    rows[1].content = "head" + ("x" * 12_000) + "plain middle fact" + ("y" * 12_000)
    agent_id = uuid.uuid4()
    summary = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and progress\n- placeholder\n\n"
        "### Key facts\n- " + ("context " * 30) + "\n\n"
        "### Open items\n- continue\n"
    )
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(summary, {"completion_tokens": 100})),
    )
    _configure_local_storage(monkeypatch, tmp_path)

    task = asyncio.create_task(
        _do_compact(
            agent_id=agent_id,
            session_id="cancel-archive",
            conversation_id="cancel-archive",
            model=_FakeModel(),
            trigger_prompt_tokens=100_000,
            trigger_ratio=3.1,
            trigger_reason="pre_flight",
        )
    )
    await asyncio.wait_for(lock_wait_started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert db.added == []
    archive_dir = tmp_path / str(agent_id) / ".tool_results" / "cancel-archive"
    assert len(list(archive_dir.iterdir())) == 1


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_without_deleting_stable_archive(monkeypatch, tmp_path):
    class _CommitFailDB(_FakeDB):
        async def commit(self):
            self.commit_count += 1
            self.committed = True
            if self.commit_count == 2:
                raise RuntimeError("injected final commit failure")

    db = _CommitFailDB()
    rows = _rows_with_span(4000)
    rows[0].content = "head" * 3_000 + "middle" + "tail" * 3_000
    agent_id = uuid.uuid4()
    summary = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and progress\n- placeholder\n\n"
        "### Key facts\n- " + ("context " * 30) + "\n\n"
        "### Open items\n- continue\n"
    )
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(
        compactor,
        "_summarize_via_llm",
        AsyncMock(return_value=(summary, {"completion_tokens": 100})),
    )
    _configure_local_storage(monkeypatch, tmp_path)

    with pytest.raises(RuntimeError, match="injected final commit failure"):
        await _do_compact(
            agent_id=agent_id,
            session_id="commit-fail-archive",
            conversation_id="commit-fail-archive",
            model=_FakeModel(),
            trigger_prompt_tokens=100_000,
            trigger_ratio=3.1,
            trigger_reason="pre_flight",
        )

    assert db.rollback_count == 1
    archive_dir = tmp_path / str(agent_id) / ".tool_results" / "commit-fail-archive"
    assert len(list(archive_dir.iterdir())) == 1
