"""Guardrail test: tool_call rows replayed from history must keep RAW args.

This locks the correct behavior that prevents the sanitize-poisoning bug:
  persist_tool_call stores args RAW → expand_tool_call_row replays them RAW
  → the LLM sees the real connection_string, not "******"
"""

import json
import uuid

from app.services.chat_history import expand_tool_call_row


class _Row:
    def __init__(self, content):
        self.id = uuid.uuid4()
        self.role = "tool_call"
        self.content = content
        self.thinking = None
        self.user_id = uuid.uuid4()


def test_tool_call_replay_keeps_raw_connection_string():
    real = "mysql://u:p@h:3306/db"
    row = _Row(json.dumps({
        "name": "sql_execute",
        "args": {"connection_string": real, "sql": "SELECT 1"},
        "status": "done",
        "result": "ok",
    }))
    msgs = expand_tool_call_row(row)
    asst = next(m for m in msgs if m["role"] == "assistant")
    args_replayed = asst["tool_calls"][0]["function"]["arguments"]
    assert real in args_replayed
    assert "******" not in args_replayed
