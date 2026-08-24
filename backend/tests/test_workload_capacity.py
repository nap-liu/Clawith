"""Unit coverage for process-local workload admission control."""

import asyncio

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from app.config import Settings
from app.services.workload_capacity import (
    CapacityDimension,
    WorkloadCapacity,
    WorkloadCapacityMetrics,
    WorkloadKind,
    WorkloadOverloadedError,
)


def test_default_limits_reserve_capacity_for_non_interactive_work() -> None:
    fields = Settings.model_fields

    assert fields["WORKLOAD_GLOBAL_LIMIT"].default == 700
    assert fields["WORKLOAD_TENANT_LIMIT"].default == 700
    assert fields["WORKLOAD_INTERACTIVE_LIMIT"].default == 500
    assert fields["WORKLOAD_PROJECT_LIMIT"].default == 100
    assert fields["WORKLOAD_SCHEDULED_LIMIT"].default == 50
    assert fields["WORKLOAD_BACKGROUND_LIMIT"].default == 50


def _capacity(
    *,
    global_limit: int = 4,
    tenant_limit: int = 3,
    interactive_limit: int = 4,
    project_limit: int = 2,
    scheduled_limit: int = 1,
    background_limit: int = 1,
) -> WorkloadCapacity:
    return WorkloadCapacity(
        global_limit=global_limit,
        tenant_limit=tenant_limit,
        category_limits={
            WorkloadKind.INTERACTIVE: interactive_limit,
            WorkloadKind.PROJECT: project_limit,
            WorkloadKind.SCHEDULED: scheduled_limit,
            WorkloadKind.BACKGROUND: background_limit,
        },
        default_timeout_seconds=0.02,
        instance_id="test-instance",
    )


@pytest.mark.asyncio
async def test_global_capacity_has_bounded_wait_and_explicit_overload() -> None:
    capacity = _capacity(global_limit=2, tenant_limit=2)
    first = await capacity.acquire(WorkloadKind.INTERACTIVE, "tenant-a")
    second = await capacity.acquire(WorkloadKind.INTERACTIVE, "tenant-a")

    with pytest.raises(WorkloadOverloadedError) as exc_info:
        await capacity.acquire(
            WorkloadKind.PROJECT,
            "tenant-b",
            timeout_seconds=0.01,
        )

    assert exc_info.value.blocked_by == (CapacityDimension.GLOBAL,)
    assert exc_info.value.kind is WorkloadKind.PROJECT
    assert exc_info.value.tenant_id == "tenant-b"
    await first.release()
    await second.release()


@pytest.mark.asyncio
async def test_categories_are_isolated_without_head_of_line_blocking() -> None:
    capacity = _capacity(global_limit=3, scheduled_limit=1)
    scheduled = await capacity.acquire(WorkloadKind.SCHEDULED, "tenant-a")
    blocked = asyncio.create_task(
        capacity.acquire(
            WorkloadKind.SCHEDULED,
            "tenant-a",
            timeout_seconds=0.2,
        )
    )
    await asyncio.sleep(0)

    waiting_snapshot = await capacity.snapshot()
    assert waiting_snapshot.global_capacity.waiting == 1
    assert waiting_snapshot.categories["scheduled"].waiting == 1
    assert waiting_snapshot.tenants["tenant-a"].waiting == 1

    interactive = await capacity.acquire(WorkloadKind.INTERACTIVE, "tenant-a")
    assert not blocked.done()

    await scheduled.release()
    admitted = await blocked
    await admitted.release()
    await interactive.release()


@pytest.mark.asyncio
async def test_per_tenant_limit_does_not_block_another_tenant() -> None:
    capacity = _capacity(global_limit=3, tenant_limit=1)
    first = await capacity.acquire(WorkloadKind.INTERACTIVE, "tenant-a")

    with pytest.raises(WorkloadOverloadedError) as exc_info:
        await capacity.acquire(
            WorkloadKind.INTERACTIVE,
            "tenant-a",
            timeout_seconds=0.01,
        )
    second_tenant = await capacity.acquire(WorkloadKind.INTERACTIVE, "tenant-b")

    assert CapacityDimension.TENANT in exc_info.value.blocked_by
    await first.release()
    await second_tenant.release()


@pytest.mark.asyncio
async def test_context_manager_releases_after_failure_and_records_metrics() -> None:
    capacity = _capacity(global_limit=1, tenant_limit=1)

    with pytest.raises(RuntimeError, match="turn failed"):
        async with capacity.slot(WorkloadKind.PROJECT, "tenant-a"):
            raise RuntimeError("turn failed")

    async with capacity.slot(WorkloadKind.PROJECT, "tenant-a"):
        snapshot = await capacity.snapshot()
        assert snapshot.instance_id == "test-instance"
        assert snapshot.global_capacity.active == 1
        assert snapshot.categories["project"].active == 1
        assert snapshot.tenants["tenant-a"].active == 1

    snapshot = await capacity.snapshot()
    assert snapshot.global_capacity.active == 0
    assert snapshot.global_capacity.high_watermark == 1
    assert snapshot.global_capacity.admitted_total == 2
    assert snapshot.global_capacity.completed_total == 2


