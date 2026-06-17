"""D5: Integration tests verifying _execute_code threads cli_injection through
to backend.execute correctly.

Strategy:
- patch build_cli_injection to return a fixed sentinel dict
- patch get_sandbox_backend to return a mock backend (AsyncMock.execute)
- patch get_sandbox_config and _get_tool_config to return minimal stubs
- call _execute_code and assert:
    * execute_code_aio + bash/node/python → inject == sentinel dict
    * execute_code (local) → inject is None
    * _execute_cli_tool passes cli_injection= to _execute_code
    * returns None for non-CLI / error for missing command / error for unavailable tool
"""
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The canonical injection dict shape returned by build_cli_injection.
_INJECTION_DICT = {
    "env": {"YYBPC_CLI_USER_PHONE": "1"},
    "wrappers": [{"name": "svc", "binary_path": "/x"}],
}


class _FakeResult:
    pass


class _FakeSandboxConfig:
    """Minimal stub so SandboxConfig.from_dict/get_sandbox_backend won't fail.

    Mirrors the real SandboxConfig fields that _execute_code reads directly:
    - max_timeout (added upstream 395f3fa6) — used in min(requested_timeout, ...)
    - allow_network (added upstream b31b9778) — read on the legacy fallback path
    """
    enabled = True
    type = "aio_sandbox"
    api_url = "http://fake"
    api_key = ""
    max_timeout = 60
    allow_network = False


def _make_mock_backend(result_sentinel=""):
    """Return a mock backend whose execute() returns a _FakeResult and
    whose _format_result() returns the given string."""
    backend = MagicMock()
    fake_result = _FakeResult()
    backend.execute = AsyncMock(return_value=fake_result)
    backend._format_result = MagicMock(return_value=result_sentinel)
    return backend


