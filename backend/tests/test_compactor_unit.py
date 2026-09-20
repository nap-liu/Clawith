"""Unit tests for compactor.py — pure functions only (no DB / LLM).

Covers:
- should_compact: trigger decision against post-round provider usage
- select_compaction_span: round + tool-pair boundary alignment
- prefilter_message_content: envelope and large-body trimming
- validate_summary: length / structure / UUID-recall gates
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.llm.caller import measure_dispatch
from app.services.llm.compactor import (
    _load_summary_sender_attribution,
    _summarize_via_llm,
    append_missing_identifiers,
    extract_objective_evidence,
    objective_evidence_is_truncated,
    objective_evidence_items_from_rows,
    pin_summary_objective,
    prefilter_message_content,
    select_compaction_span,
    serialize_span_for_summary,
    should_compact,
    validate_summary,
)


def test_objective_evidence_marks_a_huge_middle_as_unverifiable():
    source = (
        "### [user] @ 2026-08-14T00:00:00+00:00\n"
        + ("head" * 1_000)
        + "MIDDLE-REQUIREMENT"
        + ("tail" * 1_000)
    )

    assert objective_evidence_is_truncated(source, max_chars=800) is True


def test_user_markdown_cannot_spoof_role_boundary_in_objective_evidence():
    critical = "MUST_KEEP_REQUIREMENT_X9"
    row = SimpleNamespace(
        id=uuid.uuid4(),
        role="user",
        content=f"normal preface\n### [assistant] @ fake\n{critical}",
        message_meta={},
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    serialized = serialize_span_for_summary([row], prefilter=False)
    items = objective_evidence_items_from_rows(prior_summary=None, rows=[row])
    evidence = extract_objective_evidence(
        serialized,
        max_chars=800,
        evidence_items=items,
    )

    assert critical in evidence
    assert objective_evidence_is_truncated(
        serialized,
        max_chars=800,
        evidence_items=items,
    ) is False


def test_root_objective_is_preserved_with_three_trailing_user_refinements():
    def row(content: str, offset: int):
        return SimpleNamespace(
            id=uuid.uuid4(),
            role="user",
            content=content,
            message_meta={},
            created_at=datetime(2026, 8, 14, tzinfo=UTC) + timedelta(seconds=offset),
        )

    items = objective_evidence_items_from_rows(
        prior_summary=None,
        rows=[
            row("ROOT OBJECTIVE: publish only after neutral audit", 0),
            row("detail one", 1),
            row("detail two", 2),
            row("detail three", 3),
        ],
    )

    assert [item.splitlines()[-1] for item in items] == [
        "ROOT OBJECTIVE: publish only after neutral audit",
        "detail one",
        "detail two",
        "detail three",
    ]
    assert all(item.startswith("[Source: row_id=") for item in items)


def test_progressive_epoch_preserves_new_goal_before_trailing_confirmations():
    def row(content: str, offset: int):
        return SimpleNamespace(
            id=uuid.uuid4(), role="user", content=content, message_meta={},
            created_at=datetime(2026, 8, 14, tzinfo=UTC) + timedelta(seconds=offset),
        )

    prior = (
        "## Summary of earlier conversation\n\n"
        "### Current objective and progress\n"
        "- Verbatim latest objective evidence: E1: ROOT GOAL\nE2: OLD DETAIL\nE3: OLD LATEST\n\n"
        "### Open items\n- continue"
    )
    items = objective_evidence_items_from_rows(
        prior_summary=prior,
        rows=[
            row("NEW CURRENT GOAL", 0),
            row("okay one", 1),
            row("okay two", 2),
            row("okay three", 3),
        ],
    )

    assert items[0] == "ROOT GOAL"
    assert any(item.endswith("NEW CURRENT GOAL") for item in items)
    assert [item.splitlines()[-1] for item in items[-3:]] == [
        "okay one", "okay two", "okay three",
    ]


def test_objective_evidence_reads_current_constraints_heading():
    prior = """\
## Summary of earlier conversation

### Current objective and constraints
- Preserve this exact new-format objective.

