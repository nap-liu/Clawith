"""Unit tests for the shared tool_call → LLM message expansion.

``expand_tool_call_row`` is the single source of truth that both the web
WebSocket path and the IM channels use to replay persisted ``tool_call``
rows back into OpenAI-format ``assistant`` + ``tool`` message pairs. These
tests pin the exact field mapping (1:1 with the historical websocket.py
behaviour) and the dual-schema tolerance for legacy Feishu rows.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from app.services.chat_history import expand_tool_call_row


def _row(content: str, mid: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(role="tool_call", content=content, id=mid or uuid.uuid4())


def test_expand_web_schema_produces_assistant_and_tool_pair():
    mid = uuid.uuid4()
    row = _row(
        json.dumps(
            {
                "name": "read_file",
                "args": {"path": "a.txt"},
                "status": "done",
                "result": "file contents",
                "reasoning_content": "let me read it",
            }
        ),
        mid,
    )

    out = expand_tool_call_row(row)

    assert len(out) == 2
    asst, tool = out
    assert asst["role"] == "assistant"
    assert asst["content"] is None
    assert asst["tool_calls"][0]["id"] == f"call_{mid}"
    assert asst["tool_calls"][0]["type"] == "function"
    assert asst["tool_calls"][0]["function"]["name"] == "read_file"
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"path": "a.txt"}
    assert asst["reasoning_content"] == "let me read it"
    assert tool["role"] == "tool"
    assert tool["tool_call_id"] == f"call_{mid}"
    assert "file contents" in tool["content"]


def test_expand_pending_confirmation_emits_placeholder_result():
    """A request_confirmation tool_call still awaiting the user (status='pending',
    empty result) must replay as assistant(tool_call) + tool(placeholder) — every
    tool_call needs a paired result for strict providers, and the placeholder tells
    the model the user hasn't responded yet rather than feeding it an empty string."""
    mid = uuid.uuid4()
    row = _row(
        json.dumps(
            {
                "name": "request_confirmation",
                "args": {"title": "删库确认", "summary": "..."},
                "status": "pending",
                "result": "",
            }
        ),
        mid,
    )

    out = expand_tool_call_row(row)

    assert len(out) == 2
    asst, tool = out
    assert asst["tool_calls"][0]["function"]["name"] == "request_confirmation"
    assert tool["role"] == "tool"
    assert tool["tool_call_id"] == f"call_{mid}"
    # Not an empty string — a concrete "still pending" marker.
    assert tool["content"].strip() != ""
    assert "未响应" in tool["content"] or "未决" in tool["content"]


def test_expand_legacy_feishu_schema_is_tolerated():
    """Legacy Feishu rows stored tool_name/arguments instead of name/args."""
    row = _row(
        json.dumps(
            {
                "tool_name": "send_message",
                "arguments": {"to": "bob"},
                "status": "done",
                "result": "sent",
            }
        )
    )

    out = expand_tool_call_row(row)

    assert len(out) == 2
    asst, tool = out
    assert asst["tool_calls"][0]["function"]["name"] == "send_message"
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"to": "bob"}
    assert tool["content"] == "sent"


def test_expand_without_reasoning_omits_field():
    row = _row(json.dumps({"name": "noop", "args": {}, "result": ""}))

    out = expand_tool_call_row(row)

    assert "reasoning_content" not in out[0]


def test_expand_malformed_json_returns_empty():
    row = _row("{not valid json")

    assert expand_tool_call_row(row) == []


def test_display_parse_web_schema():
    from app.services.chat_history import parse_tool_call_for_display

    out = parse_tool_call_for_display(
        json.dumps(
            {
                "name": "read_file",
                "call_id": "call-123",
                "args": {"path": "a"},
                "status": "done",
                "result": "r",
                "reasoning_content": "think",
            }
        )
    )
    assert out["toolName"] == "read_file"
    assert out["toolArgs"] == {"path": "a"}
    assert out["toolStatus"] == "done"
    assert out["toolCallId"] == "call-123"
    assert out["toolResult"] == "r"
    assert out["toolThinking"] == "think"


