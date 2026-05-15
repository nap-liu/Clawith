"""Tests for tool_output_store.

Covers the contract that downstream code relies on:
  - String <= budget is returned inline, unchanged.
  - Non-string input (vision payloads) passes through.
  - Empty output is normalized.
  - String > budget is materialized: file written, llm_view has
    <persisted-output> marker + preview + file ref.
  - Per-tool budget takes precedence over default.
  - Env override changes the default.
  - Missing agent_id / unwritable path falls back to inline shape
    (never silently drops data).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from app.services.llm import tool_output_store as tos


@pytest.fixture
def agent_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def tmp_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    # Invalidate cached settings so AGENT_DATA_DIR is re-read
    from app.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _finalize(result, *, tool_name="grep", agent_id=None, session_id="sess1", tool_call_id="call_1"):
    return tos.finalize_tool_output(
        result,
        tool_name=tool_name,
        agent_id=agent_id,
        session_id=session_id,
        tool_call_id=tool_call_id,
    )


def test_short_string_inline(tmp_workspace, agent_id):
    view = _finalize("hello", agent_id=agent_id)
    assert view == "hello"


def test_non_string_passthrough(tmp_workspace, agent_id):
    vision = [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "x"}}]
    assert _finalize(vision, agent_id=agent_id) is vision


def test_empty_output_normalized(tmp_workspace, agent_id):
    view = _finalize("", tool_name="execute_code", agent_id=agent_id)
    assert "execute_code" in view
    assert "no output" in view


def test_large_string_materialized(tmp_workspace, agent_id):
    big = "x" * 60_000
    view = _finalize(big, tool_name="grep", agent_id=agent_id, session_id="s1", tool_call_id="call_abc")

    assert tos.PERSISTED_OPEN in view
    assert tos.PERSISTED_CLOSE in view
    assert ".tool_results/s1/grep_call_abc.txt" in view
    assert "60,000" in view.replace(",", ",") or "58.6 KB" in view

    written = tmp_workspace / agent_id / ".tool_results" / "s1" / "grep_call_abc.txt"
    assert written.exists()
    assert written.read_text() == big


def test_json_output_detected_and_suffixed(tmp_workspace, agent_id):
    payload = {"chunks": [{"content": "x" * 10_000}] * 5}
    result = json.dumps(payload)
    assert len(result) > 20_000

    view = _finalize(result, tool_name="grep", agent_id=agent_id, session_id="s1", tool_call_id="call_j")
    assert "grep_call_j.json" in view

    written = tmp_workspace / agent_id / ".tool_results" / "s1" / "grep_call_j.json"
    assert written.exists()
    assert json.loads(written.read_text()) == payload


def test_per_tool_budget_beats_default(tmp_workspace, agent_id):
    # grep has budget 40_000; default is 100_000. A 50k string exceeds grep's
    # budget → materialized. Same string under a tool that uses default →
    # inline.
    size = 50_000
    s = "y" * size

    view_grep = _finalize(s, tool_name="grep", agent_id=agent_id, tool_call_id="c1")
    assert tos.PERSISTED_OPEN in view_grep

    view_default = _finalize(s, tool_name="some_mcp_tool", agent_id=agent_id, tool_call_id="c2")
    assert view_default == s


def test_read_file_is_infinite(tmp_workspace, agent_id):
    # read_file must never be materialized — it has its own pagination.
    huge = "z" * 5_000_000
    view = _finalize(huge, tool_name="read_file", agent_id=agent_id, tool_call_id="c")
    assert view == huge


def test_env_override_changes_default(tmp_workspace, agent_id, monkeypatch):
    monkeypatch.setenv(tos.ENV_OVERRIDE, "1000")
    s = "w" * 2_000  # Under default (50k) but over override (1k)
    view = _finalize(s, tool_name="some_mcp_tool", agent_id=agent_id, tool_call_id="c")
    assert tos.PERSISTED_OPEN in view


def test_env_override_does_not_affect_per_tool(tmp_workspace, agent_id, monkeypatch):
    # Per-tool budget is hardcoded; env override only kicks in for the
    # default bucket. A tool listed in the registry keeps its own budget.
    monkeypatch.setenv(tos.ENV_OVERRIDE, "10")
    s = "q" * 15_000  # under grep (20k) but over env override
    view = _finalize(s, tool_name="grep", agent_id=agent_id, tool_call_id="c")
    assert view == s  # inline, grep's 20k still applies


def test_missing_agent_falls_back_to_inline_shape(tmp_workspace):
    # No agent_id → cannot materialize → must shape inline, not drop.
    big = "a" * 60_000
    view = _finalize(big, tool_name="grep", agent_id=None, tool_call_id="c")
    assert tos.PERSISTED_OPEN not in view  # not materialized
    assert "truncated" in view  # shape_tool_result marker
    assert "a" in view


def test_materialize_failure_falls_back_to_inline_shape(tmp_workspace, agent_id, monkeypatch):
    # Force mkdir to raise — ensures we fall back to inline shape instead
    # of silently losing the tool result.
    def _boom(self, *a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", _boom)

    big = "b" * 60_000
    view = _finalize(big, tool_name="grep", agent_id=agent_id, tool_call_id="c")
    assert tos.PERSISTED_OPEN not in view
    assert "truncated" in view


def test_filename_sanitizes_unsafe_tool_call_id(tmp_workspace, agent_id):
    # tool_call_id is LLM-generated and may contain slashes etc.
    big = "c" * 60_000
    view = _finalize(
        big,
        tool_name="grep",
        agent_id=agent_id,
        session_id="s1",
        tool_call_id="call/../evil",
    )
    # The dangerous traversal parts must be sanitized away.
    assert "../" not in view
    # File must exist somewhere under the expected session dir.
    session_dir = tmp_workspace / agent_id / ".tool_results" / "s1"
    files = list(session_dir.iterdir())
    assert len(files) == 1
    assert files[0].read_text() == big


def test_budget_for_unknown_tool_uses_default():
    assert tos.budget_for("nonexistent_tool_xyz") == tos.TOOL_OUTPUT_MAX_CHARS["_default"]


def test_budget_for_known_tool_uses_registry():
    assert tos.budget_for("grep") == 40_000
    assert tos.budget_for("execute_code") == 60_000