@pytest.mark.asyncio
async def test_execute_code_aio_bash_receives_inject(tmp_path):
    """execute_code_aio + bash: inject dict must be threaded through to backend.execute."""
    from app.services.agent_tools import _execute_code

    agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    mock_backend = _make_mock_backend()

    with (
        patch(
            "app.services.agent_tools.build_cli_injection",
            new=AsyncMock(return_value=_INJECTION_DICT),
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
    assert call_kwargs.get("inject") == _INJECTION_DICT, (
        f"expected inject={_INJECTION_DICT!r}, got {call_kwargs.get('inject')!r}"
    )


@pytest.mark.asyncio
async def test_execute_code_aio_node_receives_inject(tmp_path):
    """execute_code_aio + node: inject dict must also be threaded through."""
    from app.services.agent_tools import _execute_code

    mock_backend = _make_mock_backend()

    with (
        patch(
            "app.services.agent_tools.build_cli_injection",
            new=AsyncMock(return_value=_INJECTION_DICT),
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
    assert call_kwargs.get("inject") == _INJECTION_DICT


@pytest.mark.asyncio
async def test_execute_code_aio_python_receives_inject(tmp_path):
    """execute_code_aio + python: inject dict is also passed (python prelude handles it)."""
    from app.services.agent_tools import _execute_code

    mock_backend = _make_mock_backend()

    with (
        patch(
            "app.services.agent_tools.build_cli_injection",
            new=AsyncMock(return_value=_INJECTION_DICT),
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
    assert call_kwargs.get("inject") == _INJECTION_DICT, (
        f"python must receive inject dict, got {call_kwargs.get('inject')!r}"
    )


@pytest.mark.asyncio
async def test_execute_code_local_no_inject(tmp_path):
    """execute_code (local/subprocess) must NOT receive inject (inject=None)."""
    from app.services.agent_tools import _execute_code

    mock_backend = _make_mock_backend()

    with (
        patch(
            "app.services.agent_tools.build_cli_injection",
            new=AsyncMock(return_value=_INJECTION_DICT),
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
    assert call_kwargs.get("inject") is None, (
        f"local execute_code must pass inject=None, got {call_kwargs.get('inject')!r}"
    )


@pytest.mark.asyncio
async def test_execute_code_cli_injection_override_used_directly(tmp_path):
    """cli_injection kwarg is passed straight through — no build_cli_injection call."""
    from app.services.agent_tools import _execute_code

    mock_backend = _make_mock_backend()
    build_mock = AsyncMock(return_value={"env": {}, "wrappers": []})
    override_inject = {"env": {"X": "Y"}, "wrappers": [{"name": "svc", "binary_path": "/y"}]}

    with (
        patch("app.services.agent_tools.build_cli_injection", new=build_mock),
        patch("app.services.sandbox.registry.get_sandbox_backend", return_value=mock_backend),
        patch("app.config.get_sandbox_config", return_value=_FakeSandboxConfig()),
        patch("app.services.agent_tools._get_tool_config", new=AsyncMock(return_value=None)),
    ):
        await _execute_code(
            None,
            tmp_path,
            {"language": "bash", "code": "svc report list"},
            tool_name="execute_code_aio",
            user_id=None,
            cli_injection=override_inject,
        )

    build_mock.assert_not_called()  # override skips the second DB round-trip
    assert mock_backend.execute.call_args.kwargs.get("inject") == override_inject


@pytest.mark.asyncio
async def test_execute_cli_tool_runs_command_in_aio_with_single_inject(tmp_path):
    """_execute_cli_tool runs the bash command line in aio with single-tool inject."""
    from app.services import agent_tools

    seen = {}

    async def fake_exec_code(agent_id, ws, arguments, *, tool_name, user_id, cli_injection=None, session_id=None):
        seen.update(arguments=arguments, tool_name=tool_name, cli_injection=cli_injection)
        return "OUTPUT"

    with (
        patch("app.services.agent_tools.build_cli_injection", new=AsyncMock(return_value=_INJECTION_DICT)),
        patch("app.services.agent_tools._execute_code", new=fake_exec_code),
    ):
        out = await agent_tools._execute_cli_tool(
            None, tmp_path, "svc", {"command": "svc report list | jq '.[0]'"}, user_id=None
        )

    assert out == "OUTPUT"
    assert seen["arguments"] == {"language": "bash", "code": "svc report list | jq '.[0]'"}
    assert seen["tool_name"] == "execute_code_aio"
    # The prebuilt injection dict must be passed through as cli_injection=
    assert seen["cli_injection"] == _INJECTION_DICT


@pytest.mark.asyncio
async def test_execute_cli_tool_returns_none_when_not_a_cli_tool(tmp_path):
    """Unknown / non-CLI tool name → None so the dispatcher falls through to MCP."""
    from app.services import agent_tools

    with (
        patch("app.services.agent_tools.build_cli_injection", new=AsyncMock(return_value=None)),
        patch("app.services.agent_tools._is_cli_tool_name", new=AsyncMock(return_value=False)),
    ):
        out = await agent_tools._execute_cli_tool(
            None, tmp_path, "not_a_cli", {"command": "x"}, user_id=None
        )
    assert out is None


@pytest.mark.asyncio
async def test_execute_cli_tool_errors_when_cli_tool_unavailable(tmp_path):
    """A surfaced CLI tool that can't be built (binary gone / transient error)
    returns a clear error — NOT None — so it isn't masked as 'Unknown tool'."""
    from app.services import agent_tools

    with (
        patch("app.services.agent_tools.build_cli_injection", new=AsyncMock(return_value=None)),
        patch("app.services.agent_tools._is_cli_tool_name", new=AsyncMock(return_value=True)),
    ):
        out = await agent_tools._execute_cli_tool(
            None, tmp_path, "svc", {"command": "svc report list"}, user_id=None
        )
    assert out is not None and "unavailable" in out.lower() and "svc" in out


@pytest.mark.asyncio
async def test_execute_cli_tool_missing_command_errors(tmp_path):
    """A CLI tool called without `command` returns a clear error (not None)."""
    from app.services import agent_tools

    with patch("app.services.agent_tools.build_cli_injection", new=AsyncMock(return_value=_INJECTION_DICT)):
        out = await agent_tools._execute_cli_tool(
            None, tmp_path, "svc", {}, user_id=None
        )
    assert out is not None and "command" in out.lower()
