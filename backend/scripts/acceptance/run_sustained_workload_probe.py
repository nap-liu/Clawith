"""Run repeated mixed-workload capacity waves against the Docker stack.

The probe uses the production admission controller, per-session dispatcher,
and Docker PostgreSQL. Each wave fills the configured 700-turn envelope with
500 interactive turns across 300 sessions plus the reserved project,
scheduled, and background lanes. A bounded overflow burst measures queueing
and rejection before the wave is released. The live backend health endpoint is
checked before and after the run.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from contextlib import suppress
from statistics import median

import httpx
from prometheus_client import CollectorRegistry
from sqlalchemy import text

from app.database import async_session, engine
from app.services import channel_dispatch
from app.services.channel_dispatch import ChannelReactions
from app.services.workload_capacity import (
    WorkloadCapacity,
    WorkloadCapacityMetrics,
    WorkloadKind,
    WorkloadOverloadedError,
)

SESSION_COUNT = 300
INTERACTIVE_TURNS = 500
RESERVED_TURNS = {
    WorkloadKind.PROJECT: 100,
    WorkloadKind.SCHEDULED: 50,
    WorkloadKind.BACKGROUND: 50,
}
TOTAL_TURNS = INTERACTIVE_TURNS + sum(RESERVED_TURNS.values())
OVERFLOW_TURNS = int(os.getenv("CLAWITH_CAPACITY_OVERFLOW_TURNS", "25"))
WAVE_COUNT = int(os.getenv("CLAWITH_CAPACITY_WAVES", "5"))
PROVIDER_PARALLELISM = int(os.getenv("CLAWITH_CAPACITY_PROVIDER_PARALLELISM", "64"))
HEALTH_URL = os.getenv("CLAWITH_HEALTH_URL", "http://backend:8000/api/health")


def _percentile(samples: list[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return ordered[index]


async def _health_check() -> dict[str, object]:
    started_at = time.perf_counter()
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(HEALTH_URL)
        response.raise_for_status()
    return {
        "status_code": response.status_code,
        "latency_ms": round((time.perf_counter() - started_at) * 1_000, 2),
    }


async def _sample_runtime(
    capacity: WorkloadCapacity,
    stop: asyncio.Event,
    samples: list[dict[str, int]],
) -> None:
    while not stop.is_set():
        snapshot = await capacity.snapshot()
        samples.append(
            {
                "active": snapshot.global_capacity.active,
                "waiting": snapshot.global_capacity.waiting,
                "db_checked_out": int(getattr(engine.pool, "checkedout", lambda: 0)()),
            }
        )
        await asyncio.sleep(0.002)


async def run_probe() -> dict[str, object]:
    if WAVE_COUNT < 2:
        raise ValueError("CLAWITH_CAPACITY_WAVES must be at least 2")

    registry = CollectorRegistry()
    capacity = WorkloadCapacity(
        global_limit=700,
        tenant_limit=700,
        category_limits={
            WorkloadKind.INTERACTIVE: 500,
            WorkloadKind.PROJECT: 100,
            WorkloadKind.SCHEDULED: 50,
            WorkloadKind.BACKGROUND: 50,
        },
        default_timeout_seconds=0.1,
        instance_id="docker-sustained-700-turn-acceptance",
        metrics=WorkloadCapacityMetrics(registry),
    )
    channel_dispatch.get_workload_capacity = lambda: capacity

    provider_gate = asyncio.Semaphore(PROVIDER_PARALLELISM)
    active_by_session: Counter[str] = Counter()
    max_active_by_session: Counter[str] = Counter()
    turn_latencies_ms: list[float] = []
    wave_durations_ms: list[float] = []
    runtime_samples: list[dict[str, int]] = []
    errors: list[str] = []
    rejected_overflow = 0
    stop_sampling = asyncio.Event()
    sampler = asyncio.create_task(_sample_runtime(capacity, stop_sampling, runtime_samples))
    health_before = await _health_check()

    async def database_boundary() -> None:
        async with async_session() as db:
            assert await db.scalar(text("SELECT 1")) == 1

    async def interactive_work(session_key: str, release_wave: asyncio.Event) -> str:
        active_by_session[session_key] += 1
        max_active_by_session[session_key] = max(
            max_active_by_session[session_key],
            active_by_session[session_key],
        )
        try:
            await database_boundary()
            await release_wave.wait()
            async with provider_gate:
                await asyncio.sleep(0.003)
            await database_boundary()
            return session_key
        finally:
            active_by_session[session_key] -= 1

    async def reserved_work(
        kind: WorkloadKind,
        index: int,
        release_wave: asyncio.Event,
    ) -> str:
        await database_boundary()
        await release_wave.wait()
        async with provider_gate:
            await asyncio.sleep(0.003)
        await database_boundary()
        return f"{kind.value}:{index}"

    async def timed(coroutine) -> object:
        started_at = time.perf_counter()
        try:
            return await coroutine
        finally:
            turn_latencies_ms.append((time.perf_counter() - started_at) * 1_000)

    async def run_reserved(
        kind: WorkloadKind,
        index: int,
        release_wave: asyncio.Event,
    ) -> str:
        async with capacity.slot(kind, "capacity-probe-tenant"):
            return await reserved_work(kind, index, release_wave)

    try:
        for wave in range(WAVE_COUNT):
            release_wave = asyncio.Event()
            wave_started_at = time.perf_counter()
            tasks = [
                asyncio.create_task(
                    timed(
                        channel_dispatch.run_channel_message(
                            f"capacity:{index % SESSION_COUNT}",
                            is_command=False,
                            reactions=ChannelReactions(),
                            work=lambda key=f"capacity:{index % SESSION_COUNT}": interactive_work(
                                key,
                                release_wave,
                            ),
                            tenant_id="capacity-probe-tenant",
                        )
                    )
                )
                for index in range(INTERACTIVE_TURNS)
            ]
            for kind, count in RESERVED_TURNS.items():
                tasks.extend(
                    asyncio.create_task(timed(run_reserved(kind, index, release_wave))) for index in range(count)
                )

            overflow_tasks: list[asyncio.Task[object]] = []
            try:
                for _ in range(4_000):
                    snapshot = await capacity.snapshot()
                    if snapshot.global_capacity.active == TOTAL_TURNS:
                        break
                    await asyncio.sleep(0.002)
                else:
                    raise AssertionError(f"wave {wave + 1} did not fill the 700-turn envelope")

                overflow_tasks = [
                    asyncio.create_task(
                        channel_dispatch.run_channel_message(
                            f"capacity:overflow:{wave}:{index}",
                            is_command=False,
                            reactions=ChannelReactions(),
                            work=lambda: interactive_work("capacity:overflow", release_wave),
                            tenant_id="capacity-probe-tenant",
                        )
                    )
                    for index in range(OVERFLOW_TURNS)
                ]
                overflow_results = await asyncio.gather(*overflow_tasks, return_exceptions=True)
                rejected_overflow += sum(isinstance(result, WorkloadOverloadedError) for result in overflow_results)
                unexpected = [
                    result
                    for result in overflow_results
                    if isinstance(result, BaseException) and not isinstance(result, WorkloadOverloadedError)
                ]
                errors.extend(type(result).__name__ for result in unexpected)

                release_wave.set()
                results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)
                assert len(results) == TOTAL_TURNS
            finally:
                release_wave.set()
                for task in (*tasks, *overflow_tasks):
                    if not task.done():
                        task.cancel()
                with suppress(Exception):
                    await asyncio.gather(*tasks, *overflow_tasks, return_exceptions=True)

            wave_durations_ms.append((time.perf_counter() - wave_started_at) * 1_000)

        recovery_started_at = time.perf_counter()
        for _ in range(2_000):
            final_snapshot = await capacity.snapshot()
            if final_snapshot.global_capacity.active == 0 and final_snapshot.global_capacity.waiting == 0:
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError("capacity did not recover after the final spike")
        recovery_ms = (time.perf_counter() - recovery_started_at) * 1_000
        health_after = await _health_check()
    finally:
        stop_sampling.set()
        await sampler

    final_snapshot = await capacity.snapshot()
    result = {
        "passed": not errors,
        "health_before": health_before,
        "health_after": health_after,
        "waves": WAVE_COUNT,
        "turns_per_wave": TOTAL_TURNS,
        "completed_turns": WAVE_COUNT * TOTAL_TURNS,
        "interactive_sessions": SESSION_COUNT,
        "same_session_max_active": max(max_active_by_session.values()),
        "overflow_attempts": WAVE_COUNT * OVERFLOW_TURNS,
        "overflow_rejected": rejected_overflow,
        "error_count": len(errors),
        "errors": errors,
        "latency_ms": {
            "p50": round(_percentile(turn_latencies_ms, 0.50), 2),
            "p95": round(_percentile(turn_latencies_ms, 0.95), 2),
            "p99": round(_percentile(turn_latencies_ms, 0.99), 2),
            "max": round(max(turn_latencies_ms, default=0.0), 2),
        },
        "wave_duration_ms": {
            "median": round(median(wave_durations_ms), 2),
            "max": round(max(wave_durations_ms), 2),
        },
        "queue_depth_high_watermark": max(
            (sample["waiting"] for sample in runtime_samples),
            default=0,
        ),
        "capacity_high_watermark": final_snapshot.global_capacity.high_watermark,
        "database_pool_checked_out_high_watermark": max(
            (sample["db_checked_out"] for sample in runtime_samples),
            default=0,
        ),
        "database_pool_configured_max": int(
            getattr(engine.pool, "size", lambda: 0)() + getattr(engine.pool, "_max_overflow", 0)
        ),
        "recovery_ms": round(recovery_ms, 2),
        "final_active": final_snapshot.global_capacity.active,
        "final_waiting": final_snapshot.global_capacity.waiting,
    }
    assert result["same_session_max_active"] == 1
    assert result["overflow_rejected"] == result["overflow_attempts"]
    assert result["capacity_high_watermark"] == TOTAL_TURNS
    assert result["database_pool_checked_out_high_watermark"] <= result["database_pool_configured_max"]
    assert result["final_active"] == 0
    assert result["final_waiting"] == 0
    assert result["passed"]
    return result


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_probe()), ensure_ascii=False, indent=2))
