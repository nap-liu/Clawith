"""Exercise 700 mixed concurrent turns against Docker PostgreSQL.

The probe models the production turn boundary: a short database read, provider
waiting without a checked-out connection, then a short database write/read.
It starts 500 interactive turns across 300 sessions plus the reserved project,
scheduled, and background lanes. This verifies the unified admission limit,
same-session serialization, lane isolation, and database-pool high watermark.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from contextlib import suppress

from sqlalchemy import text

from app.database import async_session, engine
from app.services import channel_dispatch
from app.services.channel_dispatch import ChannelReactions
from app.services.workload_capacity import (
    WorkloadCapacity,
    WorkloadKind,
    WorkloadOverloadedError,
)

SESSION_COUNT = 300
TURN_COUNT = 500
RESERVED_TURNS = {
    WorkloadKind.PROJECT: 100,
    WorkloadKind.SCHEDULED: 50,
    WorkloadKind.BACKGROUND: 50,
}
PROVIDER_PARALLELISM = 32


async def _sample_pool(stop: asyncio.Event, samples: list[int]) -> None:
    while not stop.is_set():
        checked_out = getattr(engine.pool, "checkedout", lambda: 0)()
        samples.append(int(checked_out))
        await asyncio.sleep(0.001)


async def run_probe() -> dict[str, object]:
    capacity = WorkloadCapacity(
        global_limit=700,
        tenant_limit=700,
        category_limits={
            WorkloadKind.INTERACTIVE: 500,
            WorkloadKind.PROJECT: 100,
            WorkloadKind.SCHEDULED: 50,
            WorkloadKind.BACKGROUND: 50,
        },
        default_timeout_seconds=0.05,
        instance_id="docker-700-turn-acceptance",
    )
    channel_dispatch.get_workload_capacity = lambda: capacity

    release_provider = asyncio.Event()
    all_sessions_started = asyncio.Event()
    all_reserved_started = asyncio.Event()
    provider_gate = asyncio.Semaphore(PROVIDER_PARALLELISM)
    active_by_session: Counter[str] = Counter()
    max_active_by_session: Counter[str] = Counter()
    started_sessions: set[str] = set()
    reserved_started = 0
    pool_samples: list[int] = []
    stop_sampling = asyncio.Event()
    sampler = asyncio.create_task(_sample_pool(stop_sampling, pool_samples))

    async def work(session_key: str) -> str:
        active_by_session[session_key] += 1
        max_active_by_session[session_key] = max(
            max_active_by_session[session_key],
            active_by_session[session_key],
        )
        started_sessions.add(session_key)
        if len(started_sessions) == SESSION_COUNT:
            all_sessions_started.set()
        try:
            async with async_session() as db:
                assert await db.scalar(text("SELECT 1")) == 1

            await all_sessions_started.wait()
            await release_provider.wait()
            async with provider_gate:
                await asyncio.sleep(0.005)

            async with async_session() as db:
                assert await db.scalar(text("SELECT 1")) == 1
            return session_key
        finally:
            active_by_session[session_key] -= 1

    async def reserved_work(kind: WorkloadKind, index: int) -> str:
        nonlocal reserved_started
        async with async_session() as db:
            assert await db.scalar(text("SELECT 1")) == 1

        reserved_started += 1
        if reserved_started == sum(RESERVED_TURNS.values()):
            all_reserved_started.set()
        await release_provider.wait()
        async with provider_gate:
            await asyncio.sleep(0.005)

        async with async_session() as db:
            assert await db.scalar(text("SELECT 1")) == 1
        return f"{kind.value}:{index}"

    async def run_reserved(kind: WorkloadKind, index: int) -> str:
        async with capacity.slot(kind, "capacity-probe-tenant"):
            return await reserved_work(kind, index)

    started_at = time.perf_counter()
    tasks = [
        asyncio.create_task(
            channel_dispatch.run_channel_message(
                f"capacity:{index % SESSION_COUNT}",
                is_command=False,
                reactions=ChannelReactions(),
                work=lambda key=f"capacity:{index % SESSION_COUNT}": work(key),
                tenant_id="capacity-probe-tenant",
            )
        )
        for index in range(TURN_COUNT)
    ]
    for kind, count in RESERVED_TURNS.items():
        tasks.extend(asyncio.create_task(run_reserved(kind, index)) for index in range(count))

    try:
        await asyncio.wait_for(all_sessions_started.wait(), timeout=20)
        await asyncio.wait_for(all_reserved_started.wait(), timeout=20)
        total_turns = TURN_COUNT + sum(RESERVED_TURNS.values())
        for _ in range(2_000):
            snapshot = await capacity.snapshot()
            if snapshot.global_capacity.active == total_turns:
                break
            await asyncio.sleep(0.001)
        assert snapshot.global_capacity.active == total_turns
        assert snapshot.categories[WorkloadKind.INTERACTIVE.value].active == TURN_COUNT
        for kind, count in RESERVED_TURNS.items():
            assert snapshot.categories[kind.value].active == count

        overload_observed = False
        try:
            await channel_dispatch.run_channel_message(
                "capacity:overflow",
                is_command=False,
                reactions=ChannelReactions(),
                work=lambda: work("capacity:overflow"),
                tenant_id="capacity-probe-tenant",
            )
        except WorkloadOverloadedError:
            overload_observed = True
        assert overload_observed

        release_provider.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)
    finally:
        release_provider.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        with suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)
        stop_sampling.set()
        await sampler

    final_snapshot = await capacity.snapshot()
    elapsed_seconds = time.perf_counter() - started_at
    result = {
        "passed": True,
        "sessions": len(started_sessions),
        "interactive_turns": TURN_COUNT,
        "reserved_turns": sum(RESERVED_TURNS.values()),
        "total_turns": len(results),
        "same_session_max_active": max(max_active_by_session.values()),
        "capacity_high_watermark": final_snapshot.global_capacity.high_watermark,
        "interactive_high_watermark": final_snapshot.categories[WorkloadKind.INTERACTIVE.value].high_watermark,
        "project_high_watermark": final_snapshot.categories[WorkloadKind.PROJECT.value].high_watermark,
        "scheduled_high_watermark": final_snapshot.categories[WorkloadKind.SCHEDULED.value].high_watermark,
        "background_high_watermark": final_snapshot.categories[WorkloadKind.BACKGROUND.value].high_watermark,
        "rejected_overflow_turns": final_snapshot.global_capacity.rejected_total,
        "database_pool_configured_max": int(
            getattr(engine.pool, "size", lambda: 0)() + getattr(engine.pool, "_max_overflow", 0)
        ),
        "database_pool_checked_out_high_watermark": max(pool_samples, default=0),
        "provider_parallelism": PROVIDER_PARALLELISM,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }
    assert result["sessions"] == SESSION_COUNT
    assert result["total_turns"] == TURN_COUNT + sum(RESERVED_TURNS.values())
    assert result["same_session_max_active"] == 1
    assert result["capacity_high_watermark"] == TURN_COUNT + sum(RESERVED_TURNS.values())
    assert result["database_pool_checked_out_high_watermark"] <= result["database_pool_configured_max"]
    return result


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_probe()), ensure_ascii=False, indent=2))
