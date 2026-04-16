"""Execute a CLI tool: tenant check -> schema -> placeholders -> binary runner.

This replaces the pre-M2 executor. The call site in `agent_tools.py` is
updated to pass DB objects rather than raw dicts.

Structure: `execute_cli_tool` is a thin orchestrator. Each step is a
pure helper so that the control flow is greppable and individual steps
are unit-testable without spinning up a runner.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping
from uuid import uuid4

import jsonschema

from app.core.logging_config import get_trace_id
from app.services.cli_tools.errors import CliToolError, CliToolErrorClass
from app.services.cli_tools.metrics import (
    OUTCOME_INTERNAL_ERROR,
    OUTCOME_OK,
    record_execution,
)
from app.services.cli_tools.placeholders import PlaceholderContext, resolve, resolve_args
from app.services.cli_tools.rate_limiter import RateLimiter
from app.services.cli_tools.schema import CliToolConfig
from app.services.cli_tools.state_storage import StateStorage
from app.services.cli_tools.storage import BinaryStorage
from app.services.sandbox.backend import SandboxBackend
from app.services.sandbox.factory import get_sandbox_backend
from app.services.sandbox.local.binary_runner import BinaryRunResult

logger = logging.getLogger(__name__)


@dataclass
class CliExecutionResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    # Correlation id for this run. Copied from the request-scoped
    # contextvar (TraceIdMiddleware) or synthesized for background calls,
    # and injected into the sandbox as `CLAWITH_TRACE_ID`. The Task E
    # audit writer reads this off the result so the audit row can be
    # stitched against logs and the per-run Prometheus counter.
    trace_id: str = ""


@dataclass(frozen=True)
class CliExecutionAudit:
    """Audit payload for one CLI tool execution.

    Delivered to the caller through an `audit_sink` callback from the
    executor's `finally` block so that both success and failure paths
    produce exactly one audit row. Deliberately decoupled from the DB:
    the executor stays pure and the caller decides how / where to
    persist (see spec §5.4 "每次 execute 都写 AuditLog").

    PII defense:
        * `args_hash` is sha256(rendered_args)[:16]; we never store the
          rendered argv because `$user.phone` / `$params.*` can carry
          real phone numbers, tokens, or free-text prompts.
        * `stdout_len` records size only. Tool stdout is user-facing and
          unconstrained — storing the body in the audit log would be an
          unbounded PII / secrets exposure.
        * `stderr_tail` is the last 200 bytes for triage. Stderr is for
          the tool author to write diagnostics, much lower PII risk than
          stdout, and the 200-byte cap makes accidental dumps cheap.
    """

    tool_id: str
    tool_name: str
    tenant_id: str | None
    agent_id: str
    user_id: str | None
    args_hash: str
    args_len: int
    outcome: str
    exit_code: int | None
    duration_ms: int
    stderr_tail: str
    stdout_len: int
    trace_id: str | None


AuditSink = Callable[[CliExecutionAudit], Awaitable[None]]


def _hash_args(rendered_args: list[str] | None) -> tuple[str, int]:
    """Return (sha256[:16], len) for a rendered argv.

    Pre-render failure paths (schema / permission / rate-limit) have no
    argv yet, so `rendered_args=None` produces ("", 0). That distinguishes
    "no args computed" from "computed to empty list".
    """
    if rendered_args is None:
        return "", 0
    blob = json.dumps(rendered_args, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16], len(rendered_args)


@dataclass(frozen=True)
class RenderedCommand:
    """Output of placeholder rendering: argv + environment, both fully resolved."""

    args: list[str]
    env: dict[str, str]


@dataclass(frozen=True)
class PreparedPaths:
    """Filesystem handles needed by the runner: binary location and optional HOME mount."""

    binary_path: str
    home_host_path: str | None


def _check_access(tool: Any, agent: Any) -> None:
    """Reject disabled tools and cross-tenant access.

    Global tools (tenant_id=None) are allowed for any tenant; tenant-scoped
    tools must match the agent's tenant exactly.
    """
    if not tool.enabled:
        raise CliToolError(CliToolErrorClass.PERMISSION_DENIED, "tool is disabled")
    if tool.tenant_id is not None and tool.tenant_id != agent.tenant_id:
        raise CliToolError(
            CliToolErrorClass.PERMISSION_DENIED,
            "tool not available to this tenant",
        )


def _validate_params(parameters_schema: Any, params: Mapping[str, Any]) -> None:
    """Run JSON-Schema validation on `params` if the tool declares a schema.

    An empty / missing schema skips validation entirely — agents without
    a parameters contract are allowed to pass arbitrary data through.
    """
    schema = dict(parameters_schema or {})
    if not schema:
        return
    try:
        jsonschema.validate(instance=dict(params), schema=schema)
    except jsonschema.ValidationError as exc:
        raise CliToolError(CliToolErrorClass.VALIDATION_ERROR, exc.message) from exc


async def _check_rate_limit(
    config: CliToolConfig,
    tool: Any,
    agent: Any,
    user_context: Mapping[str, str],
    rate_limiter: RateLimiter | None,
) -> None:
    """Enforce the per-(tool, agent, user) sliding-window rate limit.

    `rate_limit_per_minute=0` is the no-op fast path and never touches
    Redis. Otherwise we call into `RateLimiter.check_and_record`, which is
    fail-open: Redis outages log a warning and allow the call rather than
    breaking every CLI tool on the platform.
    """
    if config.rate_limit_per_minute <= 0:
        return

    limiter = rate_limiter
    if limiter is None:
        # Lazy-import to keep rate limiting optional: if the caller
        # explicitly passes a limiter (tests, custom stacks) we never
        # touch the global redis client, and unit tests with mocked
        # redis don't need the real one to be configured.
        from app.core.events import get_redis
        limiter = RateLimiter(await get_redis())

    user_id = (user_context.get("id") if user_context else "") or "_anonymous"
    allowed, count = await limiter.check_and_record(
        tool.id,
        agent.id,
        user_id,
        config.rate_limit_per_minute,
    )
    if not allowed:
        raise CliToolError(
            CliToolErrorClass.RATE_LIMITED,
            f"rate limit exceeded: {count}/{config.rate_limit_per_minute} per minute",
        )


def _render_command(
    config: CliToolConfig,
    tool: Any,
    agent: Any,
    params: Mapping[str, Any],
    user_context: Mapping[str, str],
) -> RenderedCommand:
    """Resolve `$user.*` / `$agent.*` / `$tenant.*` / `$params.*` placeholders.

    List-typed `$params.X` tokens in `args_template` expand in place
    (multi-segment CLIs like `svc report list`). `env_inject` values are
    always scalars; list-typed params are JSON-dumped by `resolve()`.
    """
    ctx = PlaceholderContext(
        user=dict(user_context),
        agent={"id": str(agent.id)},
        tenant={"id": str(agent.tenant_id) if agent.tenant_id else ""},
        params=dict(params),
    )
    rendered_args = resolve_args(list(config.args_template), ctx)
    rendered_env = {k: resolve(v, ctx) for k, v in config.env_inject.items()}
    return RenderedCommand(args=rendered_args, env=rendered_env)


def _prepare_paths(
    config: CliToolConfig,
    tool: Any,
    user_context: Mapping[str, str],
    storage: BinaryStorage,
    state_storage: StateStorage | None,
) -> PreparedPaths:
    """Resolve the on-disk binary path and (if requested) the persistent HOME.

    The persistent HOME is scoped to (tenant, tool, user). We refuse to
    proceed when `persistent_home=True` but no `user_id` is available —
    silently sharing a HOME across users would leak cached auth tokens.
    """
    tenant_key = str(tool.tenant_id) if tool.tenant_id is not None else "_global"
    binary_path = storage.resolve(tenant_key, str(tool.id), config.binary_sha256)
    if not binary_path.is_file():
        raise CliToolError(
            CliToolErrorClass.NOT_FOUND,
            f"binary {config.binary_sha256[:12]}... missing on disk",
        )

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

        # Soft disk quota: if the HOME has already grown past the limit,
        # refuse the run rather than letting a runaway tool fill the
        # whole cli_state volume. Only meaningful for persistent_home;
        # ephemeral /tmp HOMEs are wiped after every call and can't
        # accumulate. `os.walk` over a live HOME is sub-ms for typical
        # tool caches (tokens, a few config files) — we skip caching to
        # keep the code obvious; revisit if profiling says otherwise.
        if config.home_quota_mb > 0:
            within, current_bytes = store.check_quota(
                tenant_id=tool.tenant_id,
                tool_id=tool.id,
                user_id=user_id,
                limit_mb=config.home_quota_mb,
            )
            if not within:
                used_mb = current_bytes // (1024 * 1024)
                raise CliToolError(
                    CliToolErrorClass.VALIDATION_ERROR,
                    f"home quota exceeded: {used_mb}MB used, "
                    f"limit {config.home_quota_mb}MB. "
                    f"Admin must clear the cache before new runs.",
                )

    return PreparedPaths(binary_path=str(binary_path), home_host_path=home_host_path)


def _classify_failure(config: CliToolConfig, result: BinaryRunResult) -> None:
    """Raise the matching `CliToolError` for any non-success runner outcome.

    Order matters: sandbox failures (infra-level) take precedence over
    timeouts, which take precedence over non-zero exits. A clean exit
    (exit_code=0, no failures) returns without raising.
    """
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


async def execute_cli_tool(
    *,
    tool: Any,
    agent: Any,
    params: Mapping[str, Any],
    user_context: Mapping[str, str],
    storage: BinaryStorage,
    runner: SandboxBackend | None = None,
    state_storage: StateStorage | None = None,
    rate_limiter: RateLimiter | None = None,
    audit_sink: AuditSink | None = None,
) -> CliExecutionResult:
    """Execute `tool` (a Tool ORM row) against `agent` (an Agent ORM row).

    Raises CliToolError with an explicit error_class on any validation,
    permission, or execution failure.

    Observability contract (do not regress):
        * One Prometheus counter + histogram update per call, in `finally`,
          so success and every failure path are counted exactly once.
        * Labels are intentionally low-cardinality: `tool_name`, `tenant_id`,
          `outcome`. Never add `agent_id` or `user_id`.
        * The request's trace id is propagated into the sandbox env as
          `CLAWITH_TRACE_ID` and surfaced on the result so callers /
          audit writers can stitch logs together. Background callers
          with no active trace context get a fresh uuid4 rather than
          "unknown".
        * If `audit_sink` is provided, it is awaited once in `finally`
          with a `CliExecutionAudit` payload covering this run. The
          executor stays DB-free; the caller (api endpoint / agent_tools
          dispatch) owns the session and writes the AuditLog row. A
          sink exception is logged and swallowed — audit must not break
          tool execution for the user.
    """
    # Label values captured up front so the `finally` block still has them
    # even if we raise before `config` is parsed. `tool_name` falls back
    # to the tool's id (still bounded) when the row has no name.
    tool_name_label = str(getattr(tool, "name", None) or getattr(tool, "id", "unknown"))
    tenant_id_label = str(tool.tenant_id) if tool.tenant_id is not None else "_global"

    # Prefer the request-scoped trace id from TraceIdMiddleware; fall back
    # to a short uuid for background / scheduled invocations. We do NOT
    # build a generic tracing framework here — that's an infra PR.
    trace_id = get_trace_id() or str(uuid4())[:12]

    start = time.monotonic()
    # Pessimistic default: any path that exits without setting `outcome`
    # (e.g. an unexpected exception) is classified as internal_error so
    # the bug cannot hide from the counter.
    outcome = OUTCOME_INTERNAL_ERROR
    # Captured across try/finally so the audit payload can be assembled
    # regardless of which branch we exit through. `rendered_args` stays
    # None on pre-render failures (hash will be empty); `result` stays
    # None until the binary actually runs.
    rendered_args: list[str] | None = None
    result: BinaryRunResult | None = None
    try:
        _check_access(tool, agent)

        # Add-only schema means older records tolerate default-filled fields.
        config = CliToolConfig.model_validate(tool.config or {})
        if not config.binary_sha256:
            raise CliToolError(CliToolErrorClass.NOT_FOUND, "tool has no binary uploaded yet")

        _validate_params(tool.parameters_schema, params)
        # Rate limit runs after validation (so bogus calls don't burn a slot)
        # and before rendering (so we don't do the string work for rejected
        # calls, and so denial happens before any downstream disk / Docker
        # operation could start).
        await _check_rate_limit(config, tool, agent, user_context, rate_limiter)
        cmd = _render_command(config, tool, agent, params, user_context)
        rendered_args = cmd.args
        # Inject the trace id into the sandbox env so tool authors can
        # echo it on stderr / their own telemetry and stitch back into
        # the request. The CLAWITH_ prefix avoids collisions with any
        # tool-supplied env var.
        cmd.env["CLAWITH_TRACE_ID"] = trace_id

        # Egress allowlist pass-through. When a non-empty list is set we
        # expose the hostnames to the sandboxed binary as a comma-separated
        # `CLAWITH_EGRESS_ALLOWLIST` env var.
        #
        # IMPORTANT: this is *pass-through only*, not kernel-level
        # enforcement. A cooperative CLI can read this variable and stay
        # within bounds; a hostile or LLM-controlled binary can still
        # `connect()` anywhere the backend permits. Real enforcement
        # (tinyproxy sidecar + nftables) is tracked in
        # docs/superpowers/TODO-egress-enforcement.md. The schema field
        # is the stable integration point — when enforcement lands it
        # will consume the same list with no schema change.
        if config.sandbox.egress_allowlist:
            cmd.env["CLAWITH_EGRESS_ALLOWLIST"] = ",".join(
                config.sandbox.egress_allowlist
            )
        paths = _prepare_paths(config, tool, user_context, storage, state_storage)

        # Pick the sandbox backend. Callers that pass `runner` (tests,
        # custom stacks) keep full control; the common path gets a
        # singleton chosen by tool config. The factory is cached so
        # this is a dict lookup after the first call.
        effective_runner = runner if runner is not None else get_sandbox_backend(config.sandbox.backend)

        # Single stateless runner serves every tool — per-call overrides
        # travel with run() arguments, no per-tool instantiation.
        result = await effective_runner.run(
            binary_host_path=paths.binary_path,
            args=cmd.args,
            env=cmd.env,
            timeout_seconds=config.timeout_seconds,
            home_host_path=paths.home_host_path,
            image=config.sandbox.image,
            cpu_limit=config.sandbox.cpu_limit,
            memory_limit=config.sandbox.memory_limit,
            network=config.sandbox.network,
        )

        logger.info(
            "cli_tool.executed",
            extra={
                "trace_id": trace_id,
                "tool_id": str(tool.id),
                "tool_name": tool_name_label,
                "agent_id": str(agent.id),
                "tenant_id": str(agent.tenant_id) if agent.tenant_id else None,
                "binary_sha256": config.binary_sha256,
                "duration_ms": result.duration_ms,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "sandbox_failed": result.sandbox_failed,
            },
        )

        _classify_failure(config, result)

        outcome = OUTCOME_OK
        return CliExecutionResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            trace_id=trace_id,
        )
    except CliToolError as exc:
        # Known, classified failure → split it out as a distinct outcome
        # (timeout vs binary_failed vs sandbox_failed vs …) so ops can
        # distinguish "Docker is sick" from "tool exited non-zero" in
        # Prometheus without parsing logs.
        outcome = exc.error_class.value.lower()
        raise
    except Exception:
        # Anything else is still a failed execution from the caller's
        # perspective and MUST be counted — otherwise an executor bug
        # can silently drop calls out of the dashboards.
        outcome = OUTCOME_INTERNAL_ERROR
        raise
    finally:
        # Single place that updates metrics + delivers audit, regardless
        # of the path out of the function. Kept in `finally` so no raise
        # path can skip either of them.
        elapsed_seconds = time.monotonic() - start
        record_execution(
            tool_name=tool_name_label,
            tenant_id=tenant_id_label,
            outcome=outcome,
            duration_seconds=elapsed_seconds,
        )
        if audit_sink is not None:
            args_hash, args_len = _hash_args(rendered_args)
            # Prefer real runner numbers when we got that far; fall back
            # to the wall-clock from our `start` so failed pre-runner
            # paths still report a non-zero duration.
            duration_ms = (
                result.duration_ms if result is not None else int(elapsed_seconds * 1000)
            )
            exit_code = result.exit_code if result is not None else None
            stderr_tail = (result.stderr[-200:] if result and result.stderr else "")
            stdout_len = len(result.stdout) if result and result.stdout else 0
            user_id_raw = user_context.get("id") if user_context else None
            audit = CliExecutionAudit(
                tool_id=str(getattr(tool, "id", "")),
                tool_name=tool_name_label,
                tenant_id=str(tool.tenant_id) if tool.tenant_id is not None else None,
                agent_id=str(getattr(agent, "id", "")),
                user_id=str(user_id_raw) if user_id_raw else None,
                args_hash=args_hash,
                args_len=args_len,
                outcome=outcome,
                exit_code=exit_code,
                duration_ms=duration_ms,
                stderr_tail=stderr_tail,
                stdout_len=stdout_len,
                trace_id=trace_id,
            )
            try:
                await audit_sink(audit)
            except Exception:
                # Audit MUST NOT break tool execution. A sink failure is
                # a compliance incident to flag at the logging layer, not
                # a reason to drop the user's result / raise a new error.
                logger.exception(
                    "cli_tool.audit_sink_failed",
                    extra={
                        "trace_id": trace_id,
                        "tool_id": str(getattr(tool, "id", "")),
                        "tool_name": tool_name_label,
                    },
                )
