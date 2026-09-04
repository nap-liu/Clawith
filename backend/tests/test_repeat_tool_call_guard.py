"""Behavior checks for bounded period-1/2 tool-loop detection."""

from app.services.llm.caller import (
    _repeating_tool_period,
    _tool_call_signature,
    _tool_round_fingerprint,
    _tool_round_observation,
)


def _tc(name: str, arguments):
    return {"id": "x", "function": {"name": name, "arguments": arguments}}


def _observation(tool_calls, results):
    return _tool_round_observation(tool_calls, results)


def test_signature_normalizes_json_but_preserves_meaning():
    first = _tool_call_signature(_tc("q", '{"a": 1, "b": 2}'))
    reordered = _tool_call_signature(_tc("q", '{ "b": 2, "a": 1 }'))
    changed = _tool_call_signature(_tc("q", '{"a": 2, "b": 2}'))

    assert first == reordered
    assert first != changed


def test_round_fingerprint_is_order_independent_for_parallel_calls():
    first = [_tc("a", '{"v": 1}'), _tc("b", '{"v": 2}')]
    reversed_round = list(reversed(first))

    assert _tool_round_fingerprint(first) == _tool_round_fingerprint(reversed_round)


def test_period_one_stops_only_after_calls_and_results_repeat():
    same = [_tc("status", '{"job": 1}')]
    history = [
        _observation(same, ["pending"]),
        _observation(same, ["pending"]),
    ]

    assert _repeating_tool_period(history) == 1
    assert _repeating_tool_period(history, same) == 1


def test_period_two_detects_alternating_calls_with_unchanged_results():
    first = [_tc("lookup", '{"id": 1}')]
    second = [_tc("refresh", '{"id": 1}')]
    history = [
        _observation(first, ["missing"]),
        _observation(second, ["unchanged"]),
        _observation(first, ["missing"]),
        _observation(second, ["unchanged"]),
    ]

    assert _repeating_tool_period(history) == 2
    assert _repeating_tool_period(history, first) == 2
    assert _repeating_tool_period(history, second) is None


def test_same_polling_call_with_changing_result_is_progress():
    same = [_tc("status", '{"job": 1}')]
    history = [
        _observation(same, ["queued"]),
        _observation(same, ["running"]),
        _observation(same, ["75%"]),
    ]

    assert _repeating_tool_period(history) is None
    assert _repeating_tool_period(history, same) is None


def test_changing_multimodal_tool_content_is_progress():
    same = [_tc("screenshot", "{}")]
    history = [
        _observation(same, [[{"type": "image_url", "image_url": {"url": "frame-1"}}]]),
        _observation(same, [[{"type": "image_url", "image_url": {"url": "frame-2"}}]]),
    ]

    assert _repeating_tool_period(history) is None


def test_non_periodic_work_is_not_stopped():
    history = [
        _observation([_tc("q", '{"page": 1}')], ["one"]),
        _observation([_tc("q", '{"page": 2}')], ["two"]),
        _observation([_tc("q", '{"page": 3}')], ["three"]),
        _observation([_tc("q", '{"page": 4}')], ["four"]),
    ]

    assert _repeating_tool_period(history) is None


def test_missing_result_does_not_create_a_loop_observation():
    calls = [_tc("a", "{}"), _tc("b", "{}")]

    assert _tool_round_observation(calls, ["only one result"]) == ()
