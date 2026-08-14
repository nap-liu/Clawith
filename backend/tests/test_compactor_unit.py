"""Unit tests for compactor.py — pure functions only (no DB / LLM).

Covers:
- should_compact: trigger decision against post-round actual / pre-flight
- select_compaction_span: round + tool-pair boundary alignment
- prefilter_message_content: envelope and large-body trimming
- validate_summary: length / structure / UUID-recall gates
- estimate_prompt_tokens: char-based estimate shape
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.llm.caller import measure_dispatch
from app.services.llm.compactor import (
    UUID_RECALL_THRESHOLD,
    _summarize_via_llm,
    append_missing_identifiers,
    estimate_prompt_tokens,
    extract_preserved_identifiers,
    prefilter_message_content,
    select_compaction_span,
    serialize_span_for_summary,
    should_compact,
    validate_summary,
)


def _model(context_window=131072, ratio=0.85, keep=8, summary_max=2000):
    """A SimpleNamespace duck-typing the LLMModel surface compactor reads."""
    return SimpleNamespace(
        provider="custom",
        model="test-model",
        max_output_tokens=1,
        context_window=context_window,
        compact_trigger_ratio=ratio,
        keep_recent_turns=keep,
        compact_summary_max_tokens=summary_max,
        base_url=None,
    )


def test_summary_serialization_preserves_structured_attachment_identity():
    row = SimpleNamespace(
        role="user",
        content="stored transport envelope",
        message_meta={
            "source_channel": "web",
            "display_content": "请分析这份文件",
            "attachments": [{
                "display_name": "report.pdf",
                "path": "workspace/uploads/report.pdf",
                "kind": "file",
                "mime_type": "application/pdf",
            }],
        },
        created_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )

    serialized = serialize_span_for_summary([row])

    assert "请分析这份文件" in serialized
    assert "文件名：report.pdf" in serialized
    assert "路径：workspace/uploads/report.pdf" in serialized
    assert "base64" not in serialized


def test_summary_serialization_never_truncates_attachment_paths_in_long_user_row():
    paths = [
        f"workspace/uploads/{index}-{'x' * 850}.png"
        for index in range(10)
    ]
    row = SimpleNamespace(
        role="user",
        content="body" * 3000,
        message_meta={
            "source_channel": "web",
            "display_content": "body" * 3000,
            "attachments": [
                {
                    "display_name": f"{index}.png",
                    "path": path,
                    "kind": "image",
                    "mime_type": "image/png",
                }
                for index, path in enumerate(paths)
            ],
        },
        created_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )

    serialized = serialize_span_for_summary([row])

    assert all(path in serialized for path in paths)


def test_attachment_path_with_spaces_is_mechanically_preserved_from_summary_input():
    original = """\
### [user] @ 2026-08-14T00:00:00+00:00
[文件附件]
文件名：my report.pdf
路径：workspace/uploads/my report.pdf
"""
    summary = """\
## Summary of earlier conversation

