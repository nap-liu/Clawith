"""Sanitization is an OUTPUT-BOUNDARY concern, not a storage concern.

Root cause of the sql_execute "Unsupported database type" loop + empty reply:
``persist_tool_call`` masked ``connection_string`` to ``******`` at WRITE time,
and that same masked history was replayed back into the LLM context — so the
model faithfully copied ``connection_string: "******"`` into new tool calls,
which fail the ``mysql://`` prefix check forever.

The fix: store tool_call args RAW (the single source of truth the model needs),
and mask only at the human-facing read boundaries:
  - ``parse_tool_call_for_display`` (web messages API + WS reload)
  - the live WS broadcast
LLM replay (``expand_tool_call_row``) must see the RAW connection string.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from app.services.chat_history import (
    expand_tool_call_row,
    parse_tool_call_for_display,
    persist_tool_call,
)

_RAW_CONN = "mysql://liuxi:NqJ2yry01U1C0qu%23@fe-c-714913dbe928ddca-internal.starrocks.aliyuncs.com:9030/"


def _row(content: str, mid: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(role="tool_call", content=content, id=mid or uuid.uuid4())


class _FakeSession:
    def __init__(self, captured: list):
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self._captured.append(obj)

    async def commit(self):
        pass


def _factory(captured: list):
    def make():
        return _FakeSession(captured)

    return make


# ─── Storage stays RAW (what the model needs) ───────────────────────────────


async def test_persist_tool_call_stores_connection_string_raw():
    captured: list = []
    await persist_tool_call(
        _factory(captured),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id="conv-1",
        evt={
            "name": "sql_execute",
            "args": {"connection_string": _RAW_CONN, "sql": "SHOW DATABASES"},
            "status": "done",
            "result": "ok",
        },
    )
    assert len(captured) == 1, "expected exactly one ChatMessage to be persisted"
    stored = json.loads(captured[0].content)
    # The model replays this verbatim — it MUST be the real connection string.
    assert stored["args"]["connection_string"] == _RAW_CONN
    assert stored["args"]["sql"] == "SHOW DATABASES"


async def test_expand_tool_call_row_feeds_raw_connection_string_to_llm():
    content = json.dumps(
        {
            "name": "sql_execute",
            "args": {"connection_string": _RAW_CONN, "sql": "SHOW DATABASES"},
            "status": "done",
            "result": "ok",
        }
    )
    asst, _tool = expand_tool_call_row(_row(content))
    args = json.loads(asst["tool_calls"][0]["function"]["arguments"])
    assert args["connection_string"] == _RAW_CONN


# ─── Display/output boundary MASKS (what humans see) ────────────────────────


def test_parse_tool_call_for_display_masks_connection_string():
    content = json.dumps(
        {
            "name": "sql_execute",
            "args": {"connection_string": _RAW_CONN, "sql": "SHOW DATABASES"},
            "status": "done",
            "result": "ok",
        }
    )
    out = parse_tool_call_for_display(content)
    assert out["toolArgs"]["connection_string"] == "******"
    # Non-sensitive args are untouched.
    assert out["toolArgs"]["sql"] == "SHOW DATABASES"
    # The raw secret must not leak through the display payload.
    assert "NqJ2yry01U1C0qu" not in json.dumps(out, ensure_ascii=False)
