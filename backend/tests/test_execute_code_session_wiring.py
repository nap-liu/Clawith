"""Wiring tests: the ChatSession id travels execute_tool → _execute_code →
backend.execute(conversation_id=...) so the sandbox can isolate per-session.

Same patching strategy as test_execute_code_inject_wiring.py: mock the sandbox
backend and assert on the kwargs that reach backend.execute.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeSandboxConfig:
    enabled = True
    type = "aio_sandbox"
    api_url = "http://fake"
    api_key = ""


def _make_mock_backend():
    backend = MagicMock()
    backend.execute = AsyncMock(return_value=MagicMock())
    backend._format_result = MagicMock(return_value="ok")
    return backend


def _patches(mock_backend):
    return (
        patch(
            "app.services.agent_tools.build_cli_injection",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.sandbox.registry.get_sandbox_backend",
            return_value=mock_backend,
        ),
        patch(
            "app.config.get_sandbox_config",
            return_value=_FakeSandboxConfig(),
        ),
        patch(
            "app.services.agent_tools._get_tool_config",
            new=AsyncMock(return_value=None),
        ),
    )


@pytest.mark.asyncio
async def test_execute_code_threads_session_id_as_conversation_id(tmp_path):
    from app.services.agent_tools import _execute_code

    mock_backend = _make_mock_backend()
    p1, p2, p3, p4 = _patches(mock_backend)
    with p1, p2, p3, p4:
        await _execute_code(
            uuid.uuid4(),
            tmp_path,
            {"language": "bash", "code": "echo hi"},
            tool_name="execute_code_aio",
            user_id=uuid.uuid4(),
            session_id="conv-123",
        )

    assert mock_backend.execute.call_args.kwargs.get("conversation_id") == "conv-123"


@pytest.mark.asyncio
async def test_execute_code_without_session_passes_none(tmp_path):
    from app.services.agent_tools import _execute_code

    mock_backend = _make_mock_backend()
    p1, p2, p3, p4 = _patches(mock_backend)
    with p1, p2, p3, p4:
        await _execute_code(
            uuid.uuid4(),
            tmp_path,
            {"language": "bash", "code": "echo hi"},
            tool_name="execute_code_aio",
            user_id=uuid.uuid4(),
        )

    assert mock_backend.execute.call_args.kwargs.get("conversation_id") is None


@pytest.mark.asyncio
async def test_dispatch_code_exec_passes_session_id(tmp_path):
    """execute_tool's code-exec branch must forward its session_id."""
    from app.services import agent_tools

    captured = {}

    async def fake_execute_code(agent_id, ws, arguments, *, tool_name="", user_id=None, session_id=None, **kw):
        captured["session_id"] = session_id
        return "ok"

    with (
        patch.object(agent_tools, "_execute_code", new=fake_execute_code),
        patch.object(agent_tools, "ensure_workspace", new=AsyncMock(return_value=tmp_path)),
        patch.object(agent_tools, "_get_agent_tenant_id", new=AsyncMock(return_value=None)),
        # Keep the test off the DB: skip the autonomy check and activity log.
        patch.dict(agent_tools._TOOL_AUTONOMY_MAP, clear=True),
        patch("app.services.activity_logger.log_activity", new=AsyncMock()),
    ):
        await agent_tools.execute_tool(
            "execute_code_aio",
            {"language": "bash", "code": "echo hi"},
            agent_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            session_id="conv-456",
        )

    assert captured.get("session_id") == "conv-456"


@pytest.mark.asyncio
async def test_cli_tool_threads_session_id(tmp_path):
    """_execute_cli_tool reuses _execute_code and must forward session_id."""
    from app.services import agent_tools

    captured = {}

    async def fake_execute_code(agent_id, ws, arguments, *, tool_name="", user_id=None, session_id=None, **kw):
        captured["session_id"] = session_id
        return "ok"

    injection = {"env": {}, "wrappers": [{"name": "svc", "binary_path": "/x"}]}
    with (
        patch.object(agent_tools, "_execute_code", new=fake_execute_code),
        patch.object(
            agent_tools, "build_cli_injection", new=AsyncMock(return_value=injection)
        ),
    ):
        await agent_tools._execute_cli_tool(
            uuid.uuid4(),
            tmp_path,
            "svc",
            {"command": "svc report query"},
            user_id=uuid.uuid4(),
            session_id="conv-789",
        )

    assert captured.get("session_id") == "conv-789"