### Key facts
- The user uploaded a report for review.
""" + ("Additional context. " * 20)

    repaired, missing = append_missing_identifiers(
        summary=summary,
        original_text=original,
    )

    assert "workspace/uploads/my report.pdf" in missing
    assert "- workspace/uploads/my report.pdf" in repaired
    passed, reason, recall = validate_summary(
        summary=repaired,
        original_text=original,
        max_tokens=2000,
    )
    assert passed is True, reason
    assert recall == 1.0


# ─── should_compact ──────────────────────────────────────────────────


class TestShouldCompact:
    def test_post_round_above_threshold_triggers(self):
        m = _model(context_window=100_000, ratio=0.85)
        fire, ratio, reason = should_compact(model=m, last_prompt_tokens=86_000, pre_flight_estimate=None)
        assert fire is True
        assert reason == "post_round"
        assert ratio == pytest.approx(0.86)

    def test_post_round_below_threshold_does_not_trigger(self):
        m = _model(context_window=100_000, ratio=0.85)
        fire, ratio, reason = should_compact(model=m, last_prompt_tokens=84_999, pre_flight_estimate=None)
        assert fire is False
        assert reason == "below_threshold"

    def test_pre_flight_safety_net_at_95_percent(self):
        m = _model(context_window=100_000, ratio=0.85)
        # pre_flight at 95% — fires even though no post_round info
        fire, ratio, reason = should_compact(model=m, last_prompt_tokens=None, pre_flight_estimate=95_000)
        assert fire is True
        assert reason == "pre_flight"

    def test_pre_flight_at_94_percent_does_not_fire(self):
        m = _model(context_window=100_000, ratio=0.85)
        # 94% < PRE_FLIGHT_TRIGGER_RATIO (0.95), and post-round not given
        fire, _, reason = should_compact(model=m, last_prompt_tokens=None, pre_flight_estimate=94_000)
        assert fire is False

    def test_post_round_takes_precedence_when_both_given(self):
        m = _model(context_window=100_000, ratio=0.85)
        # post 90% triggers; pre-flight 80% would not
        fire, _, reason = should_compact(model=m, last_prompt_tokens=90_000, pre_flight_estimate=80_000)
        assert fire is True
        assert reason == "post_round"

    def test_unconfigured_context_window_blocks_trigger(self):
        m = _model(context_window=0)
        fire, _, reason = should_compact(model=m, last_prompt_tokens=999_999, pre_flight_estimate=None)
        assert fire is False
        assert reason == "no_context_window_configured"

    def test_no_signals_means_no_trigger(self):
        m = _model()
        fire, ratio, reason = should_compact(model=m, last_prompt_tokens=None, pre_flight_estimate=None)
        assert fire is False
        assert reason == "below_threshold"
        assert ratio == 0.0


# ─── select_compaction_span ─────────────────────────────────────────


@dataclass
class _Row:
    role: str
    id: uuid.UUID = None
    created_at: datetime = None

    def __post_init__(self):
        self.id = self.id or uuid.uuid4()
        self.created_at = self.created_at or datetime.now(timezone.utc)


def _conversation(turns):
    """Build a row sequence from a turn shorthand: each char in `turns`
    is one role: 'u'=user, 'a'=assistant, 't'=tool_call.
    """
    role_map = {"u": "user", "a": "assistant", "t": "tool_call"}
    return [_Row(role=role_map[c]) for c in turns]


class TestSelectCompactionSpan:
    def test_returns_none_for_empty(self):
        assert select_compaction_span([], keep_recent_turns=8) is None

    def test_returns_none_when_too_few_user_turns(self):
        # Only 3 user turns total, keep_recent=8 → can't compact anything
        rows = _conversation("uauauauua")  # 4 user turns
        assert select_compaction_span(rows, keep_recent_turns=8) is None

    def test_basic_split_keeps_recent_turns(self):
        # 12 user turns; keep 4 means span ends before user-msg #9
        rows = []
        for _ in range(12):
            rows.append(_Row(role="user"))
            rows.append(_Row(role="assistant"))
        # Indices: 0=u 1=a 2=u 3=a ... 22=u 23=a (12 pairs = 24 rows)
        span = select_compaction_span(rows, keep_recent_turns=4)
        assert span is not None
        from_idx, to_idx = span
        assert from_idx == 0
        # The trailing 4 user turns are 8/9/10/11 (0-indexed), starting at row index 16.
        # Span ends at row 15, which is assistant of turn 7. End must be assistant.
        assert rows[to_idx].role == "assistant"

    def test_span_end_snaps_to_assistant_boundary(self):
        # A tool-heavy turn ends at its final assistant, never at tool_call.
        rows = []
        for _ in range(10):
            rows.append(_Row(role="user"))
            rows.append(_Row(role="tool_call"))
            rows.append(_Row(role="assistant"))
        span = select_compaction_span(rows, keep_recent_turns=3)
        assert span is not None
        _, to_idx = span
        # End MUST be the final assistant, not tool_call.
        assert rows[to_idx].role == "assistant"

    def test_refuses_too_short_span(self):
        # Only 5 turns, keep_recent=4 → span would be 1 row, too small
        rows = _conversation("uauauauaua")  # 5 user-assistant pairs
        # keep 4 means span = first 1 round (2 rows) = below the
        # 4-row floor in the implementation → returns None
        result = select_compaction_span(rows, keep_recent_turns=4)
        assert result is None


# ─── prefilter_message_content ──────────────────────────────────────


class TestPrefilter:
    def test_passes_short_text_unchanged(self):
        s = "short content"
        assert prefilter_message_content(s) == s

    def test_truncates_persisted_envelope_body(self):
        body_filler = "x" * 50_000  # massive body inside envelope
        s = (
            f'<persisted-output path=".tool_results/abc.json" size="50000">'
            f'{body_filler}</persisted-output>'
        )
        out = prefilter_message_content(s)
        assert "[body materialized to disk]" in out
        assert "xxxx" not in out  # the bulk body is stripped
        assert 'path=".tool_results/abc.json"' in out
        assert 'size="50000"' in out

    def test_head_tail_truncates_oversized_body(self):
        s = "A" * 6000
        out = prefilter_message_content(s)
        assert "truncated" in out
        assert len(out) < 3000  # well below original
        assert out.startswith("A" * 100)  # head preserved
        assert out.endswith("A" * 100)    # tail preserved

    def test_passthrough_for_non_string(self):
        # List content (vision messages) — pre-filter is for strings only
        x = [{"type": "text", "text": "hi"}]
        assert prefilter_message_content(x) is x


# ─── validate_summary ────────────────────────────────────────────────


_GOOD_SUMMARY = """\
## Summary of earlier conversation

