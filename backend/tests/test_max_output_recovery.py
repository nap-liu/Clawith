"""P4: max_output_tokens resume recovery (Claude-Code-aligned).

When a provider truncates the response mid-thought because the per-call output
token cap was hit, we do NOT surface an error. Instead we push the partial
content into history as an assistant message, append a terse RESUME_PROMPT
user message, and re-stream. Up to MAX_OUTPUT_TOKENS_RECOVERY_LIMIT (3)
attempts per call; after that we surface a clean error string.

Cases locked in here:

C. Three consecutive truncated responses + a fourth still truncated →
   caller returns the exhaustion error string verbatim.
D. Recovery concatenates partial content and the dispatch ``messages`` sent
   to ``client.stream`` retain an append-only
   ``assistant(partial) + user(RESUME_PROMPT)`` tail.
E. If the re-streamed (resumed) response carries ``tool_calls``, the outer
   tool-calling loop handles it normally — truncation recovery and tool
   calling compose cleanly without interfering.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.chat_history import build_llm_messages_from_rows
from app.services.llm.caller import (
    MAX_OUTPUT_TOKENS_RECOVERY_LIMIT,
    RESUME_PROMPT,
    _response_was_truncated_by_length,
    call_llm,
)
from app.services.llm.client import LLMError, LLMMessage, LLMResponse
from app.database import engine


@pytest.fixture(autouse=True)
async def isolated_engine():
    # Plain completions also clear recovered Responses checkpoints in the DB.
    # Each pytest event loop must release the connections it created.
    await engine.dispose()
    yield
    await engine.dispose()


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
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

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
    monkeypatch.setattr(
        "app.services.llm.caller._persist_tool_call_events_strict",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.session_token_usage.load_latest_round_context_usage",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.conversation_turn_lifecycle.assert_conversation_turn_running",
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


def test_durable_max_output_partial_replays_resume_prompt():
    row = SimpleNamespace(
        id=uuid.uuid4(),
        role="assistant",
        content="partial-",
        thinking=None,
        message_meta={
            "artifact_role": "intermediate_assistant",
            "turn_status": "running",
            "max_output_resume_prompt": RESUME_PROMPT,
            "attachments": [],
        },
    )

    assert build_llm_messages_from_rows([row]) == [
        {"role": "assistant", "content": "partial-"},
        {"role": "user", "content": RESUME_PROMPT},
    ]
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
    assert "DYN" in call_0[-1].content
    assert call_0[-1].content.endswith("keep going")

    # Second call (first resume): tail should be …user, assistant(part-a), user(RESUME_PROMPT)
    call_1: list[LLMMessage] = client.stream_calls[1]["messages"]
    assert call_1[: len(call_0)] == call_0
    assert "DYN" in call_1[-3].content
    assert call_1[-2].role == "assistant"
    assert call_1[-2].content == "part-a "
    assert call_1[-1].role == "user"
    assert call_1[-1].content == RESUME_PROMPT

    # Third call (second resume): tail should have TWO (assistant, user) resume pairs.
    call_2: list[LLMMessage] = client.stream_calls[2]["messages"]
    assert call_2[: len(call_1)] == call_1
    assert "DYN" in call_2[-5].content
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

    tool_events = []

    async def capture_tool_event(event):
        tool_events.append(dict(event))

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "please check a.txt"}],
        agent_name="T", role_description="",
        agent_id="agent-x", user_id="user-x", session_id="s",
        on_tool_call=capture_tool_event,
    )

    # Output-limit recovery within the tool round remains lossless in its
    # durable recovery prefix, but tool-round narration is not terminal reply
    # content.
    assert result == "done-after-tool"
    # 3 stream calls: initial + 1 resume + 1 follow-up round
    assert len(client.stream_calls) == 3

    # Round 1 must retain the exact recovery request as its prefix, then append
    # the resumed assistant tool call and tool result.
    round_1_msgs: list[LLMMessage] = client.stream_calls[2]["messages"]
    recovery_request: list[LLMMessage] = client.stream_calls[1]["messages"]
    assert round_1_msgs[: len(recovery_request)] == recovery_request

    # Find the assistant turn with tool_calls
    tool_turn_idx = None
    for i, m in enumerate(round_1_msgs):
        if m.role == "assistant" and m.tool_calls:
            tool_turn_idx = i
            break
    assert tool_turn_idx is not None, "expected an assistant/tool_calls message"
    tool_turn = round_1_msgs[tool_turn_idx]
    assert tool_turn.content == "going to call a tool", (
        "the tool-call turn contains only the resumed segment; the earlier "
        "partial remains in the immutable recovery prefix"
    )
    assert any(m.role == "user" and m.content == RESUME_PROMPT for m in round_1_msgs)
    # And the tool result follows right after the tool-calls assistant turn.
    assert round_1_msgs[tool_turn_idx + 1].role == "tool"
    assert round_1_msgs[tool_turn_idx + 1].content == "file-contents"
    done_event = next(event for event in tool_events if event.get("status") == "done")
    assert done_event["assistant_content"] == "going to call a tool"
    assert done_event["recovery_prefix_messages"] == [
        {"role": "assistant", "content": "thinking... "},
        {"role": "user", "content": RESUME_PROMPT},
    ]


@pytest.mark.asyncio
async def test_changing_file_failures_remain_visible_to_model_until_it_replies(monkeypatch):
    def _tool_response(call_id: int, code: str) -> LLMResponse:
        return LLMResponse(
            content="trying another approach",
            tool_calls=[{
                "id": f"call_{call_id}",
                "type": "function",
                "function": {
                    "name": "execute_code_aio",
                    "arguments": json.dumps({
                        "action": "execute",
                        "execution_mode": "foreground",
                        "language": "python",
                        "code": code,
                    }),
                },
            }],
            finish_reason="tool_calls",
        )

    client = _ScriptedClient([
        _tool_response(1, "open('uploads/6 月稽核月报.xlsx')"),
        _tool_response(2, "import pandas as pd; pd.read_excel('workspace/uploads/6月稽核月报.xlsx')"),
        _tool_response(3, "from pathlib import Path; Path('workspace/uploads/６　月稽核月报.xlsx').read_bytes()"),
        _stop_response("I could not find the exact requested path after three different attempts."),
    ])
    _patch_caller_collaborators(monkeypatch, client, tools=[
        {"type": "function", "function": {"name": "execute_code_aio", "description": "run"}},
    ])

    errors = iter([
        "FileNotFoundError: [Errno 2] No such file or directory: 'uploads/6 月稽核月报.xlsx'",
        "File not found: workspace/uploads/6月稽核月报.xlsx",
        "stat: cannot statx 'workspace/uploads/６　月稽核月报.xlsx': No such file or directory",
    ])

    async def _fake_tool(*_args, **_kwargs):
        return next(errors)

    monkeypatch.setattr("app.services.llm.caller.execute_tool", _fake_tool)

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "analyze the attachment"}],
        agent_name="T",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="s",
    )

    assert result == "I could not find the exact requested path after three different attempts."
    assert len(client.stream_calls) == 4
    final_round_messages: list[LLMMessage] = client.stream_calls[3]["messages"]
    tool_results = [message.content for message in final_round_messages if message.role == "tool"]
    assert tool_results == [
        "FileNotFoundError: [Errno 2] No such file or directory: 'uploads/6 月稽核月报.xlsx'",
        "File not found: workspace/uploads/6月稽核月报.xlsx",
        "stat: cannot statx 'workspace/uploads/６　月稽核月报.xlsx': No such file or directory",
    ]
    assert client.closed is True


@pytest.mark.asyncio
async def test_late_injection_tail_ignores_prior_tool_narration_during_recovery(
    monkeypatch,
):
    client = _ScriptedClient(
        [
            LLMResponse(
                content="tool preamble",
                tool_calls=[
                    {
                        "id": "missing-1",
                        "type": "function",
                        "function": {"name": "missing_tool", "arguments": "{}"},
                    }
                ],
                finish_reason="tool_calls",
            ),
            _stop_response("A1"),
            LLMError("context_length_exceeded"),
            _stop_response("A2"),
        ]
    )
    _patch_caller_collaborators(monkeypatch, client)
    monkeypatch.setattr(
        "app.services.llm.caller._persist_tool_call_events_strict",
        AsyncMock(return_value={"missing-1": uuid.uuid4()}),
    )
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    anchor_id = uuid.uuid4()
    events: list[str] = []
    persisted: list[dict[str, Any]] = []

    async def persist_intermediate(_factory, **kwargs):
        events.append("persist:A1")
        persisted.append(dict(kwargs))
        return uuid.uuid4()

    monkeypatch.setattr(
        "app.services.chat_history.persist_intermediate_assistant_reply",
        persist_intermediate,
    )
    delivered = False

    async def before_round(_round, *, before_injection=None):
        nonlocal delivered
        if delivered or before_injection is None:
            return []
        await before_injection()
        events.append("claim:injection")
        delivered = True
        return [{"role": "user", "content": "injection"}]

    async def recover(_model, _budget):
        events.append("recover")
        return [
            {"role": "user", "content": "root"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "injection"},
        ]

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "root"}],
        agent_name="T",
        role_description="",
        agent_id=agent_id,
        user_id=user_id,
        session_id=str(uuid.uuid4()),
        turn_anchor_id=anchor_id,
        turn_anchor_agent_id=agent_id,
        context_recovery=recover,
        before_round=before_round,
    )

    assert result == "A1\n\nA2"
    assert events[:2] == ["persist:A1", "claim:injection"]
    assert events.count("persist:A1") == 1
    assert persisted[0]["content"] == "A1"
    assert persisted[0]["visible_joiner_before"] == ""
    assert len(client.stream_calls) == 4
    recovered_messages = client.stream_calls[-1]["messages"]
    assert [message.role for message in recovered_messages][-3:] == [
        "user",
        "assistant",
        "user",
    ]
    assert recovered_messages[-2].content == "A1"
    assert "injection" in recovered_messages[-1].content


@pytest.mark.asyncio
async def test_max_output_partial_is_durable_before_resume_overflow_recovery(
    monkeypatch,
):
    client = _ScriptedClient(
        [
            LLMResponse(content="partial-", finish_reason="length"),
            LLMError("maximum context length exceeded"),
            _stop_response("tail"),
        ]
    )
    _patch_caller_collaborators(monkeypatch, client)
    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    persisted: list[dict[str, Any]] = []

    async def persist_intermediate(_factory, **kwargs):
        persisted.append(dict(kwargs))
        return uuid.uuid4()

    monkeypatch.setattr(
        "app.services.chat_history.persist_intermediate_assistant_reply",
        persist_intermediate,
    )

    async def recover(_model, _budget):
        return [
            {"role": "user", "content": "root"},
            {"role": "assistant", "content": "partial-"},
            {"role": "user", "content": RESUME_PROMPT},
        ]

    result = await call_llm(
        model=_FakeModel(),
        messages=[{"role": "user", "content": "root"}],
        agent_name="T",
        role_description="",
        agent_id=agent_id,
        user_id=user_id,
        session_id=str(uuid.uuid4()),
        turn_anchor_id=uuid.uuid4(),
        turn_anchor_agent_id=agent_id,
        context_recovery=recover,
    )

    assert result == "partial-tail"
    assert len(persisted) == 1
    assert persisted[0]["content"] == "partial-"
    assert persisted[0]["max_output_resume_prompt"] == RESUME_PROMPT
    assert len(client.stream_calls) == 3
    recovered_messages = client.stream_calls[-1]["messages"]
    assert recovered_messages[-2].role == "assistant"
    assert recovered_messages[-2].content == "partial-"
    assert recovered_messages[-1].role == "user"
    assert recovered_messages[-1].content.endswith(RESUME_PROMPT)
