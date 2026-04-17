"""Rewritten cli_tool_executor: binary runner, tenant check, schema validation."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.cli_tool_executor import (
    RenderedCommand,
    PreparedPaths,
    _check_access,
    _classify_failure,
    _prepare_paths,
    _render_command,
    _validate_params,
    execute_cli_tool,
)
from app.services.cli_tools.errors import CliToolError, CliToolErrorClass
from app.services.cli_tools.schema import CliToolConfig
from app.services.sandbox.backend import BinaryRunResult


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
    """Stub runner that records `run()` kwargs and returns a fixed result.

    Matches the stateless BinaryRunner contract: no per-tool construction,
    per-call params flow through `run()`.
    """
    runner = MagicMock()
    runner.default_image = "default-image"
    runner.run = AsyncMock(return_value=result or BinaryRunResult(
        exit_code=0, stdout="ok", stderr="", duration_ms=12,
    ))
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

    async def _capture(**kwargs):
        captured.update(kwargs)
        return BinaryRunResult(exit_code=0, stdout="", stderr="", duration_ms=1)

    runner = MagicMock()
    runner.default_image = "default"
    runner.run = _capture

    await execute_cli_tool(
        tool=tool, agent=agent, params={"action": "ping"},
        user_context={"id": "u1", "phone": "13800000000", "email": "u@example.com"},
        storage=_mock_storage(), runner=runner,
    )

    assert captured["args"] == ["u1", "--flag", "ping"]
    # Tool-supplied env_inject entries must be passed through verbatim.
    # The executor also injects `CLAWITH_TRACE_ID` for observability —
    # it's part of the sandbox env contract, so we assert the user keys
    # are preserved rather than the exact dict equality.
    assert captured["env"]["PHONE"] == "13800000000"
    assert captured["env"]["LITERAL"] == "some-static-value"
    # Trace id plumbed through the sandbox so tool authors can stitch
    # their own logs back to the request.
    assert "CLAWITH_TRACE_ID" in captured["env"]
    assert captured["env"]["CLAWITH_TRACE_ID"]  # non-empty


@pytest.mark.asyncio
async def test_executor_mounts_persistent_home_when_configured(tmp_path):
    """persistent_home=True must bind-mount the per-(tool,user) dir."""
    from app.services.cli_tools.state_storage import StateStorage

    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={
            "binary_sha256": "a" * 64,
            "persistent_home": True,
        },
    )
    agent = _agent(tenant)
    state = StateStorage(root=tmp_path)

    captured = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return BinaryRunResult(exit_code=0, stdout="", stderr="", duration_ms=1)

    runner = MagicMock()
    runner.default_image = "default"
    runner.run = _capture

    await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "user-42", "phone": "", "email": ""},
        storage=_mock_storage(), runner=runner, state_storage=state,
    )

    # The runner got a path pointing at the per-(tool,user) subtree, and
    # the directory actually exists on disk.
    home_path = captured["home_host_path"]
    assert home_path is not None
    assert str(tool.id) in home_path
    assert "user-42" in home_path
    assert (tmp_path / str(tenant) / str(tool.id) / "user-42").is_dir()


@pytest.mark.asyncio
async def test_executor_refuses_persistent_home_without_user():
    """Missing user_id with persistent_home=True must not silently share a HOME."""
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={
            "binary_sha256": "a" * 64,
            "persistent_home": True,
        },
    )
    agent = _agent(tenant)

    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "", "phone": "", "email": ""},
            storage=_mock_storage(), runner=_mock_runner(),
        )
    assert exc_info.value.error_class is CliToolErrorClass.VALIDATION_ERROR
    assert "user_id" in exc_info.value.message


@pytest.mark.asyncio
async def test_executor_no_home_mount_when_persistent_home_false():
    """Stateless tools must not get a bind mount (saves disk + simplifies the default)."""
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={"binary_sha256": "a" * 64},  # persistent_home defaults to False
    )
    agent = _agent(tenant)

    captured = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return BinaryRunResult(exit_code=0, stdout="", stderr="", duration_ms=1)

    runner = MagicMock()
    runner.default_image = "default"
    runner.run = _capture

    await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(), runner=runner,
    )

    assert captured["home_host_path"] is None


@pytest.mark.asyncio
async def test_executor_raises_when_home_over_quota(tmp_path):
    """Soft quota: pre-populated HOME over limit must raise VALIDATION_ERROR.

    The runner must not be touched — the refusal happens in _prepare_paths
    before we get anywhere near spawning a container. This is the contract
    admins rely on to stop a runaway tool filling the disk.
    """
    from app.services.cli_tools.state_storage import StateStorage

    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={
            "binary_sha256": "a" * 64,
            "persistent_home": True,
            "home_quota_mb": 1,  # 1 MiB limit
        },
    )
    agent = _agent(tenant)
    state = StateStorage(root=tmp_path)

    # Pre-populate the user's HOME with > 1 MiB so the next run trips the
    # quota. ensure_home creates the leaf; we then drop a 2 MiB blob.
    leaf = state.ensure_home(tenant_id=tenant, tool_id=tool.id, user_id="user-42")
    (leaf / "blob").write_bytes(b"x" * (2 * 1024 * 1024))

    runner = _mock_runner()

    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "user-42", "phone": "", "email": ""},
            storage=_mock_storage(), runner=runner, state_storage=state,
        )
    assert exc_info.value.error_class is CliToolErrorClass.VALIDATION_ERROR
    assert "quota" in exc_info.value.message.lower()
    # Runner must never be invoked — we refuse before spawning.
    runner.run.assert_not_called()


@pytest.mark.asyncio
async def test_executor_raises_rate_limited_error():
    """When the injected RateLimiter denies the call, we raise RATE_LIMITED
    before rendering args / touching the runner."""
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={"binary_sha256": "a" * 64, "rate_limit_per_minute": 5},
    )
    agent = _agent(tenant)

    limiter = MagicMock()
    limiter.check_and_record = AsyncMock(return_value=(False, 5))
    runner = _mock_runner()

    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(), runner=runner,
            rate_limiter=limiter,
        )
    assert exc_info.value.error_class is CliToolErrorClass.RATE_LIMITED
    assert "5/5" in exc_info.value.message
    # Runner must not have been invoked — denial happens before binary run.
    runner.run.assert_not_called()
    # Limiter got the right triple + limit.
    limiter.check_and_record.assert_awaited_once()
    call_args = limiter.check_and_record.await_args
    assert call_args.args[0] == tool.id
    assert call_args.args[1] == agent.id
    assert call_args.args[2] == "u1"
    assert call_args.args[3] == 5


@pytest.mark.asyncio
async def test_executor_skips_limiter_when_limit_is_zero():
    """rate_limit_per_minute=0 bypasses the limiter entirely."""
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        config={"binary_sha256": "a" * 64, "rate_limit_per_minute": 0},
    )
    agent = _agent(tenant)
    limiter = MagicMock()
    limiter.check_and_record = AsyncMock(return_value=(True, 0))

    await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(), runner=_mock_runner(),
        rate_limiter=limiter,
    )
    limiter.check_and_record.assert_not_called()


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

    async def _capture(**kwargs):
        captured.update(kwargs)
        return BinaryRunResult(exit_code=0, stdout="", stderr="", duration_ms=1)

    runner = MagicMock()
    runner.default_image = "default"
    runner.run = _capture

    await execute_cli_tool(
        tool=tool, agent=agent,
        params={"command": ["report", "list", "--env", "dev"]},
        user_context={"id": "u1", "phone": "13800000000", "email": ""},
        storage=_mock_storage(), runner=runner,
    )

    assert captured["args"] == ["report", "list", "--env", "dev"]


# ---------------------------------------------------------------------------
# Helper-level unit tests — exercise each pure step without the full pipeline.
# ---------------------------------------------------------------------------


class TestCheckAccess:
    def test_disabled_tool_raises_permission_denied(self):
        tenant = uuid.uuid4()
        tool = _tool(tenant_id=tenant, config={}, enabled=False)
        agent = _agent(tenant)
        with pytest.raises(CliToolError) as exc_info:
            _check_access(tool, agent)
        assert exc_info.value.error_class is CliToolErrorClass.PERMISSION_DENIED

    def test_cross_tenant_raises_permission_denied(self):
        tool = _tool(tenant_id=uuid.uuid4(), config={})
        agent = _agent(uuid.uuid4())
        with pytest.raises(CliToolError) as exc_info:
            _check_access(tool, agent)
        assert exc_info.value.error_class is CliToolErrorClass.PERMISSION_DENIED

    def test_global_tool_allows_any_tenant(self):
        tool = _tool(tenant_id=None, config={})
        agent = _agent(uuid.uuid4())
        # Must not raise.
        _check_access(tool, agent)

    def test_same_tenant_passes(self):
        tenant = uuid.uuid4()
        tool = _tool(tenant_id=tenant, config={})
        agent = _agent(tenant)
        _check_access(tool, agent)


class TestValidateParams:
    def test_empty_schema_skips_validation(self):
        # Even garbage params must pass when no schema is declared.
        _validate_params({}, {"anything": object()})
        _validate_params(None, {"x": 1})

    def test_valid_params_pass(self):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        }
        _validate_params(schema, {"n": 5})

    def test_invalid_params_raise_validation_error(self):
        schema = {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        }
        with pytest.raises(CliToolError) as exc_info:
            _validate_params(schema, {"n": "not-int"})
        assert exc_info.value.error_class is CliToolErrorClass.VALIDATION_ERROR


class TestRenderCommand:
    def test_resolves_user_and_params_placeholders(self):
        tenant = uuid.uuid4()
        tool = _tool(tenant_id=tenant, config={})
        agent = _agent(tenant)
        config = CliToolConfig.model_validate({
            "binary_sha256": "a" * 64,
            "args_template": ["$user.id", "--flag", "$params.action"],
            "env_inject": {"PHONE": "$user.phone", "LITERAL": "static"},
        })
        cmd = _render_command(
            config,
            tool,
            agent,
            params={"action": "ping"},
            user_context={"id": "u1", "phone": "13800000000", "email": ""},
        )
        assert isinstance(cmd, RenderedCommand)
        assert cmd.args == ["u1", "--flag", "ping"]
        assert cmd.env == {"PHONE": "13800000000", "LITERAL": "static"}

    def test_list_params_expand_into_multiple_argv(self):
        tenant = uuid.uuid4()
        tool = _tool(tenant_id=tenant, config={})
        agent = _agent(tenant)
        config = CliToolConfig.model_validate({
            "binary_sha256": "a" * 64,
            "args_template": ["$params.command"],
        })
        cmd = _render_command(
            config, tool, agent,
            params={"command": ["report", "list"]},
            user_context={"id": "u1", "phone": "", "email": ""},
        )
        assert cmd.args == ["report", "list"]


class TestPreparePaths:
    def test_returns_binary_path_and_no_home_by_default(self):
        tenant = uuid.uuid4()
        tool = _tool(tenant_id=tenant, config={})
        config = CliToolConfig.model_validate({"binary_sha256": "a" * 64})
        storage = _mock_storage(exists=True)
        paths = _prepare_paths(
            config, tool,
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=storage,
            state_storage=None,
        )
        assert isinstance(paths, PreparedPaths)
        assert paths.binary_path  # non-empty (MagicMock str)
        assert paths.home_host_path is None
        storage.resolve.assert_called_once_with(str(tenant), str(tool.id), "a" * 64)

    def test_missing_binary_raises_not_found(self):
        tenant = uuid.uuid4()
        tool = _tool(tenant_id=tenant, config={})
        config = CliToolConfig.model_validate({"binary_sha256": "a" * 64})
        with pytest.raises(CliToolError) as exc_info:
            _prepare_paths(
                config, tool,
                user_context={"id": "u1", "phone": "", "email": ""},
                storage=_mock_storage(exists=False),
                state_storage=None,
            )
        assert exc_info.value.error_class is CliToolErrorClass.NOT_FOUND

    def test_persistent_home_without_user_raises_validation_error(self):
        tenant = uuid.uuid4()
        tool = _tool(tenant_id=tenant, config={})
        config = CliToolConfig.model_validate({
            "binary_sha256": "a" * 64,
            "persistent_home": True,
        })
        with pytest.raises(CliToolError) as exc_info:
            _prepare_paths(
                config, tool,
                user_context={"id": "", "phone": "", "email": ""},
                storage=_mock_storage(exists=True),
                state_storage=None,
            )
        assert exc_info.value.error_class is CliToolErrorClass.VALIDATION_ERROR
        assert "user_id" in exc_info.value.message

    def test_persistent_home_builds_per_user_path(self, tmp_path):
        from app.services.cli_tools.state_storage import StateStorage

        tenant = uuid.uuid4()
        tool = _tool(tenant_id=tenant, config={})
        config = CliToolConfig.model_validate({
            "binary_sha256": "a" * 64,
            "persistent_home": True,
        })
        paths = _prepare_paths(
            config, tool,
            user_context={"id": "user-42", "phone": "", "email": ""},
            storage=_mock_storage(exists=True),
            state_storage=StateStorage(root=tmp_path),
        )
        assert paths.home_host_path is not None
        assert str(tool.id) in paths.home_host_path
        assert "user-42" in paths.home_host_path


class TestClassifyFailure:
    def _config(self):
        return CliToolConfig.model_validate({"binary_sha256": "a" * 64})

    def test_clean_exit_does_not_raise(self):
        # exit_code=0, no flags -> return quietly.
        _classify_failure(
            self._config(),
            BinaryRunResult(exit_code=0, stdout="ok", stderr="", duration_ms=1),
        )

    def test_sandbox_failed_wins_over_other_flags(self):
        # Sandbox-level failures must take precedence even if timed_out is also set.
        with pytest.raises(CliToolError) as exc_info:
            _classify_failure(
                self._config(),
                BinaryRunResult(
                    exit_code=1, stdout="", stderr="", duration_ms=0,
                    sandbox_failed=True, error="image missing", timed_out=True,
                ),
            )
        assert exc_info.value.error_class is CliToolErrorClass.SANDBOX_FAILED

    def test_timeout_maps_to_timeout_error(self):
        with pytest.raises(CliToolError) as exc_info:
            _classify_failure(
                self._config(),
                BinaryRunResult(
                    exit_code=-1, stdout="", stderr="", duration_ms=2100, timed_out=True,
                ),
            )
        assert exc_info.value.error_class is CliToolErrorClass.TIMEOUT

    def test_nonzero_exit_maps_to_binary_failed_with_stderr_tail(self):
        with pytest.raises(CliToolError) as exc_info:
            _classify_failure(
                self._config(),
                BinaryRunResult(exit_code=2, stdout="", stderr="boom", duration_ms=5),
            )
        assert exc_info.value.error_class is CliToolErrorClass.BINARY_FAILED
        assert "boom" in exc_info.value.message


# ---------------------------------------------------------------------------
# Backend selection — executor asks the factory for the cached subprocess
# singleton when the caller didn't inject a runner. The legacy
# `sandbox.backend` config key is silently dropped (v4), so the factory is
# invoked with no arguments regardless of what legacy rows still carry.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_asks_factory_when_no_runner(monkeypatch):
    """When no runner is passed, the executor calls get_sandbox_backend()."""
    from app.services.sandbox.backend import BinaryRunResult as _BRR
    import app.services.cli_tool_executor as mod

    tenant = uuid.uuid4()

    async def _fake_run(**_kw):
        return _BRR(exit_code=0, stdout="ok", stderr="", duration_ms=1)

    fake_runner = MagicMock()
    fake_runner.run = _fake_run

    calls: list[tuple] = []

    def _fake_factory(*args, **kwargs):
        calls.append((args, kwargs))
        return fake_runner

    monkeypatch.setattr(mod, "get_sandbox_backend", _fake_factory)

    # Default config — factory must be called exactly once with no args.
    tool_default = _tool(tenant_id=tenant, config={"binary_sha256": "a" * 64})
    await execute_cli_tool(
        tool=tool_default, agent=_agent(tenant), params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(),
    )
    assert calls == [((), {})]

    # Legacy config carrying dropped sandbox.backend — still works, legacy
    # key is silently stripped and the factory is still called with no args.
    tool_legacy = _tool(
        tenant_id=tenant,
        config={"binary_sha256": "a" * 64, "sandbox": {"backend": "bwrap"}},
    )
    await execute_cli_tool(
        tool=tool_legacy, agent=_agent(tenant), params={},
        user_context={"id": "u2", "phone": "", "email": ""},
        storage=_mock_storage(),
    )
    assert calls == [((), {}), ((), {})]


@pytest.mark.asyncio
async def test_executor_honours_explicit_runner_over_factory(monkeypatch):
    """Passing `runner=...` bypasses the factory entirely (back-compat path).

    Tests and custom stacks construct their own runner; they mustn't get
    a singleton from the global factory injected under their feet.
    """
    import app.services.cli_tool_executor as mod

    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, config={"binary_sha256": "a" * 64})
    agent = _agent(tenant)

    factory_called = []
    def _fake_factory(*args, **kwargs):
        factory_called.append((args, kwargs))
        raise AssertionError("factory should NOT be called when runner is explicit")
    monkeypatch.setattr(mod, "get_sandbox_backend", _fake_factory)

    runner = _mock_runner()
    await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(), runner=runner,
    )
    runner.run.assert_awaited_once()
    assert factory_called == []