### Key facts
- Project ID: 49b2c1ab12cd34ef5678901234abcdef
- File: workspace/report.md was edited

### Decisions made
- Switched to qwen3.5-plus

### Files / paths / IDs referenced
- workspace/report.md
- workspace/draft.md

### Tool calls performed
- read_file(path=workspace/report.md) → 1.2KB

### Open items
- Pending review by Alice
"""


class TestValidateSummary:
    def test_passes_well_formed_summary_with_full_recall(self):
        original = (
            "User uploaded workspace/report.md and workspace/draft.md, "
            "project id 49b2c1ab12cd34ef5678901234abcdef"
        )
        passed, fail_reason, recall = validate_summary(
            summary=_GOOD_SUMMARY,
            original_text=original,
            max_tokens=2000,
        )
        assert passed is True
        assert fail_reason is None
        assert recall >= UUID_RECALL_THRESHOLD

    def test_rejects_too_short(self):
        passed, reason, _ = validate_summary(
            summary="too short",
            original_text="anything",
            max_tokens=2000,
        )
        assert passed is False
        assert "too_short" in reason

    def test_rejects_low_id_recall(self):
        # Original has 5 paths; summary recalls 1 → 20% < 70% threshold.
        # Summary padded to clear the MIN_SUMMARY_CHARS gate so we
        # exercise the recall gate in isolation.
        original = (
            "Files referenced: workspace/a.md workspace/b.md workspace/c.md "
            "workspace/d.md workspace/e.md"
        )
        summary = """\
## Summary of earlier conversation

### Key facts
- A single file was edited during the segment: workspace/a.md
- The user asked clarifying questions about the document layout
- The agent performed several minor edits and saved the result

### Decisions made
- The agent decided to defer the rest of the edits to a later session

### Files / paths / IDs referenced
- workspace/a.md (the only one captured here — others lost)

### Tool calls performed
- read_file(path=workspace/a.md) → file contents returned

### Open items
- Continue with the remaining files in a future round
"""
        passed, reason, recall = validate_summary(
            summary=summary,
            original_text=original,
            max_tokens=2000,
        )
        assert passed is False
        assert "low_id_recall" in reason
        assert recall < UUID_RECALL_THRESHOLD

    def test_rejects_missing_section_headings(self):
        # No ## or ### headings in the body
        s = "Just a flat paragraph with no markdown structure. " * 30
        passed, reason, _ = validate_summary(
            summary=s,
            original_text="test",
            max_tokens=2000,
        )
        assert passed is False
        assert "missing_section_headings" in reason

    def test_vacuous_pass_when_original_has_no_ids(self):
        # Original is just pleasantries — no UUIDs or paths to recall
        original = "Hello there, how are you doing today?"
        passed, _, recall = validate_summary(
            summary=_GOOD_SUMMARY,
            original_text=original,
            max_tokens=2000,
        )
        assert passed is True
        assert recall == 1.0

    def test_missing_commands_and_ids_are_appended_before_validation(self):
        opaque_id = "5baa7373-b5c5-9eb4-8d10-d81aef980b1c"
        original = (
            "Use /new if recovery is needed. "
            f"Dataset {opaque_id}. "
            "Read workspace/report.md and https://example.test/report/42"
        )
        summary = """\
