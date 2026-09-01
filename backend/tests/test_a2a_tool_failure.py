"""Failure-path coverage for A2A messaging tool execution."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from a2a_msg_type_support import DummyResult, RecordingDB


@pytest.mark.asyncio
async def test_execute_tool_failure_writes_system_message():
    """execute_tool should write a system error message to the session if a messaging tool fails."""
    from app.services.agent_tools import execute_tool

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    session_id = str(uuid.uuid4())

    tenant_id = uuid.uuid4()
    db = RecordingDB(responses=[
        DummyResult(scalar_value=tenant_id),     # tenant_id
        DummyResult(scalar_value=None),          # query in _send_channel_message (returns empty -> fails)
    ])

    with patch("app.services.agent_tools.async_session") as mock_session_ctx, \
         patch("app.services.activity_logger.log_activity", new_callable=AsyncMock):

        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        args = {
            "member_name": "hi",
            "message": "Hello from Ray",
        }

        result = await execute_tool(
            "send_channel_message",
            args,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
        )

    assert result.startswith("❌")
    assert db.committed
    assert len(db.added) == 1

    error_msg = db.added[0]
    assert error_msg.conversation_id == session_id
    assert error_msg.role == "assistant"
    assert "系统提示" in error_msg.content
    assert "send_channel_message" in error_msg.content
