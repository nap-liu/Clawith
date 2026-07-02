"""P4: max_output_tokens resume recovery (Claude-Code-aligned).

When a provider truncates the response mid-thought because the per-call output
token cap was hit, we do NOT surface an error. Instead we push the partial
content into history as an assistant message, append a terse RESUME_PROMPT
user message, and re-stream. Up to MAX_OUTPUT_TOKENS_RECOVERY_LIMIT (3)
attempts per call; after that we surface a clean error string.

Cases locked in here:

A. ``finish_reason == "stop"`` → no recovery, normal return.
B. First response truncated (``"length"``) + partial text, second response
   ``"stop"`` + remainder → caller returns full concatenated content and a
   single resume pair was added to and then popped from history (final
   api_messages seen by downstream code keeps "one logical turn =
   one assistant message" shape).
C. Three consecutive truncated responses + a fourth still truncated →
   caller returns the exhaustion error string verbatim.
D. During recovery the dispatch ``messages`` sent to ``client.stream`` carry
   a transient ``assistant(partial) + user(RESUME_PROMPT)`` tail.
E. If the re-streamed (resumed) response carries ``tool_calls``, the outer
   tool-calling loop handles it normally — truncation recovery and tool
   calling compose cleanly without interfering.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.llm.caller import (
    MAX_OUTPUT_TOKENS_RECOVERY_LIMIT,
    RESUME_PROMPT,
    _response_was_truncated_by_length,
    call_llm,
)
from app.services.llm.client import LLMMessage, LLMResponse


def _stop_response(content: str) -> LLMResponse:
    """A plain-text terminal response — no tool calls, so the loop returns it."""
    return LLMResponse(content=content, finish_reason="stop")


# ─── Fakes ────────────────────────────────────────────────────────────────────

class _ScriptedClient:
    """A fake LLM client whose ``stream`` yields a pre-scripted list of
    ``LLMResponse`` objects in order. Each call to ``stream`` pops the next
    response off the script and records the ``messages`` argument so tests
    can assert what the caller sent on each attempt."""

    def __init__(self, script: list[LLMResponse]) -> None:
        self._script = list(script)
        self.stream_calls: list[dict[str, Any]] = []
        self.closed = False

    async def stream(
        self,
        messages,
        tools=None,
        temperature=None,
        max_tokens=None,
        on_chunk=None,
        on_thinking=None,
        **kwargs,
    ):
        self.stream_calls.append({
            "messages": list(messages),
            "tools": list(tools) if tools else None,
        })
        if not self._script:
            raise AssertionError("ScriptedClient ran out of responses")
        return self._script.pop(0)

    async def close(self):
        self.closed = True


class _FakeModel:
    provider = "qwen"
    model = "qwen-turbo"
    base_url = "https://example.invalid"
    api_key_encrypted = ""
    temperature = 0.7
    max_output_tokens = None
    request_timeout = 30.0
    id = "model-x"


# ─── Common monkeypatch fixture ───────────────────────────────────────────────

def _patch_caller_collaborators(monkeypatch, client, tools=None):
    """Short-circuit every collaborator call_llm touches before/around stream."""
    monkeypatch.setattr(
        "app.services.llm.caller.create_llm_client",
        lambda **kwargs: client,
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_max_tokens",
        lambda *a, **k: 1024,
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_model_api_key",
        lambda m: "fake-key",
    )
    monkeypatch.setattr(
        "app.services.llm.caller._get_agent_config",
        AsyncMock(return_value=(50, None)),
    )
    monkeypatch.setattr(
        "app.services.llm.caller._get_user_name",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("STATIC", "DYN")),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(return_value=tools or []),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.record_token_usage",
        AsyncMock(return_value=None),
    )


# ─── Helper predicate tests ───────────────────────────────────────────────────

def test_predicate_recognises_all_three_provider_spellings():
    """Recognises OpenAI-style ``"length"``, Anthropic ``"max_tokens"``, and
    the raw Gemini ``"MAX_TOKENS"`` (defensive — Gemini's normalizer usually
    down-casts this but a fallback branch could leak the raw label)."""
    assert _response_was_truncated_by_length(
        LLMResponse(content="x", finish_reason="length")
    ) is True
    assert _response_was_truncated_by_length(
        LLMResponse(content="x", finish_reason="max_tokens")
    ) is True
    assert _response_was_truncated_by_length(
        LLMResponse(content="x", finish_reason="MAX_TOKENS")
    ) is True


def test_predicate_rejects_normal_finish_reasons():
    for r in ("stop", "end_turn", "tool_calls", "tool_use", None, ""):
        assert _response_was_truncated_by_length(
            LLMResponse(content="x", finish_reason=r)
        ) is False, f"{r!r} should NOT trigger recovery"


# ─── Case A: no truncation → single stream, no recovery ───────────────────────

@pytest.mark.asyncio
async def test_case_a_no_truncation_returns_direct(monkeypatch):
    client = _ScriptedClient([
        _stop_response("all-done"),
    ])
    _patch_caller_collaborators(monkeypatch, client)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "hi"}],
        agent_name="T", role_description="",
        agent_id="agent-x", user_id="user-x", session_id="s",
    )

    assert result == "all-done"
    assert len(client.stream_calls) == 1, "no recovery should not re-stream"
    assert client.closed is True


# ─── Case B: one recovery → content concatenated, transient pair popped ────────

@pytest.mark.asyncio
async def test_case_b_single_recovery_concatenates_content(monkeypatch):
    # First stream: partial text, cut off by length.
    # Second stream: the resume continues where the partial left off. The
    # recovery runs (1 resume = 2 stream calls); the loop stitches the partial
    # and the continuation and returns the concatenation.
    client = _ScriptedClient([
        LLMResponse(content="half-one ", finish_reason="length"),
        _stop_response("half-two"),
    ])
    _patch_caller_collaborators(monkeypatch, client)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "write something long"}],
        agent_name="T", role_description="",
        agent_id="agent-x", user_id="user-x", session_id="s",
    )

    # Returned string is the full concatenation.
    assert result == "half-one half-two"
    # We made exactly 2 stream calls: the initial + 1 resume.
    assert len(client.stream_calls) == 2
    assert client.closed is True


# ─── Case C: 3 retries exhausted → error string ───────────────────────────────

@pytest.mark.asyncio
async def test_case_c_exhausted_retries_returns_error(monkeypatch):
    # 1 initial + 3 retries = 4 stream calls, all truncated.
    client = _ScriptedClient([
        LLMResponse(content="chunk-0 ", finish_reason="length"),
        LLMResponse(content="chunk-1 ", finish_reason="length"),
        LLMResponse(content="chunk-2 ", finish_reason="length"),
        LLMResponse(content="chunk-3 ", finish_reason="length"),
    ])
    _patch_caller_collaborators(monkeypatch, client)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "write forever"}],
        agent_name="T", role_description="",
        agent_id="agent-x", user_id="user-x", session_id="s",
    )

    assert result == (
        "[LLM Error] Output token limit exceeded after 3 resume attempts"
    )
    # 1 initial + MAX_OUTPUT_TOKENS_RECOVERY_LIMIT (3) retries = 4 stream calls.
    assert len(client.stream_calls) == 1 + MAX_OUTPUT_TOKENS_RECOVERY_LIMIT
    assert client.closed is True


# ─── Case D: dispatched messages during recovery carry the resume pair ────────

@pytest.mark.asyncio
async def test_case_d_dispatched_messages_carry_resume_pair(monkeypatch):
    # Two recoveries then a clean stop — three stream calls total. The
    # final resumed response continues the text; the loop stitches all parts.
    client = _ScriptedClient([
        LLMResponse(content="part-a ", finish_reason="length"),
        LLMResponse(content="part-b ", finish_reason="length"),
        _stop_response("part-c"),
    ])
    _patch_caller_collaborators(monkeypatch, client)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "keep going"}],
        agent_name="T", role_description="",
        agent_id="agent-x", user_id="user-x", session_id="s",
    )
    assert result == "part-a part-b part-c"
    assert len(client.stream_calls) == 3

    # First call (initial): tail is the single wrapped user message.
    call_0: list[LLMMessage] = client.stream_calls[0]["messages"]
    assert call_0[-1].role == "user"
    assert call_0[-1].content.endswith("keep going")

    # Second call (first resume): tail should be …user, assistant(part-a), user(RESUME_PROMPT)
    call_1: list[LLMMessage] = client.stream_calls[1]["messages"]
    assert call_1[-2].role == "assistant"
    assert call_1[-2].content == "part-a "
    assert call_1[-1].role == "user"
    assert call_1[-1].content == RESUME_PROMPT

    # Third call (second resume): tail should have TWO (assistant, user) resume pairs.
    call_2: list[LLMMessage] = client.stream_calls[2]["messages"]
    assert call_2[-4].role == "assistant" and call_2[-4].content == "part-a "
    assert call_2[-3].role == "user" and call_2[-3].content == RESUME_PROMPT
    assert call_2[-2].role == "assistant" and call_2[-2].content == "part-b "
    assert call_2[-1].role == "user" and call_2[-1].content == RESUME_PROMPT


# ─── Case E: recovery composes with tool_calls loop ───────────────────────────

@pytest.mark.asyncio
async def test_case_e_recovery_then_tool_call_then_final(monkeypatch, tmp_path):
    # Scenario: initial response truncated → recovery → resumed response
    # carries tool_calls → caller executes the tool → next round returns
    # clean final content. Verifies truncation recovery and tool-calling
    # are independent concerns that compose correctly.
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path))

    client = _ScriptedClient([
        # Round 0, first stream: partial, cut off
        LLMResponse(content="thinking... ", finish_reason="length"),
        # Round 0, resume: now picks up a tool call
        LLMResponse(
            content="going to call a tool",
            tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
            }],
            finish_reason="tool_calls",
        ),
        # Round 1 (after tool result): final answer as plain text
        _stop_response("done-after-tool"),
    ])
    _patch_caller_collaborators(monkeypatch, client, tools=[
        {"type": "function", "function": {"name": "read_file", "description": "r"}},
    ])

    # Stub out the actual tool execution so we don't touch the filesystem.
    async def _fake_tool(tool_name, args, **kwargs):
        return "file-contents"

    monkeypatch.setattr(
        "app.services.llm.caller.execute_tool",
        _fake_tool,
    )

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "please check a.txt"}],
        agent_name="T", role_description="",
        agent_id="agent-x", user_id="user-x", session_id="s",
    )

    # Final content comes from round 1, not from the recovered-with-tool-calls round.
    assert result == "done-after-tool"
    # 3 stream calls: initial + 1 resume + 1 follow-up round
    assert len(client.stream_calls) == 3

    # Round 1's dispatched messages should contain the CONCATENATED assistant
    # content ("thinking... going to call a tool") as a single turn, plus the
    # tool message — NOT the transient (partial_assistant, resume_user) pair.
    round_1_msgs: list[LLMMessage] = client.stream_calls[2]["messages"]
    # Find the assistant turn with tool_calls
    tool_turn_idx = None
    for i, m in enumerate(round_1_msgs):
        if m.role == "assistant" and m.tool_calls:
            tool_turn_idx = i
            break
    assert tool_turn_idx is not None, "expected an assistant/tool_calls message"
    tool_turn = round_1_msgs[tool_turn_idx]
    assert tool_turn.content == "thinking... going to call a tool", (
        "partial and resumed content should be stitched into the single "
        "assistant turn that carries the tool_calls"
    )
    # Verify the transient RESUME_PROMPT user message is NOT in history.
    for m in round_1_msgs:
        if m.role == "user":
            assert m.content != RESUME_PROMPT, (
                "RESUME_PROMPT transient pair must be popped before the "
                "next round so history stays 'one turn = one message'"
            )
    # And the tool result follows right after the tool-calls assistant turn.
    assert round_1_msgs[tool_turn_idx + 1].role == "tool"
    assert round_1_msgs[tool_turn_idx + 1].content == "file-contents"
