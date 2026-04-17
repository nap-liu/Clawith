"""Prometheus metrics for CLI tool executions.

Label cardinality is deliberately constrained:
    - tool_name: bounded (one per Tool row — finite, operator-managed)
    - tenant_id: bounded (one per Tenant row — finite, operator-managed)
    - outcome:   bounded enum (one of the strings defined below)

Do NOT add `agent_id` or `user_id` — those are effectively unbounded and
would blow up Prometheus cardinality in production.

`outcome` is always the lowercased value of `CliToolErrorClass` (e.g.
"binary_failed", "timeout", "sandbox_failed", "validation_error",
"permission_denied", "not_found", "resource_limit"), plus two synthetic
outcomes produced inside the executor:
    - "ok"             — successful execution
    - "internal_error" — unexpected exception that was not a CliToolError
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

# Counter: total executions labeled by (tool_name, tenant_id, outcome).
cli_tool_executions_total = Counter(
    "clawith_cli_tool_executions_total",
    "CLI tool executions, labeled by tool and outcome",
    ["tool_name", "tenant_id", "outcome"],
)

# Histogram: wall-clock time from executor entry to result (success or
# failure). Buckets span 50 ms → 60 s because individual binary runs
# usually land in the 100 ms–5 s range and we want long-tail visibility.
cli_tool_execution_duration_seconds = Histogram(
    "clawith_cli_tool_execution_duration_seconds",
    "Wall-clock time from executor entry to result",
    ["tool_name"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)


# Outcome values — kept as module-level constants so callers and tests
# can reference them by name instead of stringly-typing.
OUTCOME_OK = "ok"
OUTCOME_INTERNAL_ERROR = "internal_error"


def record_execution(
    *,
    tool_name: str,
    tenant_id: str,
    outcome: str,
    duration_seconds: float,
) -> None:
    """Helper used by the executor to keep the call-site compact.

    Isolates the two-metric update so any future backend swap (e.g.
    OpenTelemetry) only touches this module.
    """
    cli_tool_executions_total.labels(
        tool_name=tool_name, tenant_id=tenant_id, outcome=outcome
    ).inc()
    cli_tool_execution_duration_seconds.labels(tool_name=tool_name).observe(
        duration_seconds
    )
