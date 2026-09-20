"""Behavioral contracts for compaction handoff validation and degradation."""

from __future__ import annotations

import uuid

from app.services.llm.compactor import (
    DETERMINISTIC_SUMMARY_MAX_CHARS,
    build_deterministic_summary,
    extract_preserved_identifiers,
    prefilter_message_content,
    validate_summary,
)
from tests.test_compactor_unit import _GOOD_SUMMARY


class TestValidateSummary:
    def test_well_formed_handoff_is_loadable_without_identifier_gate(self):
        original = " ".join(str(uuid.uuid4()) for _ in range(100))
        passed, reason, signal = validate_summary(
            summary=_GOOD_SUMMARY, original_text=original, max_tokens=2_000,
        )
        assert passed is True
        assert reason is None
        assert signal == 1.0

    def test_structure_does_not_claim_to_prove_semantic_truth(self):
        contradictory = """\
## Summary of earlier conversation

### Current objective and constraints
- Publish immediately.

### Progress and key context
- The issue is fixed.

### Next actions and blockers
- Publish now; no blockers.

### Necessary references
- None evidenced.
"""
        passed, reason, _ = validate_summary(
            summary=contradictory,
            original_text="Do not publish; the issue is not fixed.",
            max_tokens=500,
        )
        assert passed is True
        assert reason is None

    def test_missing_or_empty_required_section_is_rejected(self):
        missing = _GOOD_SUMMARY.replace(
            "### Necessary references\n- workspace/report.md\n- workspace/draft.md\n", "",
        )
        passed, reason, _ = validate_summary(
            summary=missing, original_text="history", max_tokens=500,
        )
        assert passed is False
        assert reason == "missing_section:necessary_references"

        empty = _GOOD_SUMMARY.replace(
            "### Next actions and blockers\n- Alice's review is pending; inspect workspace/report.md next.",
            "### Next actions and blockers\n",
        )
        passed, reason, _ = validate_summary(
            summary=empty, original_text="history", max_tokens=500,
        )
        assert passed is False
        assert reason == "empty_section:next_actions_and_blockers"

    def test_flat_text_is_rejected(self):
        passed, reason, _ = validate_summary(
            summary="A flat paragraph.", original_text="history", max_tokens=500,
        )
        assert passed is False
        assert reason == "missing_section_headings"

    def test_degraded_recovery_is_bounded_and_points_to_archive(self):
        source = "\n".join(
            f"Original history {index}: workspace/reports/{index}/result.json {uuid.uuid4()}"
            for index in range(5_000)
        )
        archive = (
            "<persisted-output>\n"
            "Full output saved to: .tool_results/session/compaction-archive.txt\n"
            "</persisted-output>"
        )
        summary = build_deterministic_summary(
            source_text=source, max_tokens=200, archive_envelope=archive,
        )
        passed, reason, signal = validate_summary(
            summary=summary, original_text=archive, max_tokens=200,
        )
        assert len(summary) <= DETERMINISTIC_SUMMARY_MAX_CHARS
        assert "Degraded recovery" in summary
        assert "before continuing or repeating any external action" in summary
        assert ".tool_results/session/compaction-archive.txt" in summary
        assert passed is True, reason
        assert signal == 1.0

    def test_slash_commands_and_durable_paths_remain_parseable(self):
        identifiers = extract_preserved_identifiers(
            r"Send /new, then read workspace/report.md; ignore /\nartifact."
        )
        assert "/new" in identifiers
        assert "workspace/report.md" in identifiers
        assert "/" not in identifiers

        original = (
            "<persisted-output>\n"
            "TRUNCATED: original output contains 180,000 characters.\n"
            "Full output saved to: .tool_results/session/report_call.txt\n\n"
            + ("bulk body\n" * 1000)
            + "</persisted-output>"
        )
        filtered = prefilter_message_content(original, allow_materialized_elision=True)
        assert ".tool_results/session/report_call.txt" in filtered
        assert len(filtered) < len(original)
