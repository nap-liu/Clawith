"""High-concurrency admission coverage for the unified channel entry."""

import asyncio
import uuid
from collections import Counter

import pytest

from app import database
from app.services import channel_dispatch
from app.services.workload_capacity import (
    CapacityDimension,
    WorkloadCapacity,
    WorkloadKind,
    WorkloadOverloadedError,
)


def _capacity(*, global_limit: int, tenant_limit: int) -> WorkloadCapacity:
    return WorkloadCapacity(
        global_limit=global_limit,
        tenant_limit=tenant_limit,
        category_limits={
            WorkloadKind.INTERACTIVE: global_limit,
            WorkloadKind.PROJECT: global_limit,
            WorkloadKind.SCHEDULED: global_limit,
            WorkloadKind.BACKGROUND: global_limit,
        },
        default_timeout_seconds=0.01,
        instance_id="channel-capacity-test",
    )


@pytest.mark.asyncio
async def test_five_hundred_turns_across_three_hundred_sessions_are_bounded_and_serial(
    monkeypatch,
) -> None:
    capacity = _capacity(global_limit=500, tenant_limit=500)
    monkeypatch.setattr(channel_dispatch, "get_workload_capacity", lambda: capacity)

    def database_session_must_not_be_opened():
        raise AssertionError("workload admission must not check out a DB connection")

    monkeypatch.setattr(database, "async_session", database_session_must_not_be_opened)

    release_work = asyncio.Event()
    all_sessions_started = asyncio.Event()
    active_by_session: Counter[str] = Counter()
    max_active_by_session: Counter[str] = Counter()
    started_sessions: set[str] = set()

    def work_for(lock_key: str):
        async def work() -> str:
            active_by_session[lock_key] += 1
            max_active_by_session[lock_key] = max(
                max_active_by_session[lock_key],
                active_by_session[lock_key],
            )
            started_sessions.add(lock_key)
            if len(started_sessions) == 300:
                all_sessions_started.set()
            try:
                await release_work.wait()
            finally:
                active_by_session[lock_key] -= 1
            return lock_key

        return work

    tasks = [
        asyncio.create_task(
            channel_dispatch.run_channel_message(
                f"session:{index % 300}",
                is_command=False,
                reactions=channel_dispatch.ChannelReactions(),
                work=work_for(f"session:{index % 300}"),
                tenant_id="tenant-a",
            )
        )
        for index in range(500)
    ]
    await asyncio.wait_for(all_sessions_started.wait(), timeout=2)

    for _ in range(100):
        snapshot = await capacity.snapshot()
        if snapshot.global_capacity.active == 500:
            break
        await asyncio.sleep(0)
    assert snapshot.global_capacity.active == 500
    assert snapshot.global_capacity.high_watermark == 500

    with pytest.raises(WorkloadOverloadedError) as exc_info:
        await channel_dispatch.run_channel_message(
            "session:overflow",
            is_command=False,
            reactions=channel_dispatch.ChannelReactions(),
            work=work_for("session:overflow"),
            tenant_id="tenant-a",
        )
    assert CapacityDimension.GLOBAL in exc_info.value.blocked_by

    release_work.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)

    assert len(results) == 500
    assert len(started_sessions) == 300
    assert set(max_active_by_session.values()) == {1}
    snapshot = await capacity.snapshot()
    assert snapshot.global_capacity.active == 0
    assert snapshot.global_capacity.completed_total == 500
    assert snapshot.global_capacity.rejected_total == 1


@pytest.mark.asyncio
async def test_explicit_workload_and_tenant_are_visible_during_work(monkeypatch) -> None:
    capacity = _capacity(global_limit=2, tenant_limit=2)
    monkeypatch.setattr(channel_dispatch, "get_workload_capacity", lambda: capacity)
    tenant_id = uuid.uuid4()

    async def work() -> str:
        snapshot = await capacity.snapshot()
        assert snapshot.categories["project"].active == 1
        assert snapshot.categories["interactive"].active == 0
        assert snapshot.tenants[str(tenant_id)].active == 1
        return "done"

    result = await channel_dispatch.run_channel_message(
        "session:project",
        is_command=False,
        reactions=channel_dispatch.ChannelReactions(),
        work=work,
        workload_kind=WorkloadKind.PROJECT,
        tenant_id=tenant_id,
    )

    assert result == "done"