## Summary of earlier conversation

### Key facts
- The report workflow and recovery procedure were discussed.

### Files / paths / IDs referenced
- workspace/report.md
- https://example.test/report/42
"""

        repaired, missing = append_missing_identifiers(
            summary=summary,
            original_text=original,
        )
        passed, reason, recall = validate_summary(
            summary=repaired,
            original_text=original,
            max_tokens=2000,
        )

        assert missing == ["/new", opaque_id]
        assert "### Preserved identifiers" in repaired
        assert passed is True
        assert reason is None
        assert recall == 1.0

    def test_slash_commands_are_classified_without_path_artifacts(self):
        identifiers = extract_preserved_identifiers(
            r"Send /new, then read workspace/report.md; ignore /\nartifact."
        )

        assert "/new" in identifiers
        assert "workspace/report.md" in identifiers
        assert "/" not in identifiers
        assert not any(identifier.startswith("/\\") for identifier in identifiers)


# ─── estimate_prompt_tokens ─────────────────────────────────────────


class TestEstimatePromptTokens:
    def test_simple_string_content(self):
        msgs = [
            {"role": "system", "content": "x" * 100},
            {"role": "user", "content": "y" * 250},
        ]
        assert estimate_prompt_tokens(msgs) == 117

    def test_list_content_with_text_blocks(self):
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "a" * 50},
                {"type": "text", "text": "b" * 50},
            ]}
        ]
        assert estimate_prompt_tokens(msgs) == 34

    def test_image_blocks_get_fixed_cost(self):
        msgs = [
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "data": "..."}},
            ]}
        ]
        # 1024 placeholder chars per image, estimated at three ASCII chars/token.
        assert estimate_prompt_tokens(msgs) == 342

    def test_legacy_base64_image_markers_get_fixed_cost(self):
        small = "[image_data:data:image/jpeg;base64," + "A" * 40 + "]"
        large = "[image_data:data:image/jpeg;base64," + "A" * 400_000 + "]"

        small_estimate = estimate_prompt_tokens([{"role": "user", "content": small}])
        large_estimate = estimate_prompt_tokens([{"role": "user", "content": large}])

        assert small_estimate == 342
        assert large_estimate == small_estimate

    def test_tool_call_arguments_counted(self):
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "x", "arguments": '{"q":"' + "y" * 100 + '"}'}}
                ],
            }
        ]
        assert estimate_prompt_tokens(msgs) >= 35

    def test_cjk_characters_are_counted_conservatively(self):
        msgs = [{"role": "user", "content": "数" * 901}]
        assert estimate_prompt_tokens(msgs) == 901


@pytest.mark.asyncio
async def test_summary_provider_request_is_bounded_before_dispatch(monkeypatch):
    captured = {}

    class _Client:
        async def complete(self, messages, **_kwargs):
            captured["messages"] = messages
            return SimpleNamespace(content="summary", usage={"completion_tokens": 1})

    monkeypatch.setattr("app.services.llm.create_llm_client", lambda **_kwargs: _Client())
    monkeypatch.setattr("app.services.llm.get_model_api_key", lambda _model: "key")
    model = _model(context_window=8_000, summary_max=500)

    await _summarize_via_llm(
        span_text="历史" * 50_000,
        prior_summary=None,
        model=model,
    )

    assert measure_dispatch(
        model=model,
        messages=captured["messages"],
        tools=None,
        max_output_tokens=model.compact_summary_max_tokens,
    ).fits


@pytest.mark.asyncio
async def test_provider_hard_limit_forces_compaction_below_token_ratio(monkeypatch):
    from app.services.llm import compactor

    applied = compactor.CompactionResult(triggered=True)
    do_compact = AsyncMock(return_value=applied)
    monkeypatch.setattr(compactor, "_do_compact", do_compact)

    result = await compactor.maybe_compact(
        agent_id=uuid.uuid4(),
        conversation_id="qwen-char-limit",
        model=_model(context_window=500_000),
        pre_flight_estimate=300_000,
        force_required=True,
    )

    assert result.triggered is True
    assert result.required is True
    assert do_compact.await_args.kwargs["trigger_reason"] == "provider_hard_limit"
