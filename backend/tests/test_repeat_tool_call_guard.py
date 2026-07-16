"""Unit tests for the repeated tool-call guard (pure helpers in llm.caller).

Covers the logic that prevents the provider 400 "Repetitive tool calls detected"
from crashing a turn: signature normalisation + consecutive-round streak counting.
"""
from app.services.llm.caller import (
    REPEAT_FILE_FAILURE_BREAK,
    REPEAT_FILE_FAILURE_NUDGE,
    REPEAT_TOOL_CALL_BREAK,
    REPEAT_TOOL_CALL_NUDGE,
    _tool_failure_signature,
    _tool_call_signature,
    _update_repeat_streaks,
)


def _tc(name: str, arguments):
    return {"id": "x", "function": {"name": name, "arguments": arguments}}


# ── _tool_call_signature ────────────────────────────────────────────────────

def test_signature_key_order_insensitive():
    a = _tool_call_signature(_tc("q", '{"a": 1, "b": 2}'))
    b = _tool_call_signature(_tc("q", '{"b": 2, "a": 1}'))
    assert a == b
    assert a[0] == "q"


def test_signature_whitespace_insensitive():
    a = _tool_call_signature(_tc("q", '{"a":1}'))
    b = _tool_call_signature(_tc("q", '{ "a" : 1 }'))
    assert a == b


def test_signature_different_args_differ():
    a = _tool_call_signature(_tc("q", '{"id": 1}'))
    b = _tool_call_signature(_tc("q", '{"id": 2}'))
    assert a != b


def test_signature_different_name_differ():
    a = _tool_call_signature(_tc("q", '{"id": 1}'))
    b = _tool_call_signature(_tc("r", '{"id": 1}'))
    assert a != b


def test_signature_non_json_falls_back_to_raw():
    a = _tool_call_signature(_tc("q", "not json ("))
    b = _tool_call_signature(_tc("q", "  not json (  "))
    assert a == b  # trimmed raw string
    assert a[0] == "q"


def test_signature_missing_args():
    assert _tool_call_signature(_tc("q", None)) == ("q", "")
    assert _tool_call_signature(_tc("q", "")) == ("q", "")
    assert _tool_call_signature({}) == ("", "")


def test_signature_accepts_dict_arguments():
    a = _tool_call_signature({"function": {"name": "q", "arguments": {"a": 1, "b": 2}}})
    b = _tool_call_signature(_tc("q", '{"b": 2, "a": 1}'))
    assert a == b


# ── _update_repeat_streaks ──────────────────────────────────────────────────

def test_streak_climbs_on_identical_consecutive_rounds():
    sig = _tool_call_signature(_tc("q", '{"id": 1}'))
    streaks: dict = {}
    streaks = _update_repeat_streaks(streaks, [sig])
    assert max(streaks.values(), default=0) == 1
    streaks = _update_repeat_streaks(streaks, [sig])
    assert max(streaks.values(), default=0) == 2  # NUDGE
    streaks = _update_repeat_streaks(streaks, [sig])
    assert max(streaks.values(), default=0) == 3  # BREAK


def test_streak_resets_when_call_changes():
    x = _tool_call_signature(_tc("q", '{"id": 1}'))
    y = _tool_call_signature(_tc("q", '{"id": 2}'))
    streaks = _update_repeat_streaks({}, [x])
    streaks = _update_repeat_streaks(streaks, [x])
    assert max(streaks.values()) == 2
    # A different call this round → x drops out, y starts fresh.
    streaks = _update_repeat_streaks(streaks, [y])
    assert streaks == {y: 1}
    assert max(streaks.values()) == 1


def test_streak_does_not_mutate_input():
    x = _tool_call_signature(_tc("q", '{"id": 1}'))
    prev = {x: 1}
    out = _update_repeat_streaks(prev, [x])
    assert prev == {x: 1}  # unchanged
    assert out == {x: 2}


def test_multiple_calls_per_round_both_climb():
    x = _tool_call_signature(_tc("a", '{"v": 1}'))
    y = _tool_call_signature(_tc("b", '{"v": 2}'))
    streaks = _update_repeat_streaks({}, [x, y])
    streaks = _update_repeat_streaks(streaks, [x, y])
    assert streaks[x] == 2 and streaks[y] == 2


def test_same_call_twice_in_one_round_counts_once():
    x = _tool_call_signature(_tc("a", '{"v": 1}'))
    streaks = _update_repeat_streaks({}, [x, x])
    assert streaks[x] == 1  # within-round dup does not inflate the cross-round streak


