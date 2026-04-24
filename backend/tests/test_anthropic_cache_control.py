"""Unit tests for AnthropicClient._build_payload prompt-cache placement.

In context-v2 the last message carries the volatile `<context>{dynamic_info}</context>\\n\\n{input}`
payload, so placing `cache_control` on it would always write a cache entry that never hits.
These tests lock in the new rule: put `cache_control` on the tail of the stable prefix
(i.e. `anthropic_messages[-2]`), not on the current turn.
"""
from __future__ import annotations

from app.services.llm.client import AnthropicClient, LLMMessage


def _make_client() -> AnthropicClient:
    # API key is never actually used by _build_payload — it just needs an instance.
    return AnthropicClient(api_key="test-key", model="claude-sonnet-4-5")


def _build(messages, tools=None):
    client = _make_client()
    return client._build_payload(messages, tools, temperature=0.0, max_tokens=1024, stream=False)


def _has_cache_control(block) -> bool:
    return isinstance(block, dict) and block.get("cache_control") == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Case 1 - single-turn user: messages must NOT carry cache_control; system/tools do.
# ---------------------------------------------------------------------------
def test_single_user_turn_does_not_cache_messages_but_keeps_system_and_tools():
    messages = [
        LLMMessage(role="system", content="you are helpful"),
        LLMMessage(role="user", content="hi"),
    ]
    tools = [
        {"type": "function", "function": {"name": "foo", "description": "", "parameters": {"type": "object"}}},
    ]
    payload = _build(messages, tools=tools)

    # Only one message in anthropic_messages (the user turn) — no cache_control anywhere on it.
    assert len(payload["messages"]) == 1
    only_msg = payload["messages"][0]
    # content may be str or list; either way, no cache_control should appear.
    if isinstance(only_msg["content"], list):
        assert not any(_has_cache_control(b) for b in only_msg["content"])
    else:
        # str content can't carry cache_control by construction
        assert isinstance(only_msg["content"], str)

    # system cache_control preserved on the static block.
    assert payload["system"][0]["text"] == "you are helpful"
    assert _has_cache_control(payload["system"][0])

    # tools cache_control preserved on the last tool.
    assert _has_cache_control(payload["tools"][-1])


# ---------------------------------------------------------------------------
# Case 2 - multi-turn: cache_control goes on messages[-2] (assistant), not messages[-1].
# ---------------------------------------------------------------------------
def test_multi_turn_places_cache_control_on_penultimate_assistant():
    messages = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="round 0 question"),
        LLMMessage(role="assistant", content="round 0 answer"),
        LLMMessage(role="user", content="round 1 question with <context>volatile</context>"),
    ]
    payload = _build(messages)

    anthropic_messages = payload["messages"]
    assert len(anthropic_messages) == 3

    # [-2] is assistant — last content block should carry cache_control.
    prefix_msg = anthropic_messages[-2]
    assert prefix_msg["role"] == "assistant"
    if isinstance(prefix_msg["content"], list):
        assert _has_cache_control(prefix_msg["content"][-1])
    else:
        # to_anthropic_format collapses single-text-block content back to str;
        # in that case _build_payload must wrap it into a list with cache_control.
        raise AssertionError("assistant content should have been wrapped into a list")

    # [-1] is the fresh user turn — must NOT carry cache_control anywhere.
    last_msg = anthropic_messages[-1]
    assert last_msg["role"] == "user"
    if isinstance(last_msg["content"], list):
        assert not any(_has_cache_control(b) for b in last_msg["content"])
    else:
        assert isinstance(last_msg["content"], str)


