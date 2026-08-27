"""Process-local admission control for turn-producing workloads.

This limiter protects one application process from accepting more work than it
can execute predictably. It deliberately does not claim distributed fairness or
a fleet-wide global quota. A horizontally scaled deployment must configure a
safe per-instance share on every replica and use an external coordinator when a
strict cross-replica limit is required.

Admission is independent from database connection pooling. A turn must not hold
an ORM session while it waits on a model, tool, subprocess, or remote service;
otherwise even a correctly sized turn limiter can still exhaust the DB pool.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from typing import Final, Self

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge

from app.config import Settings, get_settings


class WorkloadKind(StrEnum):
    """Top-level workload classes that receive independent capacity."""

    INTERACTIVE = "interactive"
    PROJECT = "project"
    SCHEDULED = "scheduled"
    BACKGROUND = "background"


class CapacityDimension(StrEnum):
    """Capacity dimensions that can prevent admission."""

    GLOBAL = "global"
    CATEGORY = "category"
    TENANT = "tenant"


class WorkloadOverloadedError(RuntimeError):
    """Raised after bounded admission waiting cannot obtain a slot."""

    def __init__(
        self,
        *,
        kind: WorkloadKind,
        tenant_id: str,
        timeout_seconds: float,
        blocked_by: tuple[CapacityDimension, ...],
    ) -> None:
        self.kind = kind
        self.tenant_id = tenant_id
        self.timeout_seconds = timeout_seconds
        self.blocked_by = blocked_by
        dimensions = ", ".join(dimension.value for dimension in blocked_by) or "capacity"
        super().__init__(
            f"Workload admission timed out after {timeout_seconds:g}s "
            f"for {kind.value} tenant {tenant_id}; blocked by {dimensions}."
        )


@dataclass(frozen=True, slots=True)
class CapacityCounterSnapshot:
    """One immutable capacity/traffic counter view."""

    limit: int
    active: int
    waiting: int
    high_watermark: int
    admitted_total: int
    completed_total: int
    rejected_total: int


@dataclass(frozen=True, slots=True)
class WorkloadCapacitySnapshot:
    """Consistent point-in-time metrics for one application instance."""

    instance_id: str
    captured_at: datetime
    global_capacity: CapacityCounterSnapshot
    categories: Mapping[str, CapacityCounterSnapshot]
    tenants: Mapping[str, CapacityCounterSnapshot]


class WorkloadCapacityMetrics:
    """Low-cardinality Prometheus metrics for workload admission.

    Only the global aggregate and workload category are exported. Tenant
    counters intentionally remain available only through :meth:`snapshot` so
    tenant identifiers cannot create an unbounded Prometheus label set.
    """

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        labels = ("kind",)
        self.active = Gauge(
            "clawith_workload_capacity_active",
            "Currently admitted workloads by category; global is the instance aggregate.",
            labels,
            registry=registry,
        )
        self.waiting = Gauge(
            "clawith_workload_capacity_waiting",
            "Workloads waiting for admission by category; global is the instance aggregate.",
            labels,
            registry=registry,
        )
        self.limit = Gauge(
            "clawith_workload_capacity_limit",
            "Configured per-instance admission limit by category.",
            labels,
            registry=registry,
        )
        self.admitted = Counter(
            "clawith_workload_capacity_admitted_total",
            "Workloads admitted by category; global is the instance aggregate.",
            labels,
            registry=registry,
        )
        self.completed = Counter(
            "clawith_workload_capacity_completed_total",
            "Workload leases released by category; global is the instance aggregate.",
            labels,
            registry=registry,
        )
        self.rejected = Counter(
            "clawith_workload_capacity_rejected_total",
            "Workloads rejected after bounded waiting by category.",
            labels,
            registry=registry,
        )
        self.high_watermark = Gauge(
            "clawith_workload_capacity_high_watermark",
            "Highest concurrent workload count observed by category in this process.",
            labels,
            registry=registry,
        )

    def initialize(
        self,
        global_limit: int,
        category_limits: Mapping[WorkloadKind, int],
    ) -> None:
        """Publish configured limits and initialize current-state gauges."""

        self._initialize_kind("global", global_limit)
        for kind, limit in category_limits.items():
            self._initialize_kind(kind.value, limit)

    def observe_state(
        self,
        kind: WorkloadKind,
        global_state: _CounterState,
        category_state: _CounterState,
    ) -> None:
        """Synchronize mutable gauges for the global and category states."""

        self._observe_kind("global", global_state)
        self._observe_kind(kind.value, category_state)

    def record_admission(self, kind: WorkloadKind) -> None:
        """Increment admitted counters for one workload."""

        self.admitted.labels(kind="global").inc()
        self.admitted.labels(kind=kind.value).inc()

    def record_completion(self, kind: WorkloadKind) -> None:
        """Increment completed counters for one released lease."""

        self.completed.labels(kind="global").inc()
        self.completed.labels(kind=kind.value).inc()

    def record_rejection(self, kind: WorkloadKind) -> None:
        """Increment rejected counters for one timed-out workload."""

        self.rejected.labels(kind="global").inc()
        self.rejected.labels(kind=kind.value).inc()

    def _initialize_kind(self, kind: str, limit: int) -> None:
        self.limit.labels(kind=kind).set(limit)
        self.active.labels(kind=kind).set(0)
        self.waiting.labels(kind=kind).set(0)
        self.high_watermark.labels(kind=kind).set(0)
        self.admitted.labels(kind=kind).inc(0)
        self.completed.labels(kind=kind).inc(0)
        self.rejected.labels(kind=kind).inc(0)

    def _observe_kind(self, kind: str, state: _CounterState) -> None:
        self.active.labels(kind=kind).set(state.active)
        self.waiting.labels(kind=kind).set(state.waiting)
        self.high_watermark.labels(kind=kind).set(state.high_watermark)


@dataclass(slots=True)
class _CounterState:
    active: int = 0
    waiting: int = 0
    high_watermark: int = 0
    admitted_total: int = 0
    completed_total: int = 0
    rejected_total: int = 0


@dataclass(slots=True)
class _Waiter:
    kind: WorkloadKind
    tenant_id: str
    future: asyncio.Future[WorkloadLease]
    admitted: bool = False


class WorkloadLease:
    """One idempotently releasable admission slot."""

    __slots__ = ("_capacity", "_released", "kind", "tenant_id")

    def __init__(
        self,
        capacity: WorkloadCapacity,
        *,
        kind: WorkloadKind,
        tenant_id: str,
    ) -> None:
        self._capacity = capacity
        self._released = False
        self.kind = kind
        self.tenant_id = tenant_id

    async def release(self) -> None:
        """Return the slot. Repeated calls are safe."""

        if self._released:
            return
        self._released = True
        await self._capacity._release(self.kind, self.tenant_id)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        await self.release()


class WorkloadCapacity:
    """Bounded, process-local admission for heterogeneous workloads.

    Waiters are considered in arrival order, while temporarily blocked workload
    classes are skipped so an available category does not suffer head-of-line
    blocking behind a saturated one.
    """

    def __init__(
        self,
        *,
        global_limit: int,
        tenant_limit: int,
        category_limits: Mapping[WorkloadKind, int],
        default_timeout_seconds: float,
        instance_id: str,
        metrics: WorkloadCapacityMetrics | None = None,
    ) -> None:
        if global_limit < 1:
            raise ValueError("global_limit must be at least 1")
        if tenant_limit < 1:
            raise ValueError("tenant_limit must be at least 1")
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")

        normalized_limits = {kind: int(category_limits.get(kind, 0)) for kind in WorkloadKind}
        if any(limit < 0 for limit in normalized_limits.values()):
            raise ValueError("category limits cannot be negative")

        self.global_limit: Final = global_limit
        self.tenant_limit: Final = tenant_limit
        self.category_limits: Final = normalized_limits
        self.default_timeout_seconds: Final = default_timeout_seconds
        self.instance_id: Final = instance_id
        self._metrics = metrics

        self._lock = asyncio.Lock()
        self._waiters: deque[_Waiter] = deque()
        self._global = _CounterState()
        self._categories = {kind: _CounterState() for kind in WorkloadKind}
        self._tenants: dict[str, _CounterState] = {}
        if self._metrics is not None:
            self._metrics.initialize(global_limit, normalized_limits)

    async def acquire(
        self,
        kind: WorkloadKind | str,
        tenant_id: object,
        *,
        timeout_seconds: float | None = None,
    ) -> WorkloadLease:
        """Wait for and return a lease, or raise an explicit overload error."""

        normalized_kind = WorkloadKind(kind)
        normalized_tenant_id = str(tenant_id).strip()
        if not normalized_tenant_id:
            raise ValueError("tenant_id cannot be empty")
        timeout = self.default_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")

        loop = asyncio.get_running_loop()
        waiter = _Waiter(
            kind=normalized_kind,
            tenant_id=normalized_tenant_id,
            future=loop.create_future(),
        )
        async with self._lock:
            self._enqueue_locked(waiter)
            self._dispatch_locked()

        try:
            return await asyncio.wait_for(asyncio.shield(waiter.future), timeout)
        except TimeoutError:
            async with self._lock:
                if waiter.admitted:
                    return waiter.future.result()
                self._remove_waiter_locked(waiter)
                waiter.future.cancel()
                blocked_by = self._blocked_dimensions_locked(
                    normalized_kind,
                    normalized_tenant_id,
                )
                self._record_rejection_locked(normalized_kind, normalized_tenant_id)
            raise WorkloadOverloadedError(
                kind=normalized_kind,
                tenant_id=normalized_tenant_id,
                timeout_seconds=timeout,
                blocked_by=blocked_by,
            ) from None
        except asyncio.CancelledError:
            async with self._lock:
                if waiter.admitted:
                    self._release_locked(
                        normalized_kind,
                        normalized_tenant_id,
                    )
                else:
                    self._remove_waiter_locked(waiter)
                    waiter.future.cancel()
            raise

    @asynccontextmanager
    async def slot(
        self,
        kind: WorkloadKind | str,
        tenant_id: object,
        *,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[WorkloadLease]:
        """Acquire and always release a workload slot around one turn."""

        lease = await self.acquire(
            kind,
            tenant_id,
            timeout_seconds=timeout_seconds,
        )
        try:
            yield lease
        finally:
            await lease.release()

    async def snapshot(self) -> WorkloadCapacitySnapshot:
        """Return an atomic metrics snapshot for health/metrics adapters."""

        async with self._lock:
            return WorkloadCapacitySnapshot(
                instance_id=self.instance_id,
                captured_at=datetime.now(UTC),
                global_capacity=self._snapshot_counter(
                    self._global,
                    self.global_limit,
                ),
                categories={
                    kind.value: self._snapshot_counter(
                        self._categories[kind],
                        self.category_limits[kind],
                    )
                    for kind in WorkloadKind
                },
                tenants={
                    tenant_id: self._snapshot_counter(state, self.tenant_limit)
                    for tenant_id, state in sorted(self._tenants.items())
                },
            )

    def _enqueue_locked(self, waiter: _Waiter) -> None:
        self._waiters.append(waiter)
        self._global.waiting += 1
        self._categories[waiter.kind].waiting += 1
        self._tenant_state(waiter.tenant_id).waiting += 1
        self._observe_metrics_locked(waiter.kind)

    def _remove_waiter_locked(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            return
        self._global.waiting -= 1
        self._categories[waiter.kind].waiting -= 1
        self._tenant_state(waiter.tenant_id).waiting -= 1
        self._observe_metrics_locked(waiter.kind)

    def _dispatch_locked(self) -> None:
        while self._global.active < self.global_limit:
            waiter = next(
                (
                    candidate
                    for candidate in self._waiters
                    if self._can_admit_locked(candidate.kind, candidate.tenant_id)
                ),
                None,
            )
            if waiter is None:
                return

            self._remove_waiter_locked(waiter)
            self._record_admission_locked(waiter.kind, waiter.tenant_id)
            waiter.admitted = True
            waiter.future.set_result(
                WorkloadLease(
                    self,
                    kind=waiter.kind,
                    tenant_id=waiter.tenant_id,
                )
            )

    def _can_admit_locked(self, kind: WorkloadKind, tenant_id: str) -> bool:
        return (
            self._global.active < self.global_limit
            and self._categories[kind].active < self.category_limits[kind]
            and self._tenant_state(tenant_id).active < self.tenant_limit
        )

    def _blocked_dimensions_locked(
        self,
        kind: WorkloadKind,
        tenant_id: str,
    ) -> tuple[CapacityDimension, ...]:
        blocked: list[CapacityDimension] = []
        if self._global.active >= self.global_limit:
            blocked.append(CapacityDimension.GLOBAL)
        if self._categories[kind].active >= self.category_limits[kind]:
            blocked.append(CapacityDimension.CATEGORY)
        if self._tenant_state(tenant_id).active >= self.tenant_limit:
            blocked.append(CapacityDimension.TENANT)
        return tuple(blocked)

    def _record_admission_locked(self, kind: WorkloadKind, tenant_id: str) -> None:
        states = (
            self._global,
            self._categories[kind],
            self._tenant_state(tenant_id),
        )
        for state in states:
            state.active += 1
            state.admitted_total += 1
            state.high_watermark = max(state.high_watermark, state.active)
        if self._metrics is not None:
            self._metrics.record_admission(kind)
        self._observe_metrics_locked(kind)

    def _record_rejection_locked(self, kind: WorkloadKind, tenant_id: str) -> None:
        self._global.rejected_total += 1
        self._categories[kind].rejected_total += 1
        self._tenant_state(tenant_id).rejected_total += 1
        if self._metrics is not None:
            self._metrics.record_rejection(kind)

    async def _release(self, kind: WorkloadKind, tenant_id: str) -> None:
        async with self._lock:
            self._release_locked(kind, tenant_id)

    def _release_locked(self, kind: WorkloadKind, tenant_id: str) -> None:
        states = (
            self._global,
            self._categories[kind],
            self._tenant_state(tenant_id),
        )
        if any(state.active < 1 for state in states):
            raise RuntimeError("workload capacity counter underflow")
        for state in states:
            state.active -= 1
            state.completed_total += 1
        if self._metrics is not None:
            self._metrics.record_completion(kind)
        self._observe_metrics_locked(kind)
        self._dispatch_locked()

    def _observe_metrics_locked(self, kind: WorkloadKind) -> None:
        if self._metrics is not None:
            self._metrics.observe_state(
                kind,
                self._global,
                self._categories[kind],
            )

    def _tenant_state(self, tenant_id: str) -> _CounterState:
        return self._tenants.setdefault(tenant_id, _CounterState())

    @staticmethod
    def _snapshot_counter(
        state: _CounterState,
        limit: int,
    ) -> CapacityCounterSnapshot:
        return CapacityCounterSnapshot(
            limit=limit,
            active=state.active,
            waiting=state.waiting,
            high_watermark=state.high_watermark,
            admitted_total=state.admitted_total,
            completed_total=state.completed_total,
            rejected_total=state.rejected_total,
        )


def workload_category_limits(settings: Settings) -> Mapping[WorkloadKind, int]:
    """Build the typed category mapping from environment-backed settings."""

    return {
        WorkloadKind.INTERACTIVE: settings.WORKLOAD_INTERACTIVE_LIMIT,
        WorkloadKind.PROJECT: settings.WORKLOAD_PROJECT_LIMIT,
        WorkloadKind.SCHEDULED: settings.WORKLOAD_SCHEDULED_LIMIT,
        WorkloadKind.BACKGROUND: settings.WORKLOAD_BACKGROUND_LIMIT,
    }


WORKLOAD_CAPACITY_METRICS = WorkloadCapacityMetrics()


@lru_cache(maxsize=1)
def get_workload_capacity() -> WorkloadCapacity:
    """Return the process-local workload capacity registry."""

    settings = get_settings()
    return WorkloadCapacity(
        global_limit=settings.WORKLOAD_GLOBAL_LIMIT,
        tenant_limit=settings.WORKLOAD_TENANT_LIMIT,
        category_limits=workload_category_limits(settings),
        default_timeout_seconds=settings.WORKLOAD_ACQUIRE_TIMEOUT_SECONDS,
        instance_id=settings.INSTANCE_ID,
        metrics=WORKLOAD_CAPACITY_METRICS,
    )
