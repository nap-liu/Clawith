from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.websocket import WebSocketChatHandler


pytestmark = pytest.mark.asyncio


class _AsyncSessionCtx:
    async def __aenter__(self):
        return SimpleNamespace()

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def test_websocket_tool_call_uses_single_canonical_writer(monkeypatch):
    """A WS tool call marker must create exactly one canonical history row.

    The canonical writer is ``persist_tool_call``. Calling the legacy
    ``save_tool_call_log`` as well writes a second ``role='tool_call'`` row for
    the same event, which bloats replay history and can trip provider-side
    repeated-tool-call guards.
    """
    persist_tool_call = AsyncMock()
    save_tool_call_log = AsyncMock()
    maybe_mark_read = AsyncMock(return_value=True)

    monkeypatch.setattr("app.services.chat_history.persist_tool_call", persist_tool_call)
    monkeypatch.setattr("app.services.chat_session_service.save_tool_call_log", save_tool_call_log)
    monkeypatch.setattr("app.api.websocket.maybe_mark_session_read_for_active_viewer", maybe_mark_read)
    monkeypatch.setattr("app.api.websocket.async_session", lambda: _AsyncSessionCtx())

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    conversation_id = str(uuid.uuid4())
    handler = WebSocketChatHandler(
        websocket=SimpleNamespace(),
        agent_id=agent_id,
        token="test-token",
        session_id=conversation_id,
    )
    handler.user = SimpleNamespace(id=user_id)
    handler.conv_id = conversation_id

    evt = {
        "name": "read_file",
        "call_id": "call_1",
        "args": {"path": "a.txt"},
        "status": "done",
        "result": "file contents",
        "reasoning_content": "need file",
    }

    await handler._save_tool_call_to_db(evt)

    persist_tool_call.assert_awaited_once()
    _, kwargs = persist_tool_call.await_args
    assert kwargs == {
        "agent_id": agent_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "evt": evt,
        "turn_anchor_id": None,
    }
    save_tool_call_log.assert_not_awaited()
    maybe_mark_read.assert_awaited_once()