@pytest.mark.asyncio
async def test_cancelling_a_waiter_does_not_leak_capacity_or_queue_metrics() -> None:
    capacity = _capacity(global_limit=1, tenant_limit=1)
    active = await capacity.acquire(WorkloadKind.INTERACTIVE, "tenant-a")
    waiting = asyncio.create_task(
        capacity.acquire(
            WorkloadKind.INTERACTIVE,
            "tenant-a",
            timeout_seconds=1,
        )
    )
    await asyncio.sleep(0)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    snapshot = await capacity.snapshot()
    assert snapshot.global_capacity.active == 1
    assert snapshot.global_capacity.waiting == 0
    assert snapshot.global_capacity.rejected_total == 0

    await active.release()
    replacement = await capacity.acquire(WorkloadKind.INTERACTIVE, "tenant-a")
    await replacement.release()


@pytest.mark.asyncio
async def test_default_capacity_can_admit_five_hundred_concurrent_turns() -> None:
    capacity = _capacity(
        global_limit=500,
        tenant_limit=500,
        interactive_limit=500,
    )
    leases = await asyncio.gather(*(capacity.acquire(WorkloadKind.INTERACTIVE, "tenant-a") for _ in range(500)))

    snapshot = await capacity.snapshot()
    assert snapshot.global_capacity.active == 500
    assert snapshot.global_capacity.high_watermark == 500

    with pytest.raises(WorkloadOverloadedError):
        await capacity.acquire(
            WorkloadKind.INTERACTIVE,
            "tenant-a",
            timeout_seconds=0.001,
        )

    await asyncio.gather(*(lease.release() for lease in leases))
    snapshot = await capacity.snapshot()
    assert snapshot.global_capacity.active == 0
    assert snapshot.global_capacity.completed_total == 500
    assert snapshot.global_capacity.rejected_total == 1


@pytest.mark.asyncio
async def test_prometheus_metrics_track_categories_without_tenant_labels() -> None:
    registry = CollectorRegistry()
    metrics = WorkloadCapacityMetrics(registry)
    capacity = WorkloadCapacity(
        global_limit=1,
        tenant_limit=1,
        category_limits={
            WorkloadKind.INTERACTIVE: 1,
            WorkloadKind.PROJECT: 1,
            WorkloadKind.SCHEDULED: 0,
            WorkloadKind.BACKGROUND: 0,
        },
        default_timeout_seconds=0.02,
        instance_id="metrics-test",
        metrics=metrics,
    )

    interactive = await capacity.acquire(WorkloadKind.INTERACTIVE, "tenant-secret")
    waiting = asyncio.create_task(
        capacity.acquire(
            WorkloadKind.PROJECT,
            "tenant-secret",
            timeout_seconds=0.05,
        )
    )
    await asyncio.sleep(0)

    assert (
        registry.get_sample_value(
            "clawith_workload_capacity_limit",
            {"kind": "global"},
        )
        == 1
    )
    assert (
        registry.get_sample_value(
            "clawith_workload_capacity_active",
            {"kind": "interactive"},
        )
        == 1
    )
    assert (
        registry.get_sample_value(
            "clawith_workload_capacity_waiting",
            {"kind": "project"},
        )
        == 1
    )
    assert (
        registry.get_sample_value(
            "clawith_workload_capacity_high_watermark",
            {"kind": "global"},
        )
        == 1
    )
    assert (
        registry.get_sample_value(
            "clawith_workload_capacity_admitted_total",
            {"kind": "interactive"},
        )
        == 1
    )
    assert (
        registry.get_sample_value(
            "clawith_workload_capacity_rejected_total",
            {"kind": "scheduled"},
        )
        == 0
    )

    with pytest.raises(WorkloadOverloadedError):
        await waiting
    await interactive.release()

    assert (
        registry.get_sample_value(
            "clawith_workload_capacity_active",
            {"kind": "global"},
        )
        == 0
    )
    assert (
        registry.get_sample_value(
            "clawith_workload_capacity_waiting",
            {"kind": "project"},
        )
        == 0
    )
    assert (
        registry.get_sample_value(
            "clawith_workload_capacity_completed_total",
            {"kind": "interactive"},
        )
        == 1
    )
    assert (
        registry.get_sample_value(
            "clawith_workload_capacity_rejected_total",
            {"kind": "project"},
        )
        == 1
    )
    assert "tenant-secret" not in generate_latest(registry).decode()
