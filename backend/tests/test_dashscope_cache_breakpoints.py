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
    # a lone user message IS the last markable message → it gets marked
    assert select_cache_breakpoints([USR]) == [0]


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


# --- integration: _apply_dashscope_cache_markers consumes the selector ---

def _dashscope_client():
    from app.services.llm.client import OpenAICompatibleClient

    return OpenAICompatibleClient(
        api_key="k",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen3.7-max",
        supports_cache_control=True,
    )


def test_dashscope_marks_toolloop_tail_payload():
    # Regression for the production bug: the tool-result tail (messages[-1])
    # must carry cache_control so the growing loop tail enters the cache.
    client = _dashscope_client()
    payload = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "big-tool-result"},
    ]
    client._apply_dashscope_cache_markers(payload)
    tail = payload[-1]
    assert isinstance(tail["content"], list), "tool tail content must be wrapped to list form"
    assert any(b.get("cache_control") for b in tail["content"]), "tool tail must carry cache_control"


def test_apply_cache_control_idempotent_skips_volatile_dynamic_block():
    # _messages_to_openai_payload marks the system STABLE block and appends a
    # VOLATILE dynamic_content block after it. Re-marking must NOT land on the
    # volatile block (that breakpoint never hits + wastes a slot).
    client = _dashscope_client()
    payload = [{
        "role": "system",
        "content": [
            {"type": "text", "text": "static prompt", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "\n\n## Current Time\n2026-06-08 17:00:00"},
        ],
    }]
    client._apply_cache_control_at(payload, 0)
    blocks = payload[0]["content"]
    assert blocks[0].get("cache_control"), "stable block stays marked"
    assert not blocks[1].get("cache_control"), "volatile dynamic block must NOT be marked"


def test_dashscope_marks_system_prefix():
    client = _dashscope_client()
    payload = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]
    client._apply_dashscope_cache_markers(payload)
    sysmsg = payload[0]
    assert isinstance(sysmsg["content"], list)
    assert any(b.get("cache_control") for b in sysmsg["content"])


# --- observability: per-round cache-hit-ratio ---

def test_cache_hit_ratio_helper():
    from app.services.llm.caller import _cache_hit_ratio

    assert _cache_hit_ratio(
        {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 70}}
    ) == 0.7
    # flat cached_tokens field (no details nesting) also works
    assert _cache_hit_ratio({"prompt_tokens": 200, "cached_tokens": 50}) == 0.25
    # unknown / zero prompt → None (don't divide by zero or log noise)
    assert _cache_hit_ratio({"prompt_tokens": 0}) is None
    assert _cache_hit_ratio({}) is None
    assert _cache_hit_ratio(None) is None


# --- guard: tool definitions must serialize byte-identically across rounds ---

def test_agent_tools_serialize_byte_stable_across_calls():
    # DashScope (and implicit prefix caching) require the tools array to be
    # byte-identical every round, or the cached prefix misses. AGENT_TOOLS is a
    # module-level constant; this guard fails loudly if anyone makes it
    # non-deterministic (e.g. building it per-call with set/dict ordering).
    import json

    from app.services.agent_tools import AGENT_TOOLS

    a = json.dumps(AGENT_TOOLS, ensure_ascii=False)
    b = json.dumps(AGENT_TOOLS, ensure_ascii=False)
    assert a == b, "AGENT_TOOLS serialization must be byte-identical across rounds"
