"""Summary validation unit tests for the compactor."""

from __future__ import annotations

import uuid

from app.services.llm.compactor import (
    DETERMINISTIC_SUMMARY_MAX_CHARS,
    UUID_RECALL_THRESHOLD,
    append_missing_identifiers,
    build_deterministic_summary,
    extract_preserved_identifiers,
    pin_summary_objective,
    prefilter_message_content,
    validate_summary,
)
from tests.test_compactor_unit import _GOOD_SUMMARY


class TestValidateSummary:
    def test_wrong_objective_is_rejected_until_source_evidence_is_pinned(self):
        original = "暂停发布，先修复生产幻觉"
        wrong = """\
## Summary of earlier conversation

### Current objective and progress
- 立即发布，问题已经解决。

### Goal ledger
- Active: 立即发布。
- Achieved: 问题已经解决。
- Not achieved / blocked: None stated.

### Related task handoff
- None evidenced.

### Key facts
- The conversation continued with enough diagnostic detail to pass the length gate.
- Additional neutral context keeps this deliberately malformed summary above the minimum size.

### Open items
- Publish now.
"""
        passed, reason, _ = validate_summary(
            summary=wrong,
            original_text=original,
            max_tokens=500,
        )
        assert passed is False
        assert reason == "objective_evidence_missing"

    def test_recent_user_evidence_is_content_agnostic_and_keeps_ack_predecessors(self):
        source = """\
### [user] @ 2026-08-29T00:00:00+00:00
暂停发布，先修复幻觉，不得触碰生产。

### [user] @ 2026-08-29T00:01:00+00:00
补充验证 3.5 到 3.8。

### [user] @ 2026-08-29T00:02:00+00:00
好的
"""
        pinned = pin_summary_objective(
            summary=_GOOD_SUMMARY,
            original_text=source,
            max_tokens=500,
        )
        assert "暂停发布，先修复幻觉，不得触碰生产。" in pinned
        assert "补充验证 3.5 到 3.8。" in pinned
        assert "好的" in pinned

    def test_progressive_objective_evidence_is_stable_for_forty_epochs(self):
        source = """\
### [user] @ 2026-08-29T00:00:00+00:00
CRITICAL_GOAL: pause release, fix hallucinations, never touch production.

### [user] @ 2026-08-29T00:01:00+00:00
continue

### [user] @ 2026-08-29T00:02:00+00:00
okay
"""
        summary = pin_summary_objective(
            summary=_GOOD_SUMMARY,
            original_text=source,
            max_tokens=200,
        )
        for epoch in range(40):
            new_span = f"""\
### [user] @ 2026-08-30T00:00:00+00:00
continue-{epoch}

### [user] @ 2026-08-30T00:01:00+00:00
status-{epoch}

### [user] @ 2026-08-30T00:02:00+00:00
okay-{epoch}
"""
            summary = pin_summary_objective(
                summary=_GOOD_SUMMARY,
                original_text=summary + "\n\n" + new_span,
                max_tokens=200,
            )
            assert "CRITICAL_GOAL" in summary
            assert "P: P:" not in summary

    def test_passes_well_formed_summary_with_full_recall(self):
        original = (
            "User uploaded workspace/report.md and workspace/draft.md, "
            "project id 49b2c1ab12cd34ef5678901234abcdef"
        )
        passed, fail_reason, recall = validate_summary(
            summary=pin_summary_objective(summary=_GOOD_SUMMARY, original_text=original),
            original_text=original,
            max_tokens=2000,
        )
        assert passed is True
        assert fail_reason is None
        assert recall >= UUID_RECALL_THRESHOLD

    def test_rejects_low_id_recall(self):
        # Original has 5 paths; summary recalls 1 → 20% < 70% threshold.
        original = (
            "Files referenced: workspace/a.md workspace/b.md workspace/c.md "
            "workspace/d.md workspace/e.md"
        )
        summary = """\
## Summary of earlier conversation

### Current objective and progress
- Continue editing the remaining files.

### Goal ledger
- Active: Continue editing.
- Achieved: workspace/a.md was edited.
- Not achieved / blocked: Remaining files are incomplete.

### Related task handoff
- Task/project/focus item: edit remaining files.
- Owner: current agent.
- Status: active.
- Completed evidence: workspace/a.md was edited.
- Remaining steps: edit the other four files.
- Blockers: none evidenced.
- Next action: read the remaining files.
- Archive/file paths: workspace/a.md.

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
            summary=pin_summary_objective(summary=_GOOD_SUMMARY, original_text=original),
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

### Current objective and progress
- Complete the report workflow and retain the recovery procedure.

### Goal ledger
- Active: Complete the report workflow.
- Achieved: Recovery procedure captured.
- Not achieved / blocked: Report workflow remains incomplete.

### Related task handoff
- Task/project/focus item: report workflow.
- Owner: current agent.
- Status: active.
- Completed evidence: recovery procedure captured.
- Remaining steps: finish the report workflow.
- Blockers: none evidenced.
- Next action: continue with the preserved IDs.
- Archive/file paths: workspace/report.md.

### Key facts
- The report workflow and recovery procedure were discussed.

### Files / paths / IDs referenced
- workspace/report.md
- https://example.test/report/42

### Open items
- Continue the report workflow.
"""

        repaired, missing = append_missing_identifiers(
            summary=summary,
            original_text=original,
        )
        passed, reason, recall = validate_summary(
            summary=pin_summary_objective(summary=repaired, original_text=original),
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

    def test_persisted_output_path_survives_prefilter_and_identifier_recall(self):
        original = (
            "<persisted-output>\n"
            "TRUNCATED: original output contains 180,000 characters.\n"
            "Full output saved to: .tool_results/session/report_call.txt\n\n"
            + ("bulk body\n" * 1000)
            + "</persisted-output>"
        )

        filtered = prefilter_message_content(original, allow_materialized_elision=True)
        identifiers = extract_preserved_identifiers(filtered)

        assert ".tool_results/session/report_call.txt" in filtered
        assert ".tool_results/session/report_call.txt" in identifiers
        assert len(filtered) < len(original)

    def test_identifier_heavy_summary_has_no_summary_length_ceiling(self):
        identifiers = " ".join(str(uuid.uuid4()) for _ in range(1_000))
        summary = f"{_GOOD_SUMMARY}\n\n### Preserved identifiers\n{identifiers}"
        summary = pin_summary_objective(summary=summary, original_text=identifiers)

        passed, reason, _ = validate_summary(
            summary=summary,
            original_text=identifiers,
            max_tokens=2_000,
        )

        assert len(summary) > 24_000
        assert passed is True, reason

    def test_deterministic_fallback_preserves_goal_sections_and_identifiers(self):
        original = (
            "User objective: finish workspace/report.md before Friday.\n"
            "Open item: validate dataset 5baa7373-b5c5-9eb4-8d10-d81aef980b1c.\n"
            + "Evidence line.\n" * 100
        )
        summary = build_deterministic_summary(source_text=original, max_tokens=500)
        passed, reason, recall = validate_summary(
            summary=summary,
            original_text=original,
            max_tokens=500,
        )
        assert passed is True, reason
        assert "### Current objective and progress" in summary
        assert "### Open items" in summary
        assert "workspace/report.md" in summary
        assert recall == 1.0

    def test_deterministic_fallback_is_bounded_with_unbounded_identifier_input(self):
        source = "\n".join(
            f"Open item {index}: workspace/reports/{index}/result.json "
            f"{uuid.uuid4()}"
            for index in range(5_000)
        )
        archive = (
            "<persisted-output>\n"
            "Full output saved to: .tool_results/session/compaction-archive.txt\n"
            "</persisted-output>"
        )
        max_tokens = 200

        summary = build_deterministic_summary(
            source_text=source,
            max_tokens=max_tokens,
            archive_envelope=archive,
        )

        assert len(summary) <= DETERMINISTIC_SUMMARY_MAX_CHARS
        assert "### Current objective and progress" in summary
        assert "### Open items" in summary
        assert ".tool_results/session/compaction-archive.txt" in summary
        passed, reason, recall = validate_summary(
            summary=summary,
            original_text=prefilter_message_content(archive),
            max_tokens=max_tokens,
            objective_text=source,
            objective_evidence_max_chars=800,
            objective_archived=True,
        )
        assert passed is True, reason
        assert recall == 1.0
