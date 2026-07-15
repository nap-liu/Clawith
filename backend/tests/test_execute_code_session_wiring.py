"""Wiring tests: the ChatSession id travels execute_tool → _execute_code →
backend.execute(conversation_id=...) so the sandbox can isolate per-session.

Same patching strategy as test_execute_code_inject_wiring.py: mock the sandbox
backend and assert on the kwargs that reach backend.execute.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeSandboxConfig:
    # max_timeout (upstream 395f3fa6) + allow_network (upstream b31b9778) are read
    # directly by _execute_code; the stub must carry them or the read raises
    # AttributeError before backend.execute is ever called.
    enabled = True
    type = "aio_sandbox"
    api_url = "http://fake"
    api_key = ""
    max_timeout = 60
    allow_network = False


def _make_mock_backend():
    backend = MagicMock()
    backend.execute = AsyncMock(return_value=MagicMock())
    backend.start_background_job = AsyncMock(
        return_value={"success": True, "status": "running", "job_id": "job_123"}
    )
    backend.manage_background_jobs = AsyncMock(
        return_value={"success": True, "status": "ok", "jobs": []}
    )
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
async def test_background_execute_uses_same_session_and_injection(tmp_path):
    from app.services.agent_tools import _execute_code

    agent_id = uuid.uuid4()
    injection = {"wrappers": [{"name": "svc", "binary_path": "/x"}]}
    mock_backend = _make_mock_backend()
    p1, p2, p3, p4 = _patches(mock_backend)
    with p1, p2, p3, p4:
        await _execute_code(
            agent_id,
            tmp_path,
            {
                "language": "bash",
                "code": "svc auth wait",
                "execution_mode": "background",
                "timeout": 120,
            },
            tool_name="execute_code_aio",
            user_id=uuid.uuid4(),
            cli_injection=injection,
            session_id="conv-background",
        )

    kwargs = mock_backend.start_background_job.call_args.kwargs
    assert kwargs["agent_id"] == str(agent_id)
    assert kwargs["conversation_id"] == "conv-background"
    assert kwargs["inject"] == injection
    assert kwargs["timeout"] == 120
    mock_backend.execute.assert_not_called()


@pytest.mark.asyncio
async def test_job_management_is_scoped_to_current_session(tmp_path):
    from app.services.agent_tools import _execute_code

    agent_id = uuid.uuid4()
    mock_backend = _make_mock_backend()
    p1, p2, p3, p4 = _patches(mock_backend)
    with p1, p2, p3, p4:
        await _execute_code(
            agent_id,
            tmp_path,
            {"action": "job_logs", "job_id": "job_123", "tail_lines": 25},
            tool_name="execute_code_aio",
            session_id="conv-manage",
        )

    kwargs = mock_backend.manage_background_jobs.call_args.kwargs
    assert kwargs["agent_id"] == str(agent_id)
    assert kwargs["conversation_id"] == "conv-manage"
    assert kwargs["job_id"] == "job_123"
    assert kwargs["tail_lines"] == 25


@pytest.mark.asyncio
async def test_dispatch_code_exec_passes_session_id(tmp_path):
    """execute_tool's code-exec branch must forward its session_id.

    Merge-interface notes:
    - ensure_workspace was removed in the v1.10 storage refactor; the dispatch no
      longer resolves a workspace directly. The _CODE_EXEC branch now wraps the
      runner in _run_with_temp_workspace(...). We stub that wrapper so it just
      invokes the runner with a tmp dir (its real contract: runner(temp.root)),
      keeping the test off the DB/FS while still exercising the dispatch's
      runner-construction — which is where session_id must be threaded.
    """
    from app.services import agent_tools

    captured = {}

    async def fake_execute_code(agent_id, ws, arguments, *, tool_name="", user_id=None, session_id=None, **kw):
        captured["session_id"] = session_id
        return "ok"

    async def fake_run_with_temp_ws(agent_id, tenant_id, runner, *, paths=None, sync_back=False):
        return await runner(tmp_path)

    with (
        patch.object(agent_tools, "_execute_code", new=fake_execute_code),
        patch.object(agent_tools, "_run_with_temp_workspace", new=fake_run_with_temp_ws),
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
