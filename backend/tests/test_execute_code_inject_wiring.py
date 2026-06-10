"""D5: Integration tests verifying _execute_code threads inject_prefix through
to backend.execute correctly.

Strategy:
- patch build_cli_inject_prefix to return a fixed sentinel
- patch get_sandbox_backend to return a mock backend (AsyncMock.execute)
- patch get_sandbox_config and _get_tool_config to return minimal stubs
- call _execute_code and assert:
    * execute_code_aio + bash/node  → inject_prefix == sentinel
    * execute_code (local) + bash   → inject_prefix is None
    * execute_code_aio + python     → inject_prefix is None (python doesn't get prefix)
"""
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _FakeResult:
    pass


class _FakeSandboxConfig:
    """Minimal stub so SandboxConfig.from_dict/get_sandbox_backend won't fail."""
    enabled = True
    type = "aio_sandbox"
    api_url = "http://fake"
    api_key = ""


def _make_mock_backend(result_sentinel=""):
    """Return a mock backend whose execute() returns a _FakeResult and
    whose _format_result() returns the given string."""
    backend = MagicMock()
    fake_result = _FakeResult()
    backend.execute = AsyncMock(return_value=fake_result)
    backend._format_result = MagicMock(return_value=result_sentinel)
    return backend


@pytest.mark.asyncio
async def test_execute_code_aio_bash_receives_inject_prefix(tmp_path):
    """execute_code_aio + bash: inject_prefix must be threaded through."""
    from app.services.agent_tools import _execute_code

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    mock_backend = _make_mock_backend()

    with (
        patch(
            "app.services.agent_tools.build_cli_inject_prefix",
            new=AsyncMock(return_value="PREFIX_MARK"),
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
    ):
        await _execute_code(
            agent_id,
            tmp_path,
            {"language": "bash", "code": "echo hi"},
            tool_name="execute_code_aio",
            user_id=user_id,
        )

    mock_backend.execute.assert_called_once()
    call_kwargs = mock_backend.execute.call_args.kwargs
    assert call_kwargs.get("inject_prefix") == "PREFIX_MARK", (
        f"expected inject_prefix='PREFIX_MARK', got {call_kwargs.get('inject_prefix')!r}"
    )


@pytest.mark.asyncio
async def test_execute_code_aio_node_receives_inject_prefix(tmp_path):
    """execute_code_aio + node: inject_prefix must also be threaded through."""
    from app.services.agent_tools import _execute_code

    mock_backend = _make_mock_backend()

    with (
        patch(
            "app.services.agent_tools.build_cli_inject_prefix",
            new=AsyncMock(return_value="PREFIX_MARK"),
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
    ):
        await _execute_code(
            None,
            tmp_path,
            {"language": "node", "code": "console.log(1)"},
            tool_name="execute_code_aio",
            user_id=None,
        )

    call_kwargs = mock_backend.execute.call_args.kwargs
    assert call_kwargs.get("inject_prefix") == "PREFIX_MARK"


@pytest.mark.asyncio
async def test_execute_code_local_bash_no_inject_prefix(tmp_path):
    """execute_code (local/subprocess) must NOT receive inject_prefix."""
    from app.services.agent_tools import _execute_code

    mock_backend = _make_mock_backend()

    with (
        patch(
            "app.services.agent_tools.build_cli_inject_prefix",
            new=AsyncMock(return_value="SHOULD_NOT_APPEAR"),
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
    ):
        await _execute_code(
            None,
            tmp_path,
            {"language": "bash", "code": "echo hi"},
            tool_name="execute_code",  # <-- local tool, not aio
            user_id=None,
        )

    call_kwargs = mock_backend.execute.call_args.kwargs
    assert call_kwargs.get("inject_prefix") is None, (
        f"local execute_code must pass inject_prefix=None, got {call_kwargs.get('inject_prefix')!r}"
    )


@pytest.mark.asyncio
async def test_execute_code_aio_python_no_inject_prefix(tmp_path):
    """execute_code_aio + python: inject_prefix must be None (python is not a CLI shell)."""
    from app.services.agent_tools import _execute_code

    mock_backend = _make_mock_backend()

    with (
        patch(
            "app.services.agent_tools.build_cli_inject_prefix",
            new=AsyncMock(return_value="SHOULD_NOT_APPEAR"),
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
    ):
        await _execute_code(
            None,
            tmp_path,
            {"language": "python", "code": "print(1)"},
            tool_name="execute_code_aio",
            user_id=None,
        )

    call_kwargs = mock_backend.execute.call_args.kwargs
    assert call_kwargs.get("inject_prefix") is None, (
        f"python must not receive inject_prefix, got {call_kwargs.get('inject_prefix')!r}"
    )
