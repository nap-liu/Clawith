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
