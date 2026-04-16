"""Execute a CLI tool: tenant check -> schema -> placeholders -> binary runner.

This replaces the pre-M2 executor. The call site in `agent_tools.py` is
updated to pass DB objects rather than raw dicts.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import jsonschema

from app.services.cli_tools.errors import CliToolError, CliToolErrorClass
from app.services.cli_tools.placeholders import PlaceholderContext, resolve, resolve_args
from app.services.cli_tools.schema import CliToolConfig
from app.services.cli_tools.state_storage import StateStorage
from app.services.cli_tools.storage import BinaryStorage
from app.services.sandbox.local.binary_runner import BinaryRunner, BinaryRunResult

logger = logging.getLogger(__name__)


@dataclass
class CliExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


async def execute_cli_tool(
    *,
    tool: Any,
    agent: Any,
    params: Mapping[str, Any],
    user_context: Mapping[str, str],
    storage: BinaryStorage,
    runner: BinaryRunner,
    state_storage: StateStorage | None = None,
) -> CliExecutionResult:
    """Execute `tool` (a Tool ORM row) against `agent` (an Agent ORM row).

    Raises CliToolError with an explicit error_class on any validation,
    permission, or execution failure.
    """
    if not tool.enabled:
        raise CliToolError(CliToolErrorClass.PERMISSION_DENIED, "tool is disabled")

    if tool.tenant_id is not None and tool.tenant_id != agent.tenant_id:
        raise CliToolError(CliToolErrorClass.PERMISSION_DENIED, "tool not available to this tenant")

    # Parse config — add-only schema means older records tolerate default-filled.
    config = CliToolConfig.model_validate(tool.config or {})

    if not config.binary_sha256:
        raise CliToolError(CliToolErrorClass.NOT_FOUND, "tool has no binary uploaded yet")

    schema = dict(tool.parameters_schema or {})
    if schema:
        try:
            jsonschema.validate(instance=dict(params), schema=schema)
        except jsonschema.ValidationError as exc:
            raise CliToolError(CliToolErrorClass.VALIDATION_ERROR, exc.message) from exc

    # Preserve the native type of each param: a list-typed `$params.X`
    # token expands into multiple argv entries, which is how multi-segment
    # CLIs like `svc report list` or `git commit -m ...` are driven.
    ctx = PlaceholderContext(
        user=dict(user_context),
        agent={"id": str(agent.id)},
        tenant={"id": str(agent.tenant_id) if agent.tenant_id else ""},
        params=dict(params),
    )

    rendered_args = resolve_args(list(config.args_template), ctx)
    rendered_env = {k: resolve(v, ctx) for k, v in config.env_inject.items()}

    tenant_key = str(tool.tenant_id) if tool.tenant_id is not None else "_global"
    binary_path = storage.resolve(tenant_key, str(tool.id), config.binary_sha256)
    if not binary_path.is_file():
        raise CliToolError(
            CliToolErrorClass.NOT_FOUND,
            f"binary {config.binary_sha256[:12]}... missing on disk",
        )

    # Persistent HOME: (tenant, tool, user) scoped rw directory. Required
    # for svc-style tools that cache login tokens. Without a user_id we
    # refuse rather than silently letting everyone share a HOME — that
    # would be a data-leak vector for any tool relying on cached auth.
    home_host_path: str | None = None
    if config.persistent_home:
        user_id = user_context.get("id") if user_context else None
        if not user_id:
            raise CliToolError(
                CliToolErrorClass.VALIDATION_ERROR,
                "tool requires persistent HOME but no user_id is available in the request context",
            )
        store = state_storage or StateStorage()
        home_host_path = str(store.ensure_home(
            tenant_id=tool.tenant_id,
            tool_id=tool.id,
            user_id=user_id,
        ))

    # Build a per-execute runner with the tool's own sandbox overrides.
    configured_runner = runner.__class__(
        image=config.sandbox.image or runner.image,
        cpu_limit=config.sandbox.cpu_limit,
        memory_limit=config.sandbox.memory_limit,
        network=config.sandbox.network,
    )

    result: BinaryRunResult = await configured_runner.run(
        binary_host_path=str(binary_path),
        args=rendered_args,
        env=rendered_env,
        timeout_seconds=config.timeout_seconds,
        home_host_path=home_host_path,
    )

    logger.info(
        "cli_tool.executed",
        extra={
            "tool_id": str(tool.id),
            "agent_id": str(agent.id),
            "tenant_id": str(agent.tenant_id) if agent.tenant_id else None,
            "binary_sha256": config.binary_sha256,
            "duration_ms": result.duration_ms,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "sandbox_failed": result.sandbox_failed,
        },
    )

    if result.sandbox_failed:
        raise CliToolError(CliToolErrorClass.SANDBOX_FAILED, result.error)
    if result.timed_out:
        raise CliToolError(CliToolErrorClass.TIMEOUT, "binary exceeded timeout_seconds")
    if result.exit_code != 0:
        tail = result.stderr[-200:] if result.stderr else ""
        raise CliToolError(
            CliToolErrorClass.BINARY_FAILED,
            f"exit={result.exit_code}; stderr={tail}",
        )

    return CliExecutionResult(
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=result.duration_ms,
    )
