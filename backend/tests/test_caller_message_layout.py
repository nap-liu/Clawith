"""P1-A message layout contract: stable prefix for Anthropic prompt cache
and Qwen/DashScope automatic prefix cache.

What this test locks in:
  1. ``messages[0]`` is ``system`` and its ``content`` is the *static* prompt only
     — no "Current Time" or other per-call dynamic bits leak into system.
  2. ``messages[0].dynamic_content is None`` — no back-door dynamic injection
     via the old OpenAI ``to_openai_format`` path.
  3. The *last* ``user`` message sent this turn is wrapped once as
     ``<context>\n{dynamic}\n</context>\n\n{original_user_input}``.
  4. *Historical* user messages (earlier turns) are NOT wrapped — they retain
     their original append-only content.
  5. ``tools`` passed to ``client.stream`` are sorted by ``function.name`` so
     ``tools[-1]`` (where Anthropic puts cache_control) is stable per agent.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.services.llm.caller import _attach_turn_context, call_llm, call_llm_with_failover
from app.services.llm.client import LLMError, LLMMessage, LLMResponse


class _FakeClient:
    """Captures the arguments to ``stream`` / ``close`` for inspection."""

    def __init__(self) -> None:
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
        # Deep-snapshot inputs: the caller may mutate api_messages after this
        # call, but the dispatched list should already be fully-formed.
        self.stream_calls.append({
            "messages": list(messages),
            "tools": list(tools) if tools else None,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        # A plain-text response (no tool calls) ends the turn, so
        # caller.call_llm returns its content after exactly one round.
        return LLMResponse(
            content="ok-final",
            finish_reason="stop",
            usage=None,
        )

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
    supports_vision = False


class _FailingClient(_FakeClient):
    async def stream(self, messages, tools=None, temperature=None, max_tokens=None, **kwargs):
        self.stream_calls.append({"messages": list(messages), "tools": tools})
        raise LLMError("connection timeout")


def test_unattended_turn_without_user_message_still_receives_context():
    original = [LLMMessage(role="system", content="STATIC")]

    attached = _attach_turn_context(original, "MEMORY-SNAPSHOT")

    assert original == [LLMMessage(role="system", content="STATIC")]
    assert [message.role for message in attached] == ["system", "user"]
    assert attached[-1].content == "<context>\nMEMORY-SNAPSHOT\n</context>"


def test_confirmation_continuation_appends_context_without_rewriting_old_user():
    original = [
        LLMMessage(role="system", content="STATIC"),
        LLMMessage(role="user", content="historical request"),
        LLMMessage(
            role="assistant",
            content="please confirm",
            tool_calls=[{
                "id": "confirmation-1",
                "type": "function",
                "function": {"name": "request_confirmation", "arguments": "{}"},
            }],
        ),
        LLMMessage(role="tool", content="approved", tool_call_id="confirmation-1"),
    ]

    attached = _attach_turn_context(original, "CURRENT-SNAPSHOT")

    assert attached[: len(original)] == original
    assert attached[1].content == "historical request"
    assert attached[-1] == LLMMessage(
        role="user",
        content="<context>\nCURRENT-SNAPSHOT\n</context>",
    )


@pytest.mark.asyncio
async def test_failover_reuses_one_frozen_turn_context(monkeypatch):
    built_context = ("STATIC-ONCE", "DYNAMIC-ONCE")
    context_builder = AsyncMock(return_value=built_context)
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        context_builder,
    )
    monkeypatch.setattr(
        "app.services.llm.caller._get_user_name",
        AsyncMock(return_value="Current User"),
    )

    call_contexts: list[tuple[str, str] | None] = []

    async def fake_call_llm(*args, prepared_turn_context=None, **kwargs):
        call_contexts.append(prepared_turn_context)
        if len(call_contexts) == 1:
            return "[LLM Error] connection timeout"
        return "fallback-ok"

    monkeypatch.setattr("app.services.llm.caller.call_llm", fake_call_llm)

    primary = _FakeModel()
    fallback = _FakeModel()
    fallback.id = "model-y"
    result = await call_llm_with_failover(
        primary_model=primary,
        fallback_model=fallback,
        messages=[{"role": "user", "content": "hello"}],
        agent_name="T",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
    )

    assert result == "fallback-ok"
    assert context_builder.await_count == 1
    assert call_contexts == [built_context, built_context]
    assert call_contexts[0] is call_contexts[1]


@pytest.mark.parametrize(
    ("primary_vision", "fallback_vision"),
    [(True, False), (False, True)],
)
async def test_failover_rebuilds_provider_payload_for_each_model_capability(
    monkeypatch,
    primary_vision,
    fallback_vision,
):
    from types import SimpleNamespace

    from app.services import image_context

    class Storage:
        async def exists(self, _key):
            return True

        async def is_file(self, _key):
            return True

        async def stat(self, _key):
            return SimpleNamespace(size=16)

        async def read_bytes(self, _key):
            return b"\x89PNG\r\n\x1a\nimage"

    primary_client = _FailingClient()
    fallback_client = _FakeClient()
    monkeypatch.setattr(image_context, "get_storage_backend", lambda: Storage())
    monkeypatch.setattr(
        "app.services.llm.caller.create_llm_client",
        lambda **kwargs: primary_client if kwargs["model"] == "primary" else fallback_client,
    )
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *args, **kwargs: 1024)
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda model: "fake-key")
    monkeypatch.setattr("app.services.llm.caller._get_agent_config", AsyncMock(return_value=(3, None)))
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("STATIC", "DYNAMIC")),
    )
    monkeypatch.setattr("app.services.llm.caller.record_token_usage", AsyncMock(return_value=None))

    primary = _FakeModel()
    primary.id = "primary-id"
    primary.model = "primary"
    primary.supports_vision = primary_vision
    fallback = _FakeModel()
    fallback.id = "fallback-id"
    fallback.model = "fallback"
    fallback.supports_vision = fallback_vision
    result = await call_llm_with_failover(
        primary_model=primary,
        fallback_model=fallback,
        messages=[{
            "role": "user",
            "content": "请看图",
            "attachments": [{
                "display_name": "screen.png",
                "path": "workspace/uploads/screen.png",
                "kind": "image",
                "mime_type": "image/png",
            }],
        }],
        agent_name="T",
        role_description="",
        agent_id="agent-x",
        skip_tools=True,
    )

    assert result == "ok-final"
    primary_content = primary_client.stream_calls[0]["messages"][-1].content
    fallback_content = fallback_client.stream_calls[0]["messages"][-1].content
    assert isinstance(primary_content, list) is primary_vision
    assert isinstance(fallback_content, list) is fallback_vision
    for content, supports_vision in [
        (primary_content, primary_vision),
        (fallback_content, fallback_vision),
    ]:
        rendered = str(content)
        assert "workspace/uploads/screen.png" in rendered
        assert ("image_url" in rendered) is supports_vision


@pytest.mark.asyncio
async def test_message_layout_and_tool_sort(monkeypatch):
    fake_client = _FakeClient()

    # Patch all external collaborators the caller touches before ``stream``.
    monkeypatch.setattr(
        "app.services.llm.caller.create_llm_client",
        lambda **kwargs: fake_client,
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_max_tokens",
        lambda *args, **kwargs: 1024,
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_model_api_key",
        lambda model: "fake-key",
    )
    # Config lookup would hit the DB — short-circuit it.
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
        AsyncMock(return_value=("STATIC", "DYN-CONTENT")),
    )
    # get_agent_tools_for_llm is imported at top of caller.py → patch there.
    # Return tools in non-alphabetical order so we can verify the sort.
    tools_raw = [
        {"type": "function", "function": {"name": "write_file",  "description": "w"}},
        {"type": "function", "function": {"name": "read_file",   "description": "r"}},
        {"type": "function", "function": {"name": "grep",        "description": "g"}},
    ]
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(return_value=tools_raw),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.record_token_usage",
        AsyncMock(return_value=None),
    )

    # Conversation history: one earlier user → assistant exchange, then the
    # fresh user turn we're about to send. This exercises the "only wrap the
    # LAST user" rule.
    history = [
        {"role": "user",      "content": "earlier-user-question"},
        {"role": "assistant", "content": "earlier-assistant-reply"},
        {"role": "user",      "content": "current-user-question"},
    ]

    result = await call_llm(
        model=_FakeModel(),
        messages=history,
        agent_name="TestAgent",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="sess-1",
    )

    assert result == "ok-final"
    assert fake_client.closed is True
    assert len(fake_client.stream_calls) == 1

    call = fake_client.stream_calls[0]
    sent: list[LLMMessage] = call["messages"]

    # ── Assertion 1: system has ONLY the static prompt ────────────────────
    assert sent[0].role == "system"
    assert sent[0].content == "STATIC"
    assert "DYN-CONTENT" not in (sent[0].content or "")
    # Assertion 2: no legacy dynamic_content backdoor
    assert sent[0].dynamic_content is None

    # ── Assertion 3: last user wrapped with <context>…</context> ──────────
    last_user = sent[-1]
    assert last_user.role == "user"
    assert isinstance(last_user.content, str)
    assert last_user.content.startswith("<context>\n")
    assert "\n</context>\n\n" in last_user.content
    assert last_user.content.endswith("current-user-question")
    assert "DYN-CONTENT" in last_user.content

    # ── Assertion 4: historical user message stays raw ────────────────────
    # Layout: [system, user(earlier, raw), assistant, user(current, wrapped)]
    earlier_user = sent[1]
    assert earlier_user.role == "user"
    assert earlier_user.content == "earlier-user-question"
    assert "<context>" not in (earlier_user.content or "")

    # ── Assertion 5: tools sorted by function.name ────────────────────────
    tool_names = [t["function"]["name"] for t in call["tools"]]
    assert tool_names == sorted(tool_names)
    assert tool_names == ["grep", "read_file", "write_file"]


@pytest.mark.asyncio
async def test_text_model_dispatch_contains_image_path_without_image_payload(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *args, **kwargs: 1024)
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda model: "fake-key")
    monkeypatch.setattr(
        "app.services.llm.caller._get_agent_config",
        AsyncMock(return_value=(50, None)),
    )
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("STATIC", "DYNAMIC")),
    )
    monkeypatch.setattr("app.services.llm.caller.record_token_usage", AsyncMock(return_value=None))

    result = await call_llm(
        model=_FakeModel(),
        messages=[{
            "role": "user",
            "content": "请处理",
            "attachments": [{
                "display_name": "screen.png",
                "path": "workspace/uploads/screen.png",
                "kind": "image",
                "mime_type": "image/png",
            }],
        }],
        agent_name="TestAgent",
        role_description="",
        agent_id="agent-x",
        skip_tools=True,
    )

    assert result == "ok-final"
    sent = fake_client.stream_calls[0]["messages"]
    user_content = sent[-1].content
    assert isinstance(user_content, str)
    assert "文件名：screen.png" in user_content
    assert "路径：workspace/uploads/screen.png" in user_content
    assert "请处理" in user_content
    assert "image_url" not in user_content
    assert "base64" not in user_content


@pytest.mark.asyncio
async def test_input_history_remains_unmodified(monkeypatch):
    """The ephemeral turn wrapper must never leak into persisted input data."""
    # Client that returns no tool calls -> loop exits after one round; we can
    # still reach into caller state via the dispatched messages snapshot and
    # compare against what the caller would use next round. For that we need
    # a client that captures its inputs.
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "app.services.llm.caller.create_llm_client",
        lambda **kwargs: fake_client,
    )
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *a, **k: 1024)
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda m: "k")
    monkeypatch.setattr(
        "app.services.llm.caller._get_agent_config",
        AsyncMock(return_value=(50, None)),
    )
    monkeypatch.setattr(
        "app.services.llm.caller._get_user_name", AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("STATIC", "DYN")),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.record_token_usage", AsyncMock(return_value=None),
    )

    history = [{"role": "user", "content": "raw-input"}]
    await call_llm(
        model=_FakeModel(),
        messages=history,
        agent_name="T", role_description="",
        agent_id="agent-x", user_id="user-x", session_id="s",
    )

    # Dispatched message had the wrapper…
    sent = fake_client.stream_calls[0]["messages"]
    last_user_sent = sent[-1]
    assert last_user_sent.content.startswith("<context>\n")
    assert last_user_sent.content.endswith("raw-input")

    # …but the original history dict that the caller received is untouched
    # (caller builds LLMMessage copies, never mutates caller-provided list).
    assert history[-1]["content"] == "raw-input"


class _TwoRoundClient(_FakeClient):
    async def stream(self, messages, tools=None, temperature=None, max_tokens=None, **kwargs):
        self.stream_calls.append({
            "messages": list(messages),
            "tools": list(tools) if tools else None,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        if len(self.stream_calls) == 1:
            return LLMResponse(
                content="",
                tool_calls=[{
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": '{"path":"memory/memory.md"}',
                    },
                }],
                finish_reason="tool_calls",
                usage=None,
            )
        return LLMResponse(content="done", finish_reason="stop", usage=None)


@pytest.mark.asyncio
async def test_turn_context_persists_and_prefix_is_append_only_across_tool_rounds(monkeypatch):
    """Every hop sees memory, while round 2 only appends to round 1 bytes."""
    fake_client = _TwoRoundClient()
    monkeypatch.setattr("app.services.llm.caller.create_llm_client", lambda **kwargs: fake_client)
    monkeypatch.setattr("app.services.llm.caller.get_max_tokens", lambda *a, **k: 1024)
    monkeypatch.setattr("app.services.llm.caller.get_model_api_key", lambda m: "k")
    monkeypatch.setattr(
        "app.services.llm.caller._get_agent_config", AsyncMock(return_value=(5, None)),
    )
    monkeypatch.setattr("app.services.llm.caller._get_user_name", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.agent_context.build_agent_context",
        AsyncMock(return_value=("STATIC", "MEMORY-SNAPSHOT")),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.get_agent_tools_for_llm",
        AsyncMock(return_value=[{
            "type": "function",
            "function": {"name": "read_file", "description": "read"},
        }]),
    )
    monkeypatch.setattr(
        "app.services.llm.caller.execute_tool", AsyncMock(return_value="tool-result"),
    )
    monkeypatch.setattr(
        "app.services.llm.caller._persist_tool_call_events_strict",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr("app.services.llm.caller.record_token_usage", AsyncMock(return_value=None))

    history = [{"role": "user", "content": "current-input"}]
    result = await call_llm(
        model=_FakeModel(),
        messages=history,
        agent_name="T",
        role_description="",
        agent_id="agent-x",
        user_id="user-x",
        session_id="",
    )

    assert result == "done"
    assert len(fake_client.stream_calls) == 2
    round_1 = fake_client.stream_calls[0]["messages"]
    round_2 = fake_client.stream_calls[1]["messages"]

    assert "MEMORY-SNAPSHOT" in round_1[-1].content
    wrapped_user_round_2 = next(msg for msg in round_2 if msg.role == "user")
    assert "MEMORY-SNAPSHOT" in wrapped_user_round_2.content
    assert round_2[: len(round_1)] == round_1
    assert [msg.role for msg in round_2[len(round_1) :]] == ["assistant", "tool"]
    assert history == [{"role": "user", "content": "current-input"}]