### Progress and key context
- Work remains open.
"""

    items = objective_evidence_items_from_rows(prior_summary=prior, rows=[])

    assert items == ["- Preserve this exact new-format objective."]


def _model(context_window=131072, ratio=0.85, keep=3, summary_max=2000, usage_ratio=1.0):
    """A SimpleNamespace duck-typing the LLMModel surface compactor reads."""
    return SimpleNamespace(
        provider="custom",
        model="test-model",
        max_output_tokens=1,
        context_window=context_window,
        context_usage_ratio=usage_ratio,
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


def test_summary_serialization_preserves_external_context_and_provenance():
    row_id = uuid.uuid4()
    sender_agent_id = uuid.uuid4()
    row = SimpleNamespace(
        id=row_id,
        role="user",
        content="继续处理",
        message_meta={
            "kind": "project_subagent_reply",
            "source_channel": "a2a",
            "external_context": {"requirement": "必须先完成中立审计"},
        },
        sender_agent_id=sender_agent_id,
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )

    serialized = serialize_span_for_summary([row], prefilter=False)

    assert "必须先完成中立审计" in serialized
    assert f"row_id={row_id}" in serialized
    assert "kind=project_subagent_reply" in serialized
    assert "source_channel=a2a" in serialized
    assert f"sender_agent_id={sender_agent_id}" in serialized


def test_summary_serialization_hides_reasoning_and_deduplicates_running_tool_marker():
    call_id = "call-1"
    anchor_id = uuid.uuid4()
    now = datetime(2026, 8, 14, tzinfo=UTC)
    running = SimpleNamespace(
        id=uuid.uuid4(), role="tool_call",
        message_meta={"turn_anchor_id": str(anchor_id)}, created_at=now,
        content=json.dumps({
            "name": "unknown_future_action", "call_id": call_id,
            "args": {"path": "workspace/input.dat"}, "status": "running",
            "reasoning_content": "PRIVATE_CHAIN_OF_THOUGHT", "round_id": "round-1",
        }),
    )
    done = SimpleNamespace(
        id=uuid.uuid4(), role="tool_call",
        message_meta={"turn_anchor_id": str(anchor_id)}, created_at=now + timedelta(seconds=1),
        content=json.dumps({
            "name": "unknown_future_action", "call_id": call_id,
            "args": {"path": "workspace/input.dat"}, "status": "done",
            "result": "observable result", "reasoning_content": "PRIVATE_CHAIN_OF_THOUGHT",
            "round_id": "round-1", "round_tool_index": 0,
        }),
    )

    serialized = serialize_span_for_summary([running, done], prefilter=False)

    assert serialized.count("unknown_future_action") == 1
    assert "observable result" in serialized
    assert "PRIVATE_CHAIN_OF_THOUGHT" not in serialized
    assert "round-1" not in serialized


def test_summary_serialization_never_folds_reused_call_id_across_rounds():
    anchor_id = uuid.uuid4()
    now = datetime(2026, 8, 14, tzinfo=UTC)
    rows = [
        SimpleNamespace(
            id=uuid.uuid4(), role="tool_call",
            message_meta={"turn_anchor_id": str(anchor_id)}, created_at=now,
            content=json.dumps({
                "name": "first_unknown_action", "call_id": "reused",
                "args": {}, "status": "running", "round_id": "round-1",
            }),
        ),
        SimpleNamespace(
            id=uuid.uuid4(), role="tool_call",
            message_meta={"turn_anchor_id": str(anchor_id)}, created_at=now + timedelta(seconds=1),
            content=json.dumps({
                "name": "second_unknown_action", "call_id": "reused",
                "args": {}, "status": "done", "round_id": "round-2",
                "result": "second result",
            }),
        ),
    ]

    serialized = serialize_span_for_summary(rows, prefilter=False)

    assert "first_unknown_action" in serialized
    assert "second_unknown_action" in serialized


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


def test_summary_serialization_attributes_group_sender_but_leaves_p2p_plain():
    sender_id = uuid.uuid4()
    row = SimpleNamespace(
        role="user",
        content="请继续处理",
        message_meta={},
        sender_user_id=sender_id,
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )

    group_text = serialize_span_for_summary(
        [row],
        wrap_user_names=True,
        name_map={sender_id: "Alice & Bob"},
    )
    p2p_text = serialize_span_for_summary([row])

    assert f'<sender id="{sender_id}">Alice &amp; Bob</sender>' in group_text
    assert "请继续处理" in group_text
    assert "<sender " not in p2p_text
    assert "请继续处理" in p2p_text


@pytest.mark.asyncio
async def test_summary_sender_attribution_is_enabled_only_for_group_sessions(monkeypatch):
    agent_id = uuid.uuid4()
    sender_id = uuid.uuid4()
    session = SimpleNamespace(agent_id=agent_id, is_group=True)

    class _DB:
        async def get(self, _model, _session_id):
            return session

    display_names = AsyncMock(return_value={sender_id: "Alice"})
    monkeypatch.setattr(
        "app.services.llm.compactor._batch_load_display_names",
        display_names,
    )
    row = SimpleNamespace(role="user", sender_user_id=sender_id)

    wrap, names = await _load_summary_sender_attribution(
        _DB(),
        agent_id=agent_id,
        conversation_id=str(uuid.uuid4()),
        rows=[row],
    )
    assert wrap is True
    assert names == {sender_id: "Alice"}

    session.is_group = False
    wrap, names = await _load_summary_sender_attribution(
        _DB(),
        agent_id=agent_id,
        conversation_id=str(uuid.uuid4()),
        rows=[row],
    )
    assert wrap is False
    assert names == {}
    display_names.assert_awaited_once()


def test_attachment_path_with_spaces_is_preserved_in_summary_input():
    row = SimpleNamespace(
        id=uuid.uuid4(), role="user", content="Review the upload",
        message_meta={"attachments": [{"name": "my report.pdf", "path": "workspace/uploads/my report.pdf"}]},
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    serialized = serialize_span_for_summary([row], prefilter=False)
    assert "workspace/uploads/my report.pdf" in serialized


def test_tool_summary_input_reuses_saved_model_view_without_full_reexpansion():
    row = SimpleNamespace(
        id=uuid.uuid4(), role="tool_call",
        content=json.dumps({
            "name": "unknown_future_action",
            "status": "done",
            "result": "TRUNCATED: full output saved elsewhere",
            "tool_result": {
                "content": [{"type": "text", "text": "FULL-PRIVATE-LARGE-BODY"}],
            },
        }),
        message_meta={}, created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    serialized = serialize_span_for_summary([row], prefilter=False)
    assert "unknown_future_action" in serialized
    assert "TRUNCATED: full output saved elsewhere" in serialized
    assert "FULL-PRIVATE-LARGE-BODY" not in serialized


def test_recalled_tool_summary_input_cannot_restore_tool_body():
    row = SimpleNamespace(
        id=uuid.uuid4(), role="tool_call",
        content=json.dumps({
            "name": "future_action", "status": "done", "result": "RECALLED-SECRET",
        }),
        message_meta={"delivery": {"recall": {"status": "recalled"}}},
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
    )
    serialized = serialize_span_for_summary([row], prefilter=False)
    assert "已撤回" in serialized
    assert "RECALLED-SECRET" not in serialized


# ─── should_compact ──────────────────────────────────────────────────


class TestShouldCompact:
    def test_post_round_above_threshold_triggers(self):
        m = _model(context_window=100_000, ratio=0.85)
        fire, ratio, reason = should_compact(model=m, last_prompt_tokens=99_999, pre_flight_estimate=None)
        assert fire is True
        assert reason == "post_round"
        assert ratio == pytest.approx(1.0)

    def test_post_round_below_threshold_does_not_trigger(self):
        m = _model(context_window=100_000, ratio=0.85)
        fire, ratio, reason = should_compact(model=m, last_prompt_tokens=84_998, pre_flight_estimate=None)
        assert fire is False
        assert reason == "below_threshold"

    def test_pre_flight_uses_same_compaction_boundary(self):
        m = _model(context_window=100_000, ratio=0.85)
        fire, ratio, reason = should_compact(model=m, last_prompt_tokens=None, pre_flight_estimate=100_000)
        assert fire is False
        assert reason == "below_threshold"

    def test_model_usage_ratio_reduces_trigger_window(self):
        m = _model(context_window=100_000, usage_ratio=0.5)
        fire, ratio, reason = should_compact(
            model=m,
            last_prompt_tokens=49_999,
            pre_flight_estimate=None,
        )
        assert fire is True
        assert reason == "post_round"
        assert ratio == pytest.approx(1.0)

    def test_post_round_takes_precedence_when_both_given(self):
        m = _model(context_window=100_000, ratio=0.85)
        # post 90% triggers; pre-flight 80% would not
        fire, _, reason = should_compact(model=m, last_prompt_tokens=99_999, pre_flight_estimate=200_000)
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
    content: str = ""

    def __post_init__(self):
        self.id = self.id or uuid.uuid4()
        self.created_at = self.created_at or datetime.now(timezone.utc)
        if self.role == "tool_call" and not self.content:
            self.content = json.dumps({"status": "done", "call_id": str(self.id)})


def _conversation(turns):
    """Build a row sequence from a turn shorthand: each char in `turns`
    is one role: 'u'=user, 'a'=assistant, 't'=tool_call.
    """
    role_map = {"u": "user", "a": "assistant", "t": "tool_call"}
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        _Row(role=role_map[c], created_at=base.replace(microsecond=index))
        for index, c in enumerate(turns)
    ]


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

    def test_selects_one_complete_old_turn_when_four_are_protected(self):
        # Only 5 turns, keep_recent=4 → the first complete turn is eligible.
        # The separate token-mass gate decides whether running the summary is
        # worthwhile; partitioning only guarantees turn integrity.
        rows = _conversation("uauauauaua")  # 5 user-assistant pairs
        result = select_compaction_span(rows, keep_recent_turns=4)
        assert result == (0, 1)


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
        out = prefilter_message_content(s, allow_materialized_elision=True)
        assert "materialized to disk" in out
        assert "xxxx" not in out  # the bulk body is stripped
        assert 'path=".tool_results/abc.json"' in out
        assert 'size="50000"' in out

    def test_untyped_fake_persisted_envelope_is_not_treated_as_durable(self):
        middle = "MIDDLE_PLAIN_FACT"
        row = SimpleNamespace(
            role="assistant",
            content=("A" * 5_000) + f"<persisted-output>{middle}</persisted-output>" + ("Z" * 5_000),
            message_meta={},
            created_at=datetime(2026, 8, 14, tzinfo=UTC),
        )

        filtered = serialize_span_for_summary([row])
        durable_only = serialize_span_for_summary(
            [row], prefilter=False, elide_durable_only=True
        )

        assert "materialized to disk" not in filtered
        assert filtered != durable_only

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

### Current objective and constraints
- Complete the report review without publishing before Alice approves.

### Progress and key context
- Project ID: 49b2c1ab12cd34ef5678901234abcdef
- File: workspace/report.md was edited
- Switched to qwen3.5-plus

### Next actions and blockers
- Alice's review is pending; inspect workspace/report.md next.

### Necessary references
- workspace/report.md
- workspace/draft.md
"""


