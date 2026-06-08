"""Invariant tests for DashScope explicit-cache breakpoint selection.

Root cause these lock down: during a tool-calling loop the cache breakpoint
that covers the conversation history used to be skipped (messages[-2] was
always a tool / assistant-tool_call message), so the growing tool-loop tail
was re-prefilled every round → monotonically increasing latency. The core
invariant below — "the tail is always covered" — makes that regression
impossible to reintroduce silently.
"""

from app.services.llm.client import select_cache_breakpoints

SYS = {"role": "system", "content": "you are X"}
USR = {"role": "user", "content": "hi"}
ASS_TXT = {"role": "assistant", "content": "ok"}
ASS_TC = {
    "role": "assistant",
    "content": None,
    "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
}
TOOL = {"role": "tool", "tool_call_id": "c1", "content": "result-bytes"}


def _last_covered(marks):
    return max(marks) if marks else -1


def test_single_message_marks_only_system_or_nothing():
    assert select_cache_breakpoints([SYS]) == [0]
    assert select_cache_breakpoints([USR]) in ([0], [])


def test_invariant_toolloop_tail_is_covered():
    # Core invariant: mid tool-loop (tail = tool result), a breakpoint must
    # cover at/after the last stable message so the tail enters the cache.
    msgs = [SYS, USR, ASS_TC, TOOL, ASS_TC, TOOL]
    marks = select_cache_breakpoints(msgs)
    assert _last_covered(marks) >= len(msgs) - 2, f"tail not covered: {marks}"


def test_tool_role_tail_is_markable():
    # Regression for the bug: tool-result tail must be markable (was skipped).
    msgs = [SYS, USR, ASS_TC, TOOL]
    marks = select_cache_breakpoints(msgs)
    assert max(marks) >= 3, f"tool tail not marked: {marks}"


def test_assistant_toolcall_null_content_falls_back_to_prev_tool():
    # Tail is a content-less assistant tool-call turn → fall back to the
    # preceding tool result (index 3), which IS markable.
    msgs = [SYS, USR, ASS_TC, TOOL, ASS_TC]
    marks = select_cache_breakpoints(msgs)
    assert 3 in marks, f"should fall back to prior tool result: {marks}"


def test_at_most_four_breakpoints_sorted_unique():
    msgs = [SYS] + [USR, ASS_TXT] * 10 + [ASS_TC, TOOL]
    marks = select_cache_breakpoints(msgs)
    assert marks == sorted(set(marks))
    assert len(marks) <= 4


def test_system_always_included_when_present():
    msgs = [SYS, USR, ASS_TC, TOOL]
    assert 0 in select_cache_breakpoints(msgs)


def test_empty_list():
    assert select_cache_breakpoints([]) == []
