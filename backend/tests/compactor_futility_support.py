"""Shared data builders and doubles for compactor futility tests."""

import uuid
from datetime import datetime, timedelta, timezone

from app.services.llm import tool_output_store
from app.services.storage import LocalStorageBackend


class _Row:
    def __init__(self, role: str, content: str, *, offset: int):
        self.id = uuid.uuid4()
        self.role = role
        self.content = content
        self.message_meta = {}
        self.created_at = datetime(2026, 7, 2, tzinfo=timezone.utc) + timedelta(
            seconds=offset
        )


class _FakeModel:
    context_window = 32000
    compact_trigger_ratio = 0.85
    keep_recent_turns = 3
    compact_summary_max_tokens = 2000
    provider = "qwen"
    model = "qwen-test"
    temperature = 0.7


class _FakeDB:
    def __init__(self):
        self.added = []
        self.committed = False
        self.commit_count = 0
        self.execute_count = 0

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def execute(self, *_a, **_k):
        self.execute_count += 1
        class _Result:
            rowcount = 6

        return _Result()

    async def commit(self):
        self.committed = True
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count = getattr(self, "rollback_count", 0) + 1


def _fake_session_factory(db):
    class _Ctx:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *exc):
            return False

    return lambda: _Ctx()


def _configure_local_storage(monkeypatch, tmp_path):
    backend = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(tool_output_store, "get_storage_backend", lambda: backend)


def _rows_with_span(span_content_chars_each: int, n_span_rows: int = 6):
    """History whose compactable span (everything before the trailing
    protected three user turns) has n_span_rows rows of the given size."""
    span = []
    for i in range(n_span_rows):
        span.append(
            _Row(
                "user" if i % 2 == 0 else "assistant",
                # Keep ordinary user objectives short in tests that exercise
                # unrelated accounting. Assistant rows still provide the span
                # mass. Tests for oversized user objectives opt in explicitly.
                "x" * (80 if i % 2 == 0 else span_content_chars_each),
                offset=i,
            )
        )
    trailing = []
    for turn in range(3):
        offset = n_span_rows + turn * 2
        trailing.extend(
            [
                _Row("user", f"q{turn}", offset=offset),
                _Row("assistant", f"a{turn}", offset=offset + 1),
            ]
        )
    return span + trailing