def test_display_parse_legacy_feishu_schema():
    """Historical Feishu rows stored tool_name/arguments — must still render."""
    from app.services.chat_history import parse_tool_call_for_display

    out = parse_tool_call_for_display(
        json.dumps({"tool_name": "send_msg", "arguments": {"to": "x"}, "status": "done", "result": "ok"})
    )
    assert out["toolName"] == "send_msg"
    assert out["toolArgs"] == {"to": "x"}
    assert out["toolResult"] == "ok"


def test_display_parse_malformed_returns_empty_dict():
    from app.services.chat_history import parse_tool_call_for_display

    assert parse_tool_call_for_display("{bad json") == {}


def test_strip_leading_orphan_tool_messages_drops_orphan():
    """A context-window slice can cut a tool-call pair, leaving the history
    starting with a role='tool' that has no preceding tool_calls. LLM APIs
    reject that, so leading orphan tool messages must be dropped."""
    from app.services.chat_history import strip_leading_orphan_tool_messages

    msgs = [
        {"role": "tool", "tool_call_id": "x", "content": "orphan result"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "q"},
    ]
    out = strip_leading_orphan_tool_messages(msgs)
    assert [m["role"] for m in out] == ["assistant", "user"]


def test_strip_leading_orphan_tool_messages_multiple():
    from app.services.chat_history import strip_leading_orphan_tool_messages

    msgs = [
        {"role": "tool", "content": "a"},
        {"role": "tool", "content": "b"},
        {"role": "user", "content": "q"},
    ]
    assert strip_leading_orphan_tool_messages(msgs) == [{"role": "user", "content": "q"}]


def test_strip_leading_orphan_tool_messages_noop_when_valid():
    """A well-formed history (no leading tool) is returned unchanged."""
    from app.services.chat_history import strip_leading_orphan_tool_messages

    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "r"},
    ]
    assert strip_leading_orphan_tool_messages(msgs) == msgs


def _msg_row(role, content, *, user_id=None, thinking=None):
    return SimpleNamespace(role=role, content=content, id=uuid.uuid4(), user_id=user_id, thinking=thinking)


def test_build_messages_expands_tool_call_and_keeps_plain():
    """The shared rows→messages builder expands tool_call rows and passes
    plain user/assistant rows through as {role, content}."""
    from app.services.chat_history import build_llm_messages_from_rows

    rows = [
        _msg_row("user", "hi"),
        _msg_row("tool_call", json.dumps({"name": "f", "args": {"a": 1}, "result": "r"})),
        _msg_row("assistant", "ok"),
    ]
    out = build_llm_messages_from_rows(rows)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "assistant"]
    assert out[0]["content"] == "hi"
    assert out[1]["tool_calls"][0]["function"]["name"] == "f"
    assert out[2]["content"] == "r"
    assert out[3]["content"] == "ok"


def test_build_messages_group_wraps_user_only():
    """With wrap_user_names + name_map, only user rows get the sender prefix."""
    from app.services.chat_history import build_llm_messages_from_rows

    uid = uuid.uuid4()
    rows = [_msg_row("user", "q", user_id=uid), _msg_row("assistant", "a", user_id=uid)]
    out = build_llm_messages_from_rows(rows, wrap_user_names=True, name_map={uid: "Alice"})
    assert out[0]["content"].startswith('<sender id="')
    assert "Alice" in out[0]["content"] and out[0]["content"].endswith("q")
    assert out[1]["content"] == "a"  # assistant never wrapped


def test_build_messages_thinking_only_when_requested():
    """thinking is carried only when include_thinking is set (web replays it,
    IM history does not)."""
    from app.services.chat_history import build_llm_messages_from_rows

    rows = [_msg_row("assistant", "a", thinking="reasoned")]
    assert "thinking" not in build_llm_messages_from_rows(rows)[0]
    assert build_llm_messages_from_rows(rows, include_thinking=True)[0]["thinking"] == "reasoned"
