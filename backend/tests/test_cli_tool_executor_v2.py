"""Rewritten cli_tool_executor: binary runner, tenant check, schema validation."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.cli_tool_executor import execute_cli_tool
from app.services.cli_tools.errors import CliToolError, CliToolErrorClass
from app.services.sandbox.local.binary_runner import BinaryRunResult


def _tool(*, tenant_id, config, parameters_schema=None, enabled=True):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.tenant_id = tenant_id
    t.enabled = enabled
    t.config = config
    t.parameters_schema = parameters_schema or {}
    return t


def _agent(tenant_id):
    a = MagicMock()
    a.id = uuid.uuid4()
    a.tenant_id = tenant_id
    return a


def _mock_storage(exists: bool = True) -> MagicMock:
    storage = MagicMock()
    path = MagicMock()
    path.is_file.return_value = exists
    storage.resolve.return_value = path
    return storage


def _mock_runner(result: BinaryRunResult | None = None) -> MagicMock:
    runner = MagicMock()
    runner.image = "default-image"
    runner.run = AsyncMock(return_value=result or BinaryRunResult(
        exit_code=0, stdout="ok", stderr="", duration_ms=12,
    ))
    # make runner.__class__(...) callable with the same signature
    class _RunnerClass:
        def __init__(self, image, **kwargs):
            self.image = image
            self.kwargs = kwargs

        async def run(self, **kwargs):
            return result or BinaryRunResult(exit_code=0, stdout="ok", stderr="", duration_ms=12)

    runner.__class__ = _RunnerClass
    return runner


@pytest.mark.asyncio
async def test_executor_rejects_tenant_mismatch():
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    tool = _tool(tenant_id=tenant_a, config={"binary_sha256": "a" * 64})
    agent = _agent(tenant_b)

    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(), runner=_mock_runner(),
        )
    assert exc_info.value.error_class is CliToolErrorClass.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_executor_allows_global_tool_cross_tenant():
    tool = _tool(tenant_id=None, config={"binary_sha256": "a" * 64})
    agent = _agent(uuid.uuid4())

    result = await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(),
        runner=_mock_runner(BinaryRunResult(
            exit_code=0, stdout="ok", stderr="", duration_ms=12,
        )),
    )
    assert result.exit_code == 0
    assert result.stdout == "ok"


@pytest.mark.asyncio
async def test_executor_rejects_disabled_tool():
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, config={"binary_sha256": "a" * 64}, enabled=False)
    agent = _agent(tenant)
    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(), runner=_mock_runner(),
        )
    assert exc_info.value.error_class is CliToolErrorClass.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_executor_validates_params_against_schema():
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={"binary_sha256": "a" * 64},
        parameters_schema={
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
    )
    agent = _agent(tenant)
    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={"n": "not-int"},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(), runner=_mock_runner(),
        )
    assert exc_info.value.error_class is CliToolErrorClass.VALIDATION_ERROR


@pytest.mark.asyncio
async def test_executor_maps_timeout_to_error_class():
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, config={"binary_sha256": "a" * 64, "timeout_seconds": 2})
    agent = _agent(tenant)
    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(),
            runner=_mock_runner(BinaryRunResult(
                exit_code=-1, stdout="", stderr="", duration_ms=2100, timed_out=True,
            )),
        )
    assert exc_info.value.error_class is CliToolErrorClass.TIMEOUT


@pytest.mark.asyncio
async def test_executor_maps_nonzero_exit_to_binary_failed():
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, config={"binary_sha256": "a" * 64})
    agent = _agent(tenant)
    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(),
            runner=_mock_runner(BinaryRunResult(
                exit_code=2, stdout="", stderr="boom", duration_ms=10,
            )),
        )
    assert exc_info.value.error_class is CliToolErrorClass.BINARY_FAILED
    assert "boom" in exc_info.value.message


@pytest.mark.asyncio
async def test_executor_maps_sandbox_failure():
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, config={"binary_sha256": "a" * 64})
    agent = _agent(tenant)
    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(),
            runner=_mock_runner(BinaryRunResult(
                exit_code=1, stdout="", stderr="", duration_ms=0,
                sandbox_failed=True, error="image missing",
            )),
        )
    assert exc_info.value.error_class is CliToolErrorClass.SANDBOX_FAILED


@pytest.mark.asyncio
async def test_executor_reports_not_found_when_binary_missing():
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, config={"binary_sha256": "a" * 64})
    agent = _agent(tenant)
    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(exists=False),
            runner=_mock_runner(),
        )
    assert exc_info.value.error_class is CliToolErrorClass.NOT_FOUND


@pytest.mark.asyncio
async def test_executor_resolves_args_and_env_placeholders():
    """`$user.phone` / `$params.n` style tokens are resolved wholesale."""
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={
            "binary_sha256": "a" * 64,
            "args_template": ["$user.id", "--flag", "$params.action"],
            "env_inject": {"PHONE": "$user.phone", "LITERAL": "some-static-value"},
        },
    )
    agent = _agent(tenant)

    captured = {}

    class _CapturingRunner:
        def __init__(self, image, **kwargs):
            self.image = image

        async def run(self, **kwargs):
            captured.update(kwargs)
            return BinaryRunResult(exit_code=0, stdout="", stderr="", duration_ms=1)

    runner = MagicMock()
    runner.image = "default"
    runner.__class__ = _CapturingRunner

    await execute_cli_tool(
        tool=tool, agent=agent, params={"action": "ping"},
        user_context={"id": "u1", "phone": "13800000000", "email": "u@example.com"},
        storage=_mock_storage(), runner=runner,
    )

    assert captured["args"] == ["u1", "--flag", "ping"]
    assert captured["env"] == {"PHONE": "13800000000", "LITERAL": "some-static-value"}


@pytest.mark.asyncio
async def test_executor_expands_list_params_into_argv():
    """List params must expand in place so agents can drive multi-segment CLIs.

    Regression: with scalar-only substitution, an agent calling
    `svc` with `command="report list"` produced a single argv
    `"report list"` that svc (Commander.js) rejects as
    `unknown command 'report list'`. The fix is to let `$params.command`
    resolve to a list and expand in-template.
    """
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={
            "binary_sha256": "a" * 64,
            # A single placeholder that becomes multiple argv entries.
            "args_template": ["$params.command"],
        },
        parameters_schema={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["command"],
        },
    )
    agent = _agent(tenant)

    captured = {}

    class _CapturingRunner:
        def __init__(self, image, **kwargs):
            self.image = image

        async def run(self, **kwargs):
            captured.update(kwargs)
            return BinaryRunResult(exit_code=0, stdout="", stderr="", duration_ms=1)

    runner = MagicMock()
    runner.image = "default"
    runner.__class__ = _CapturingRunner

    await execute_cli_tool(
        tool=tool, agent=agent,
        params={"command": ["report", "list", "--env", "dev"]},
        user_context={"id": "u1", "phone": "13800000000", "email": ""},
        storage=_mock_storage(), runner=runner,
    )

    assert captured["args"] == ["report", "list", "--env", "dev"]
