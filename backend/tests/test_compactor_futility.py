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

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.services.llm import compactor
from app.services.llm.compactor import _do_compact


class _Row:
    def __init__(self, role: str, content: str):
        self.id = uuid.uuid4()
        self.role = role
        self.content = content
        self.created_at = datetime(2026, 7, 2, tzinfo=timezone.utc)


class _FakeModel:
    context_window = 32000
    compact_trigger_ratio = 0.85
    keep_recent_turns = 2
    compact_summary_max_tokens = 2000
    provider = "qwen"
    model = "qwen-test"
    temperature = 0.7


class _FakeDB:
    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def execute(self, *_a, **_k):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


def _fake_session_factory(db):
    class _Ctx:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    return lambda: _Ctx()


def _rows_with_span(span_content_chars_each: int, n_span_rows: int = 6):
    """History whose compactable span (everything before the trailing
    keep_recent_turns=2 user rounds) has n_span_rows rows of the given size."""
    span = []
    for i in range(n_span_rows):
        span.append(_Row("user" if i % 2 == 0 else "assistant", "x" * span_content_chars_each))
    trailing = [
        _Row("user", "q1"), _Row("assistant", "a1"),
        _Row("user", "q2"), _Row("assistant", "a2"),
    ]
    return span + trailing


@pytest.mark.asyncio
async def test_tiny_span_skips_before_summary_llm(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=_rows_with_span(50)))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))

    async def _must_not_be_called(**_kw):
        raise AssertionError("summary LLM must NOT be called for a futile span")

    monkeypatch.setattr(compactor, "_summarize_via_llm", _must_not_be_called)

    result = await _do_compact(
        agent_id=uuid.uuid4(),
        session_id="s1",
        conversation_id="s1",
        model=_FakeModel(),
        trigger_prompt_tokens=68000,
        trigger_ratio=2.1,
        trigger_reason="post_round",
    )

    assert result.triggered is False
    assert result.skipped_reason == "span_mass_too_small_to_matter"
    assert db.committed is False


@pytest.mark.asyncio
async def test_savings_reflect_span_mass_not_trigger_prompt(monkeypatch):
    db = _FakeDB()
    rows = _rows_with_span(5000)  # span ≈ 6×5000 chars ≈ 12000 est tokens — above floor
    monkeypatch.setattr(compactor, "async_session", _fake_session_factory(db))
    monkeypatch.setattr(compactor, "_load_active_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(compactor, "_load_active_marker", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(
        compactor, "_summarize_via_llm",
        AsyncMock(return_value=("## Summary of earlier conversation\n- facts", {"completion_tokens": 100})),
    )
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
    # savings = span_est (30000/2.5=12000) - summary (100); the old buggy
    # figure would have been ~199900 (trigger_prompt_tokens - summary).
    assert "节省约 11900 tokens" in result.progress_notice


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