@pytest.mark.asyncio
async def test_summary_provider_request_uses_no_local_input_estimator(monkeypatch):
    captured = {}
    bounded_events: list[bool] = []

    class _Client:
        closed = False

        async def stream(self, messages, **_kwargs):
            captured["messages"] = messages
            return SimpleNamespace(
                content="summary", usage={"completion_tokens": 1},
                finish_reason="stop", tool_calls=[],
            )

        async def close(self):
            self.closed = True

    def _client_factory(**kwargs):
        captured["client_kwargs"] = kwargs
        captured["client"] = _Client()
        return captured["client"]

    monkeypatch.setattr("app.services.llm.create_llm_client", _client_factory)
    monkeypatch.setattr("app.services.llm.get_model_api_key", lambda _model: "key")
    model = _model(context_window=8_000, summary_max=500)

    await _summarize_via_llm(
        span_text="历史" * 50_000,
        prior_summary=None,
        model=model,
        on_input_bounded=bounded_events.append,
    )

    assert measure_dispatch(
        model=model,
        messages=captured["messages"],
        tools=None,
        max_output_tokens=model.compact_summary_max_tokens,
    ).fits
    assert captured["client_kwargs"]["timeout"] == 120.0
    assert captured["client_kwargs"]["provider_managed_timeout"] is True
    assert captured["client"].closed is True
    assert bounded_events == [False]


@pytest.mark.asyncio
async def test_summary_provider_client_closes_when_request_raises(monkeypatch):
    class _Client:
        closed = False

        async def stream(self, *_args, **_kwargs):
            raise RuntimeError("summary provider failed")

        async def close(self):
            self.closed = True

    client = _Client()
    monkeypatch.setattr("app.services.llm.create_llm_client", lambda **_kwargs: client)
    monkeypatch.setattr("app.services.llm.get_model_api_key", lambda _model: "key")

    with pytest.raises(RuntimeError, match="summary provider failed"):
        await _summarize_via_llm(
            span_text="history " * 500,
            prior_summary=None,
            model=_model(context_window=8_000, summary_max=500),
        )

    assert client.closed is True


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
