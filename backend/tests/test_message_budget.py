"""Tests for message-level tool-output budget enforcement (P2).

P0 materializes a single tool result when it exceeds the per-tool
budget. P2 covers the orthogonal case: a single round produces several
in-budget tool results whose SUM exceeds the message-level cap.

The enforcer:
  * scans ALL tool messages for total size (global ceiling);
  * force-materializes the LARGEST FRESH inline tool message first;
  * never touches historical messages (append-only invariant);
  * skips tool messages with vision list content;
  * leaves already-materialized ``<persisted-output>`` messages alone.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

import pytest

from app.services.llm import tool_output_store as tos
from app.services.llm.client import LLMMessage
from app.services.llm.tool_output_store import (
    PERSISTED_OPEN,
    enforce_message_budget,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def agent_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def tmp_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _assistant(tool_calls: list[dict]) -> LLMMessage:
    return LLMMessage(role="assistant", content=None, tool_calls=tool_calls)


def _tool(tool_call_id: str, content) -> LLMMessage:
    return LLMMessage(role="tool", tool_call_id=tool_call_id, content=content)


def _tc(id_: str, name: str) -> dict:
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": "{}"}}


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_under_budget_noop(tmp_workspace, agent_id):
    """3 tool messages summing to 60k with a 120k cap → no mutation."""
    # Historical turn: 1 tool message of 20k.
    hist_assistant = _assistant([_tc("hist", "grep")])
    hist_tool = _tool("hist", "h" * 20_000)
    # Fresh turn: 2 tool messages of 20k each.
    fresh_assistant = _assistant([_tc("f1", "grep"), _tc("f2", "grep")])
    fresh_tool_1 = _tool("f1", "a" * 20_000)
    fresh_tool_2 = _tool("f2", "b" * 20_000)

    api_messages = [
        LLMMessage(role="user", content="hi"),
        hist_assistant,
        hist_tool,
        fresh_assistant,
        fresh_tool_1,
        fresh_tool_2,
    ]
    fresh_start = 3

    # Snapshot identities to confirm no replacement.
    identities_before = [id(m) for m in api_messages]

    enforce_message_budget(
        api_messages,
        fresh_start_idx=fresh_start,
        agent_id=agent_id,
        session_id="sess-noop",
    )

    identities_after = [id(m) for m in api_messages]
    assert identities_before == identities_after
    # Content unchanged.
    assert api_messages[4].content == "a" * 20_000
    assert api_messages[5].content == "b" * 20_000


def test_over_budget_picks_largest_fresh(tmp_workspace, agent_id):
    """Historical 50k stays raw; fresh 45k + 40k get materialized to fit
    under the 120k cap while the fresh 30k stays inline."""
    hist_assistant = _assistant([_tc("h1", "grep")])
    hist_tool = _tool("h1", "H" * 50_000)

    fresh_assistant = _assistant([
        _tc("f1", "grep"),
        _tc("f2", "grep"),
        _tc("f3", "grep"),
    ])
    fresh_tool_big = _tool("f1", "A" * 45_000)
    fresh_tool_mid = _tool("f2", "B" * 30_000)
    fresh_tool_large = _tool("f3", "C" * 40_000)

    api_messages = [
        LLMMessage(role="user", content="q"),
        hist_assistant,
        hist_tool,
        fresh_assistant,
        fresh_tool_big,
        fresh_tool_mid,
        fresh_tool_large,
    ]
    fresh_start = 3

    historical_identity = id(api_messages[2])

    enforce_message_budget(
        api_messages,
        fresh_start_idx=fresh_start,
        agent_id=agent_id,
        session_id="sess-over",
        max_chars=120_000,
    )

    # Historical message is byte-identical — same LLMMessage instance,
    # same content.
    assert id(api_messages[2]) == historical_identity
    assert api_messages[2].content == "H" * 50_000

    # The largest two fresh messages (45k, 40k) should have been
    # force-materialized.
    fresh_big_after = api_messages[4]
    fresh_large_after = api_messages[6]
    fresh_mid_after = api_messages[5]

    assert PERSISTED_OPEN in fresh_big_after.content
    assert PERSISTED_OPEN in fresh_large_after.content
    # The 30k mid-size fresh message stays inline.
    assert fresh_mid_after.content == "B" * 30_000

    # Under cap now.
    total = sum(
        len(m.content) for m in api_messages
        if m.role == "tool" and isinstance(m.content, str)
    )
    assert total <= 120_000

    # Files actually written.
    session_dir = tmp_workspace / agent_id / ".tool_results" / "sess-over"
    files = sorted(p.name for p in session_dir.iterdir())
    # Two materialized files; tool_call_ids f1 and f3.
    assert len(files) == 2
    assert any("f1" in f for f in files)
    assert any("f3" in f for f in files)


def test_over_budget_but_fresh_already_all_materialized(
    tmp_workspace, agent_id, caplog
):
    """When every fresh candidate is already a <persisted-output> block,
    the enforcer logs a warning and stops without touching history."""
    # Historical raw tool message of 100k — alone still under cap at this
    # size, so we add more historical to push over.
    hist_assistant = _assistant([_tc("h1", "grep"), _tc("h2", "grep")])
    hist_tool_a = _tool("h1", "H" * 80_000)
    hist_tool_b = _tool("h2", "I" * 80_000)

    # Fresh: two messages, both already carrying <persisted-output>.
    already_persisted = (
        f"{PERSISTED_OPEN}\n"
        "Output too large (42 KB). Full output saved to: .tool_results/x/y.txt\n\n"
        "Preview:\nabc\n</persisted-output>"
    )
    fresh_assistant = _assistant([_tc("f1", "grep"), _tc("f2", "grep")])
    fresh_tool_1 = _tool("f1", already_persisted)
    fresh_tool_2 = _tool("f2", already_persisted)

    api_messages = [
        LLMMessage(role="user", content="q"),
        hist_assistant,
        hist_tool_a,
        hist_tool_b,
        fresh_assistant,
        fresh_tool_1,
        fresh_tool_2,
    ]
    fresh_start = 4
    identities_before = [id(m) for m in api_messages]

    with caplog.at_level(logging.WARNING, logger="app.services.llm.tool_output_store"):
        # loguru routes through logger -> caplog bridge; but we just
        # assert identities are unchanged regardless. Loguru/caplog
        # integration is brittle across environments, so we don't make
        # the log assertion the primary check.
        enforce_message_budget(
            api_messages,
            fresh_start_idx=fresh_start,
            agent_id=agent_id,
            session_id="sess-allpersisted",
        )

    identities_after = [id(m) for m in api_messages]
    assert identities_before == identities_after
    # Historical content untouched.
    assert api_messages[2].content == "H" * 80_000
    assert api_messages[3].content == "I" * 80_000


def test_tool_name_lookup_from_preceding_assistant(tmp_workspace, agent_id):
    """With a single fresh round: a(assistant with tool_calls=[c1:grep]),
    b(tool c1, content=80k). Cap 60k forces materialization and the
    written filename must contain 'grep'."""
    fresh_assistant = _assistant([_tc("c1", "grep")])
    fresh_tool = _tool("c1", "x" * 80_000)

    api_messages = [
        LLMMessage(role="user", content="q"),
        fresh_assistant,
        fresh_tool,
    ]
    fresh_start = 1

    enforce_message_budget(
        api_messages,
        fresh_start_idx=fresh_start,
        agent_id=agent_id,
        session_id="sess-name",
        max_chars=60_000,
    )

    rewritten = api_messages[2]
    assert PERSISTED_OPEN in rewritten.content

    session_dir = tmp_workspace / agent_id / ".tool_results" / "sess-name"
    files = list(session_dir.iterdir())
    assert len(files) == 1
    # Filename format: {tool_name}_{tool_call_id}.{ext}
    assert files[0].name.startswith("grep_")
    # tool_name also embedded in the rendered llm_view via the rel_path.
    assert "grep_" in rewritten.content


def test_tool_name_missing_uses_unknown_fallback(tmp_workspace, agent_id):
    """If a fresh tool message has no matching assistant (pathological
    history), we still materialize using 'unknown' as the tool name."""
    # No assistant message at all; fresh range starts at the tool msg.
    fresh_tool = _tool("orphan", "z" * 80_000)
    api_messages = [
        LLMMessage(role="user", content="q"),
        fresh_tool,
    ]
    fresh_start = 1

    enforce_message_budget(
        api_messages,
        fresh_start_idx=fresh_start,
        agent_id=agent_id,
        session_id="sess-unknown",
        max_chars=60_000,
    )

    rewritten = api_messages[1]
    assert PERSISTED_OPEN in rewritten.content
    assert "unknown_" in rewritten.content

    session_dir = tmp_workspace / agent_id / ".tool_results" / "sess-unknown"
    files = list(session_dir.iterdir())
    assert len(files) == 1
    assert files[0].name.startswith("unknown_")


def test_vision_list_content_skipped(tmp_workspace, agent_id):
    """Vision tool messages (content is a list[dict]) do NOT count
    toward the budget and are never selected for materialization."""
    vision_parts = [
        {"type": "text", "text": "screenshot OCR result"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
    fresh_assistant = _assistant([_tc("v1", "read_image"), _tc("t1", "grep")])
    fresh_vision_tool = _tool("v1", vision_parts)
    # The other fresh tool message is small; budget isn't even exceeded.
    fresh_text_tool = _tool("t1", "small" * 100)

    api_messages = [
        LLMMessage(role="user", content="q"),
        fresh_assistant,
        fresh_vision_tool,
        fresh_text_tool,
    ]
    fresh_start = 1

    identities_before = [id(m) for m in api_messages]

    enforce_message_budget(
        api_messages,
        fresh_start_idx=fresh_start,
        agent_id=agent_id,
        session_id="sess-vision",
        max_chars=120_000,
    )

    # Nothing to do; but more importantly, the vision tool msg identity
    # is untouched and its content is still a list.
    identities_after = [id(m) for m in api_messages]
    assert identities_before == identities_after
    assert isinstance(api_messages[2].content, list)
    assert api_messages[2].content == vision_parts


def test_vision_tool_does_not_block_enforcement(tmp_workspace, agent_id):
    """Vision tool messages are skipped; enforcement should still
    materialize large text tool messages in the same round."""
    vision_parts = [
        {"type": "text", "text": "vision OCR"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
    fresh_assistant = _assistant([
        _tc("v1", "read_image"),
        _tc("t1", "grep"),
        _tc("t2", "grep"),
    ])
    fresh_vision_tool = _tool("v1", vision_parts)
    fresh_text_big = _tool("t1", "A" * 80_000)
    fresh_text_mid = _tool("t2", "B" * 50_000)

    api_messages = [
        LLMMessage(role="user", content="q"),
        fresh_assistant,
        fresh_vision_tool,
        fresh_text_big,
        fresh_text_mid,
    ]
    fresh_start = 1

    enforce_message_budget(
        api_messages,
        fresh_start_idx=fresh_start,
        agent_id=agent_id,
        session_id="sess-vision-mix",
        max_chars=120_000,
    )

    # Vision msg untouched.
    assert isinstance(api_messages[2].content, list)
    # The 80k text tool got materialized (largest over-budget candidate).
    assert PERSISTED_OPEN in api_messages[3].content
    # Remaining text tool within cap.
    total_text = sum(
        len(m.content) for m in api_messages
        if m.role == "tool" and isinstance(m.content, str)
    )
    assert total_text <= 120_000
