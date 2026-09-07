"""Tests for tool_output_store.

Covers the contract that downstream code relies on:
  - String <= budget is returned inline, unchanged.
  - Non-string input (vision payloads) passes through.
  - Empty output is normalized.
  - String > budget is materialized: file written, llm_view has
    <persisted-output> marker + preview + file ref.
  - One normalized budget applies to every tool.
  - Env override changes that budget globally.
  - Missing agent_id / unwritable path raises a materialization error
    (never silently drops data).
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.services.llm import tool_output_store as tos


@pytest.fixture
def agent_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def tmp_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    # Invalidate cached settings so AGENT_DATA_DIR is re-read
    from app.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _finalize(result, *, tool_name="grep", agent_id=None, session_id="sess1", tool_call_id="call_1"):
    return asyncio.run(
        tos.finalize_tool_output(
            result,
            tool_name=tool_name,
            agent_id=agent_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
    )


def _saved_path(view: str) -> str:
    return view.split("Full output saved to: ", 1)[1].splitlines()[0]


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
    assert ".tool_results/s1/grep_call_abc_" in view
    assert "60,000" in view.replace(",", ",") or "58.6 KB" in view
    assert len(view) <= tos.DEFAULT_TOOL_OUTPUT_MAX_CHARS
    assert len(view) > 31_000
    assert "TRUNCATED:" in view
    assert "within the 32,000-character limit" in view
    assert big[30_000:30_100] in view

    written = tmp_workspace / agent_id / _saved_path(view)
    assert written.exists()
    assert written.read_text() == big


def test_json_output_detected_and_suffixed(tmp_workspace, agent_id):
    payload = {"chunks": [{"content": "x" * 10_000}] * 5}
    result = json.dumps(payload)
    assert len(result) > 20_000

    view = _finalize(result, tool_name="grep", agent_id=agent_id, session_id="s1", tool_call_id="call_j")
    assert "grep_call_j_" in view
    assert ".json" in view

    written = tmp_workspace / agent_id / _saved_path(view)
    assert written.exists()
    assert json.loads(written.read_text()) == payload


@pytest.mark.parametrize("tool_name", ["grep", "execute_code", "read_file", "some_mcp_tool"])
def test_all_tools_share_normalized_budget(tmp_workspace, agent_id, tool_name):
    result = "y" * 50_000
    view = _finalize(result, tool_name=tool_name, agent_id=agent_id)

    assert tos.PERSISTED_OPEN in view
    assert len(view) <= tos.DEFAULT_TOOL_OUTPUT_MAX_CHARS
    assert (tmp_workspace / agent_id / _saved_path(view)).read_text() == result


def test_read_file_large_single_line_is_materialized(tmp_workspace, agent_id):
    # Line pagination does not protect a minified HTML/JSON file whose payload
    # lives on one huge line. The platform output layer must keep that body out
    # of LLM history while preserving every byte on disk.
    huge = "z" * 5_000_000
    view = _finalize(huge, tool_name="read_file", agent_id=agent_id, tool_call_id="c")
    assert tos.PERSISTED_OPEN in view
    assert len(view) < len(huge)
    written = tmp_workspace / agent_id / _saved_path(view)
    assert written.read_text() == huge
    assert "execute_code_aio" in view


def test_env_override_changes_default(tmp_workspace, agent_id, monkeypatch):
    monkeypatch.setenv(tos.ENV_OVERRIDE, "4096")
    s = "w" * 5_000  # Under the default but over the global override.
    view = _finalize(s, tool_name="some_mcp_tool", agent_id=agent_id, tool_call_id="c")
    assert tos.PERSISTED_OPEN in view
    assert len(view) <= 4096


def test_env_override_affects_known_tools_too(tmp_workspace, agent_id, monkeypatch):
    monkeypatch.setenv(tos.ENV_OVERRIDE, "1024")
    s = "q" * 15_000  # under grep (20k) but over env override
    view = _finalize(s, tool_name="grep", agent_id=agent_id, tool_call_id="c")
    assert tos.PERSISTED_OPEN in view
    assert len(view) <= 1024


def test_missing_agent_fails_without_replacing_full_output(tmp_workspace):
    # No agent_id means no readable path; an unrecoverable truncation must not
    # be persisted or dispatched as if it were the full result.
    big = "a" * 60_000
    with pytest.raises(tos.ToolOutputMaterializationError, match="missing agent_id"):
        _finalize(big, tool_name="grep", agent_id=None, tool_call_id="c")


def test_materialize_failure_is_strict(tmp_workspace, agent_id, monkeypatch):
    class BrokenStorage:
        def __init__(self):
            self.deleted = []

        async def exists(self, _key):
            return False

        async def write_text(self, *_args, **_kwargs):
            raise OSError("disk full")

        async def delete(self, key):
            self.deleted.append(key)

    storage = BrokenStorage()
    monkeypatch.setattr(tos, "get_storage_backend", lambda: storage)

    big = "b" * 60_000
    with pytest.raises(tos.ToolOutputMaterializationError, match="disk full"):
        _finalize(big, tool_name="grep", agent_id=agent_id, tool_call_id="c")
    assert storage.deleted == []


def test_retry_reuses_preexisting_content_addressed_object_without_rewrite_or_delete(
    tmp_workspace, agent_id, monkeypatch
):
    big = "stable" * 10_000
    initial = _finalize(big, tool_name="grep", agent_id=agent_id, tool_call_id="same")
    key = f"{agent_id}/{_saved_path(initial)}"

    class ExistingStorage:
        def __init__(self):
            self.writes = 0
            self.deletes = 0

        async def exists(self, candidate):
            return candidate == key

        async def read_text(self, candidate, **_kwargs):
            assert candidate == key
            return big

        async def write_text(self, *_args, **_kwargs):
            self.writes += 1
            raise OSError("must not rewrite stable object")

        async def delete(self, _key):
            self.deletes += 1

    storage = ExistingStorage()
    monkeypatch.setattr(tos, "get_storage_backend", lambda: storage)

    retried = _finalize(big, tool_name="grep", agent_id=agent_id, tool_call_id="same")

    assert _saved_path(retried) == _saved_path(initial)
    assert storage.writes == 0
    assert storage.deletes == 0


def test_reused_provider_call_id_never_overwrites_prior_full_output(
    tmp_workspace,
    agent_id,
):
    first = _finalize(
        "A" * 40_000,
        tool_name="grep",
        agent_id=agent_id,
        session_id="same-session",
        tool_call_id="same-call",
    )
    second = _finalize(
        "B" * 40_000,
        tool_name="grep",
        agent_id=agent_id,
        session_id="same-session",
        tool_call_id="same-call",
    )

    first_path = tmp_workspace / agent_id / _saved_path(first)
    second_path = tmp_workspace / agent_id / _saved_path(second)
    assert first_path != second_path
    assert first_path.read_text() == "A" * 40_000
    assert second_path.read_text() == "B" * 40_000


def test_materialization_uses_configured_local_storage_root(tmp_workspace, tmp_path, agent_id, monkeypatch):
    storage_root = tmp_path / "configured-storage"
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(storage_root))
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        big = "r" * 60_000
        view = _finalize(big, tool_name="grep", agent_id=agent_id, tool_call_id="root")
    finally:
        get_settings.cache_clear()

    assert tos.PERSISTED_OPEN in view
    written = storage_root / agent_id / _saved_path(view)
    assert written.read_text() == big


def test_remote_storage_materialization_uses_readable_agent_key(
    tmp_workspace,
    agent_id,
    monkeypatch,
):
    writes: list[tuple[str, str]] = []

    class RemoteStorage:
        async def exists(self, _key):
            return False

        async def write_text(self, key, content, **_kwargs):
            writes.append((key, content))

        async def delete(self, _key):
            return None

    monkeypatch.setattr(tos, "get_storage_backend", lambda: RemoteStorage())
    view = _finalize("s" * 60_000, tool_name="grep", agent_id=agent_id, tool_call_id="s3")

    assert tos.PERSISTED_OPEN in view
    saved_path = _saved_path(view)
    assert saved_path.startswith(".tool_results/sess1/grep_s3_")
    assert len(writes) == 1
    assert writes[0][0].endswith(f"{agent_id}/{saved_path}")
    assert writes[0][1] == "s" * 60_000


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


def test_materialization_sanitizes_unsafe_session_id(tmp_workspace, agent_id):
    big = "d" * 60_000
    view = _finalize(
        big,
        tool_name="grep",
        agent_id=agent_id,
        session_id="../unsafe/session",
        tool_call_id="safe-call",
    )

    expected = _saved_path(view)
    assert expected.startswith(".tool_results/unsafe_session/grep_safe-call_")
    assert (tmp_workspace / agent_id / expected).read_text() == big
