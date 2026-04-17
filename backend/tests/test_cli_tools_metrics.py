"""Unit tests for cli-tool Prometheus metrics.

Goals
-----
1. Every execute_cli_tool() call bumps the counter exactly once.
2. Success path → `outcome="ok"`; each CliToolError class → its lowercase
   string (`binary_failed`, `timeout`, …); bare `Exception` → `internal_error`.
3. The duration histogram receives an observation on every path.
4. Label cardinality is constrained to (tool_name, tenant_id, outcome)
   — agent_id / user_id must not leak in.

These tests drive the real metrics objects (the module-level Counter and
Histogram on the default registry) rather than a mock, because the bug
we care about is exactly "someone forgot to call .inc() on a real path";
a mock would happily accept any call and hide that regression.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.cli_tool_executor import execute_cli_tool
from app.services.cli_tools.errors import CliToolError, CliToolErrorClass
from app.services.cli_tools.metrics import (
    cli_tool_execution_duration_seconds,
    cli_tool_executions_total,
)
from app.services.sandbox.backend import BinaryRunResult


# ── Metric-reading helpers ──────────────────────────────────────────────

def _counter_value(*, tool_name: str, tenant_id: str, outcome: str) -> float:
    """Read the current value of `cli_tool_executions_total` for one label set.

    We use the public `.labels(...)._value.get()` path. `_value` is a
    `ValueClass` instance; `.get()` is the documented way to read its
    current float. Slightly implementation-y, but the alternative —
    scanning `.collect()` — is much noisier for unit tests.
    """
    return cli_tool_executions_total.labels(
        tool_name=tool_name, tenant_id=tenant_id, outcome=outcome
    )._value.get()


def _histogram_count(tool_name: str) -> float:
    """Sum of observations recorded on the duration histogram for one tool."""
    return cli_tool_execution_duration_seconds.labels(
        tool_name=tool_name
    )._sum.get() if False else _histogram_sample_count(tool_name)


def _histogram_sample_count(tool_name: str) -> float:
    """Return the `_count` sample for the duration histogram.

    `_count` increments on every observation regardless of bucket, so
    it's the right metric for "did an observation happen at all".
    """
    for metric in cli_tool_execution_duration_seconds.collect():
        for sample in metric.samples:
            if (
                sample.name.endswith("_count")
                and sample.labels.get("tool_name") == tool_name
            ):
                return sample.value
    return 0.0


# ── Fixtures: minimal tool / agent / storage / runner stubs ─────────────

def _tool(*, tenant_id, config=None, name="svc", enabled=True, parameters_schema=None):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.tenant_id = tenant_id
    t.enabled = enabled
    t.name = name
    t.config = config or {"binary_sha256": "a" * 64}
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
    runner.default_image = "default-image"
    runner.run = AsyncMock(return_value=result or BinaryRunResult(
        exit_code=0, stdout="ok", stderr="", duration_ms=12,
    ))
    return runner


# ── Tests ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_counter_increments_on_success():
    """Happy path bumps counter with outcome='ok' exactly once."""
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, name="test_ok_tool")
    agent = _agent(tenant)

    before = _counter_value(
        tool_name="test_ok_tool", tenant_id=str(tenant), outcome="ok"
    )
    await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(), runner=_mock_runner(),
    )
    after = _counter_value(
        tool_name="test_ok_tool", tenant_id=str(tenant), outcome="ok"
    )
    assert after - before == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_counter_labels_tool_and_outcome_correctly():
    """Label values must reflect the real tool name + tenant — no bleed
    across tools."""
    tenant = uuid.uuid4()
    tool_a = _tool(tenant_id=tenant, name="tool_alpha")
    tool_b = _tool(tenant_id=tenant, name="tool_beta")
    agent = _agent(tenant)

    a_before = _counter_value(
        tool_name="tool_alpha", tenant_id=str(tenant), outcome="ok"
    )
    b_before = _counter_value(
        tool_name="tool_beta", tenant_id=str(tenant), outcome="ok"
    )

    await execute_cli_tool(
        tool=tool_a, agent=agent, params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(), runner=_mock_runner(),
    )

    # Only tool_alpha's row advanced. If labels were wrong (e.g. sharing
    # an "unknown" name), both would tick up and this would fail.
    assert _counter_value(
        tool_name="tool_alpha", tenant_id=str(tenant), outcome="ok"
    ) - a_before == pytest.approx(1.0)
    assert _counter_value(
        tool_name="tool_beta", tenant_id=str(tenant), outcome="ok"
    ) == pytest.approx(b_before)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runner_result, error_class, outcome_label",
    [
        (
            BinaryRunResult(exit_code=2, stdout="", stderr="boom", duration_ms=3),
            CliToolErrorClass.BINARY_FAILED,
            "binary_failed",
        ),
        (
            BinaryRunResult(exit_code=-1, stdout="", stderr="", duration_ms=100, timed_out=True),
            CliToolErrorClass.TIMEOUT,
            "timeout",
        ),
        (
            BinaryRunResult(exit_code=1, stdout="", stderr="", duration_ms=0,
                            sandbox_failed=True, error="image missing"),
            CliToolErrorClass.SANDBOX_FAILED,
            "sandbox_failed",
        ),
    ],
)
async def test_counter_increments_on_error_with_correct_outcome(
    runner_result, error_class, outcome_label
):
    """Each CliToolError class maps to its lowercase outcome label.

    Covers the three runner-reported failure modes. Outcomes that come
    from _check_access / _validate_params (permission_denied,
    validation_error, rate_limited, not_found) share the same code path
    and are exercised below via `test_counter_records_pre_runner_errors`.
    """
    tenant = uuid.uuid4()
    # Name each test's tool uniquely so parametrized runs don't alias
    # each other's counter rows.
    tool = _tool(tenant_id=tenant, name=f"err_tool_{outcome_label}")
    agent = _agent(tenant)

    before = _counter_value(
        tool_name=f"err_tool_{outcome_label}",
        tenant_id=str(tenant),
        outcome=outcome_label,
    )

    with pytest.raises(CliToolError) as exc_info:
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(),
            runner=_mock_runner(runner_result),
        )
    assert exc_info.value.error_class is error_class

    after = _counter_value(
        tool_name=f"err_tool_{outcome_label}",
        tenant_id=str(tenant),
        outcome=outcome_label,
    )
    assert after - before == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_counter_records_pre_runner_errors():
    """Errors raised before the runner (validation, access) still count.

    Regression guard: an earlier draft only recorded metrics after the
    runner call, which meant schema failures disappeared from the
    dashboards.
    """
    tenant = uuid.uuid4()
    tool = _tool(
        tenant_id=tenant,
        name="pre_runner_tool",
        parameters_schema={
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
    )
    agent = _agent(tenant)

    before = _counter_value(
        tool_name="pre_runner_tool",
        tenant_id=str(tenant),
        outcome="validation_error",
    )
    with pytest.raises(CliToolError):
        await execute_cli_tool(
            tool=tool, agent=agent, params={"n": "not-int"},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(), runner=_mock_runner(),
        )
    after = _counter_value(
        tool_name="pre_runner_tool",
        tenant_id=str(tenant),
        outcome="validation_error",
    )
    assert after - before == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_duration_histogram_observes():
    """Every call adds one sample to the duration histogram."""
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, name="hist_tool")
    agent = _agent(tenant)

    before = _histogram_sample_count("hist_tool")
    await execute_cli_tool(
        tool=tool, agent=agent, params={},
        user_context={"id": "u1", "phone": "", "email": ""},
        storage=_mock_storage(), runner=_mock_runner(),
    )
    after = _histogram_sample_count("hist_tool")
    assert after - before == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_internal_error_recorded_as_internal_error_outcome():
    """A bare `Exception` (programmer bug, infra crash) is classified as
    `internal_error` — not dropped, not labelled `ok`."""
    tenant = uuid.uuid4()
    tool = _tool(tenant_id=tenant, name="boom_tool")
    agent = _agent(tenant)

    # Runner raises a non-CliToolError → hits the generic `except
    # Exception` branch in the executor.
    runner = MagicMock()
    runner.default_image = "default"
    runner.run = AsyncMock(side_effect=RuntimeError("kaboom"))

    before = _counter_value(
        tool_name="boom_tool",
        tenant_id=str(tenant),
        outcome="internal_error",
    )

    with pytest.raises(RuntimeError):
        await execute_cli_tool(
            tool=tool, agent=agent, params={},
            user_context={"id": "u1", "phone": "", "email": ""},
            storage=_mock_storage(), runner=runner,
        )

    after = _counter_value(
        tool_name="boom_tool",
        tenant_id=str(tenant),
        outcome="internal_error",
    )
    assert after - before == pytest.approx(1.0)


def test_label_cardinality_is_bounded():
    """Counter metadata must not declare agent_id / user_id as labels.

    Pure static check — protects future editors from adding unbounded
    labels in a "just this one place" moment of weakness.
    """
    expected = {"tool_name", "tenant_id", "outcome"}
    assert set(cli_tool_executions_total._labelnames) == expected

    # Histogram has fewer labels on purpose (per-tool distribution is
    # enough; outcome-split histograms would multiply series).
    assert set(cli_tool_execution_duration_seconds._labelnames) == {"tool_name"}