# ---------------------------------------------------------------------------
# Case 3 - mid tool-calling loop: user -> assistant(tool_calls) -> tool_result.
# cache_control should land on [-2] = assistant with tool_calls.
# ---------------------------------------------------------------------------
def test_tool_calling_loop_caches_on_assistant_tool_call():
    messages = [
        LLMMessage(role="user", content="do the thing"),
        LLMMessage(
            role="assistant",
            content=None,
            tool_calls=[
                {"id": "call_1", "type": "function", "function": {"name": "foo", "arguments": "{}"}}
            ],
        ),
        LLMMessage(role="tool", tool_call_id="call_1", content="ok"),
    ]
    payload = _build(messages)

    anthropic_messages = payload["messages"]
    assert len(anthropic_messages) == 3

    # [-2] is the assistant that issued the tool_use block
    prefix_msg = anthropic_messages[-2]
    assert prefix_msg["role"] == "assistant"
    assert isinstance(prefix_msg["content"], list)
    last_block = prefix_msg["content"][-1]
    assert last_block["type"] == "tool_use"
    assert _has_cache_control(last_block)

    # [-1] is tool_result (wrapped as role=user with tool_result block) — no cache_control.
    last_msg = anthropic_messages[-1]
    assert last_msg["role"] == "user"
    assert isinstance(last_msg["content"], list)
    assert last_msg["content"][0]["type"] == "tool_result"
    assert not _has_cache_control(last_msg["content"][0])


# ---------------------------------------------------------------------------
# Case 4 - tool_result as the cache anchor: cache_control must live at the top
# level of the tool_result block, NOT nested inside tool_result["content"].
# ---------------------------------------------------------------------------
def test_cache_control_on_tool_result_is_top_level_not_nested():
    messages = [
        LLMMessage(role="user", content="do the thing"),
        LLMMessage(
            role="assistant",
            content=None,
            tool_calls=[
                {"id": "call_1", "type": "function", "function": {"name": "foo", "arguments": "{}"}}
            ],
        ),
        LLMMessage(role="tool", tool_call_id="call_1", content="tool output body"),
        LLMMessage(role="assistant", content="final answer"),
    ]
    payload = _build(messages)

    anthropic_messages = payload["messages"]
    assert len(anthropic_messages) == 4

    prefix_msg = anthropic_messages[-2]
    # [-2] is tool_result message (role=user, content=[{type:tool_result,...}])
    assert prefix_msg["role"] == "user"
    assert isinstance(prefix_msg["content"], list)
    tr_block = prefix_msg["content"][-1]
    assert tr_block["type"] == "tool_result"

    # cache_control must be a top-level key on the tool_result block.
    assert _has_cache_control(tr_block)

    # And it must NOT have been pushed into the inner content payload.
    inner = tr_block.get("content")
    if isinstance(inner, list):
        for part in inner:
            assert not _has_cache_control(part)
    else:
        # inner is a plain string — fine.
        assert isinstance(inner, str)


# ---------------------------------------------------------------------------
# Case 5 - when [-2].content is a plain string, wrap it into a list and add cache_control.
# ---------------------------------------------------------------------------
def test_string_content_is_wrapped_into_list_with_cache_control():
    # A single-text assistant turn collapses to `content = str` in to_anthropic_format,
    # so this is the realistic path.
    messages = [
        LLMMessage(role="user", content="q0"),
        LLMMessage(role="assistant", content="plain string answer"),
        LLMMessage(role="user", content="q1"),
    ]
    payload = _build(messages)

    prefix_msg = payload["messages"][-2]
    assert prefix_msg["role"] == "assistant"
    # Must now be a list because _build_payload wrapped it to attach cache_control.
    assert isinstance(prefix_msg["content"], list), (
        "string content at [-2] must be wrapped into a list so cache_control can attach"
    )
    assert len(prefix_msg["content"]) == 1
    block = prefix_msg["content"][0]
    assert block["type"] == "text"
    assert block["text"] == "plain string answer"
    assert _has_cache_control(block)


# ---------------------------------------------------------------------------
# Case 6 - first turn (len(messages) == 1): no cache_control on messages.
# ---------------------------------------------------------------------------
def test_first_turn_no_cache_control_on_messages():
    messages = [LLMMessage(role="user", content="first ever message")]
    payload = _build(messages)

    assert len(payload["messages"]) == 1
    only_msg = payload["messages"][0]
    if isinstance(only_msg["content"], list):
        assert not any(_has_cache_control(b) for b in only_msg["content"])
    else:
        assert isinstance(only_msg["content"], str)
