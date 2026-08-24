"""Fair recovery tests for the durable project dispatch outbox."""

import uuid
from datetime import UTC, datetime

import pytest

from app.services import subagent_runtime


@pytest.mark.asyncio
async def test_recovery_visits_later_batches_after_poisoned_old_rows(monkeypatch) -> None:
    first_batch = [uuid.uuid4() for _ in range(50)]
    later_run = uuid.uuid4()
    first_cursor = (datetime(2026, 1, 1, tzinfo=UTC), first_batch[-1])
    later_cursor = (datetime(2026, 1, 2, tzinfo=UTC), later_run)
    scans = []
    dispatched = []

    async def fake_pending_batch(*, after=None):
        scans.append(after)
        if after is None:
            return first_batch, first_cursor
        assert after == first_cursor
        return [later_run], later_cursor

    async def fake_dispatch(run_id):
        dispatched.append(run_id)
        if run_id == first_batch[0]:
            raise RuntimeError("poisoned outbox row")
        if run_id in first_batch:
            return {"status": "not_dispatchable"}
        return {"status": "running"}

    monkeypatch.setattr(
        subagent_runtime,
        "_pending_project_dispatch_batch",
        fake_pending_batch,
    )
    monkeypatch.setattr(subagent_runtime, "dispatch_project_run", fake_dispatch)

    made_progress = await subagent_runtime._recover_project_dispatch_outbox_once()

    assert made_progress is True
    assert scans == [None, first_cursor]
    assert dispatched == [*first_batch, later_run]