def test_partial_overlap_only_repeated_sig_climbs():
    x = _tool_call_signature(_tc("a", '{"v": 1}'))
    y = _tool_call_signature(_tc("b", '{"v": 2}'))
    streaks = _update_repeat_streaks({}, [x, y])  # {x:1, y:1}
    streaks = _update_repeat_streaks(streaks, [x])  # y drops, x climbs
    assert streaks == {x: 2}


def test_thresholds_ordered():
    assert REPEAT_TOOL_CALL_NUDGE < REPEAT_TOOL_CALL_BREAK
    assert REPEAT_TOOL_CALL_NUDGE >= 2


# ── Result-level file failure guard ─────────────────────────────────────────

def test_file_failure_signature_matches_python_and_tool_errors():
    python_error = (
        "FileNotFoundError: [Errno 2] No such file or directory: "
        "'/data/agents/id/workspace/uploads/6 月稽核月报.xlsx'"
    )
    tool_error = "File not found: workspace/uploads/6月稽核月报.xlsx"

    assert _tool_failure_signature(python_error) == _tool_failure_signature(tool_error)
    assert _tool_failure_signature(tool_error) == ("file_not_found", "uploads/6月稽核月报.xlsx")


def test_file_failure_signature_keeps_distinct_virtual_directories_separate():
    uploads = "File not found: workspace/uploads/report.xlsx"
    reports = "File not found: workspace/reports/report.xlsx"
    assert _tool_failure_signature(uploads) != _tool_failure_signature(reports)


def test_file_failure_signature_ignores_unrelated_or_success_results():
    assert _tool_failure_signature("document loaded") is None
    assert _tool_failure_signature([{"type": "text", "text": "File not found: x"}]) is None


def test_file_failure_streak_catches_changing_tool_programs():
    results = [
        "FileNotFoundError: [Errno 2] No such file or directory: 'uploads/6 月稽核月报.xlsx'",
        "File not found: workspace/uploads/6月稽核月报.xlsx",
        "stat: cannot statx 'workspace/uploads/６　月稽核月报.xlsx': No such file or directory",
    ]
    streaks: dict = {}
    decisions = []
    for result in results:
        signature = _tool_failure_signature(result)
        streaks = _update_repeat_streaks(streaks, [signature] if signature else [])
        level = max(streaks.values(), default=0)
        if level >= REPEAT_FILE_FAILURE_BREAK:
            decisions.append("break")
        elif level == REPEAT_FILE_FAILURE_NUDGE:
            decisions.append("nudge")
        else:
            decisions.append("run")

    assert decisions == ["run", "nudge", "break"]


# ── Loop-integration contract ───────────────────────────────────────────────
# Replicates exactly how both tool loops drive the helpers across rounds:
# per round compute sigs → update streaks → break at BREAK (before append),
# nudge at NUDGE (after append). Asserts the decision timeline.

def _simulate(rounds):
    """rounds: list of tool-call lists. Returns timeline of (action) per round
    where action ∈ {'run', 'nudge', 'break'}. 'break' stops the loop."""
    streaks: dict = {}
    timeline = []
    for tcs in rounds:
        sigs = [_tool_call_signature(tc) for tc in tcs]
        streaks = _update_repeat_streaks(streaks, sigs)
        mx = max(streaks.values(), default=0)
        if mx >= REPEAT_TOOL_CALL_BREAK:
            timeline.append("break")
            break
        timeline.append("nudge" if mx == REPEAT_TOOL_CALL_NUDGE else "run")
    return timeline


def test_loop_breaks_on_third_identical_round():
    same = [_tc("guan", '{"cmd": "list"}')]
    timeline = _simulate([same, same, same, same])
    # round1 run, round2 nudge, round3 break (4th never reached)
    assert timeline == ["run", "nudge", "break"]


def test_loop_never_breaks_when_calls_vary():
    timeline = _simulate([
        [_tc("q", '{"id": 1}')],
        [_tc("q", '{"id": 2}')],
        [_tc("q", '{"id": 3}')],
        [_tc("q", '{"id": 4}')],
    ])
    assert timeline == ["run", "run", "run", "run"]
    assert "break" not in timeline


def test_loop_recovers_after_nudge_resets_streak():
    same = [_tc("q", '{"id": 1}')]
    other = [_tc("q", '{"id": 99}')]
    # identical twice (run, nudge), then model changes approach → streak resets,
    # so no break; identical again only reaches run.
    timeline = _simulate([same, same, other, same])
    assert timeline == ["run", "nudge", "run", "run"]
    assert "break" not in timeline
