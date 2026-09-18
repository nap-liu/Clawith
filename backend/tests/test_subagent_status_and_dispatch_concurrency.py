"""Focused behavior checks for async Subagent status and parent fairness."""

import json

import pytest

from app.services.agent_tools_execute_tool_preflight import execute_tool_preflight
from tests.test_subagent_runtime import (
    _dispose_engine_between_tests,  # noqa: F401 - imported autouse fixture
    _make_context,
    asyncio,
    runtime,
    uuid,
)

pytestmark = pytest.mark.asyncio


async def test_parent_dispatch_does_not_block_unrelated_parent_batches(monkeypatch):
    blocked_id = uuid.uuid4()
    ready_id = uuid.uuid4()
    blocked = asyncio.Event()
    ready = asyncio.Event()
    scans = 0
    parent_ids = {
        blocked_id: uuid.uuid4(),
        ready_id: uuid.uuid4(),
    }

    async def pending(**_kwargs):
        nonlocal scans
        scans += 1
        return [blocked_id, ready_id] if scans == 1 else []

    async def grouped(message_ids):
        return [
            (parent_ids[message_id], [message_id])
            for message_id in message_ids
        ], []

    async def dispatch(message_ids):
        if message_ids == [blocked_id]:
            await blocked.wait()
        else:
            ready.set()
        return True

    monkeypatch.setattr(runtime, "_pending_parent_events", pending)
    monkeypatch.setattr(runtime, "_pending_parent_event_groups", grouped)
    monkeypatch.setattr(runtime, "_dispatch_parent_event_batch", dispatch)
    monkeypatch.setattr(runtime, "PARENT_EVENT_BATCH_DEBOUNCE_SECONDS", 0.01)

    daemon = asyncio.create_task(runtime._dispatch_event_loop(project_scope=False))
    await asyncio.wait_for(ready.wait(), timeout=1)
    daemon.cancel()
    with pytest.raises(asyncio.CancelledError):
        await daemon


async def test_parent_dispatch_supervisor_serializes_parent_and_awaits_close():
    wake = asyncio.Event()
    supervisor = runtime.ParentDispatchSupervisor(
        concurrency=2,
        retry_seconds=0.01,
        wake_event=wake,
    )
    parent_id = uuid.uuid4()
    other_parent_id = uuid.uuid4()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked():
        try:
            await release.wait()
            return True
        except asyncio.CancelledError:
            cancelled.set()
            raise

    assert supervisor.schedule(
        parent_session_id=parent_id,
        message_ids=[uuid.uuid4()],
        work=blocked,
    )
    assert not supervisor.schedule(
        parent_session_id=parent_id,
        message_ids=[uuid.uuid4()],
        work=blocked,
    )
    assert supervisor.schedule(
        parent_session_id=other_parent_id,
        message_ids=[uuid.uuid4()],
        work=blocked,
    )

    await asyncio.sleep(0)
    await supervisor.close()
    assert cancelled.is_set()


async def test_parent_dispatch_retries_do_not_starve_healthy_parent():
    wake = asyncio.Event()
    supervisor = runtime.ParentDispatchSupervisor(
        concurrency=8,
        retry_seconds=10,
        wake_event=wake,
    )
    poisoned = asyncio.Event()
    healthy = asyncio.Event()

    async def poison():
        poisoned.set()
        return False

    async def complete():
        healthy.set()
        return True

    for _index in range(8):
        assert supervisor.schedule(
            parent_session_id=uuid.uuid4(),
            message_ids=[uuid.uuid4()],
            work=poison,
        )
    await asyncio.wait_for(poisoned.wait(), timeout=1)
    assert supervisor.schedule(
        parent_session_id=uuid.uuid4(),
        message_ids=[uuid.uuid4()],
        work=complete,
    )
    await asyncio.wait_for(healthy.wait(), timeout=1)
    await asyncio.wait_for(supervisor.close(), timeout=1)


async def test_dispatch_scan_moves_past_fifty_active_poison_parents(monkeypatch):
    poison_ids = [uuid.uuid4() for _index in range(50)]
    healthy_id = uuid.uuid4()
    parent_ids = {message_id: uuid.uuid4() for message_id in [*poison_ids, healthy_id]}
    healthy = asyncio.Event()

    async def pending(*, exclude_ids=None, **_kwargs):
        excluded = exclude_ids or set()
        return [
            message_id
            for message_id in [*poison_ids, healthy_id]
            if message_id not in excluded
        ][:50]

    async def grouped(message_ids):
        return [
            (parent_ids[message_id], [message_id])
            for message_id in message_ids
        ], []

    async def dispatch(message_ids):
        if message_ids == [healthy_id]:
            healthy.set()
            return True
        return False

    monkeypatch.setattr(runtime, "_pending_parent_events", pending)
    monkeypatch.setattr(runtime, "_pending_parent_event_groups", grouped)
    monkeypatch.setattr(runtime, "_dispatch_parent_event_batch", dispatch)
    monkeypatch.setattr(runtime, "PARENT_EVENT_BATCH_DEBOUNCE_SECONDS", 0)
    monkeypatch.setattr(runtime, "DISPATCH_RETRY_INTERVAL_SECONDS", 10)
    monkeypatch.setattr(runtime, "_dispatch_wakeup", asyncio.Event())

    daemon = asyncio.create_task(runtime._dispatch_event_loop(project_scope=False))
    await asyncio.wait_for(healthy.wait(), timeout=1)
    daemon.cancel()
    with pytest.raises(asyncio.CancelledError):
        await daemon


async def test_pending_scan_excludes_owned_a2a_handoff():
    first_agent_id, user_id, project_parent_id, _anchor_id = await _make_context(
        project=True
    )
    second_agent_id = uuid.UUID(int=first_agent_id.int - 1)
    storage_agent_id, agent_id = sorted([first_agent_id, second_agent_id], key=str)
    async with runtime.async_session() as db:
        first_agent = await db.get(runtime.Agent, first_agent_id)
        project_parent = await db.get(runtime.ChatSession, project_parent_id)
        db.add(
            runtime.Agent(
                id=second_agent_id,
                name="A2A scan peer",
                creator_id=user_id,
                tenant_id=first_agent.tenant_id,
                primary_model_id=first_agent.primary_model_id,
                status="idle",
            )
        )
        parent = runtime.ChatSession(
            project_id=project_parent.project_id,
            agent_id=storage_agent_id,
            peer_agent_id=agent_id,
            user_id=None,
            title="A2A scan parent",
            source_channel="agent",
            is_primary=False,
            is_group=False,
        )
        db.add(parent)
        await db.commit()
        parent_id = parent.id
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-a2a-handoff-scan",
        task="A2A handoff",
        mode="async",
    )
    async with runtime.async_session() as db:
        event = runtime.ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            role="assistant",
            content="completed",
            conversation_id=str(run.id),
            message_meta={
                "kind": runtime.SUBAGENT_COMPLETION,
                "subagent_wake": True,
                "attachments": [],
            },
        )
        db.add(event)
        await db.flush()
        db.add(
            runtime.ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="materialized",
                conversation_id=str(parent_id),
                external_event_key=f"project-subagent:{event.id}",
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        event_id = event.id

    pending = await runtime._pending_parent_events(
        debounce_seconds=0,
        include_legacy=True,
        project_scope=True,
    )
    assert event_id in pending
    excluded = await runtime._pending_parent_events(
        debounce_seconds=0,
        include_legacy=True,
        project_scope=True,
        exclude_ids={event_id},
    )
    assert event_id not in excluded


@pytest.mark.parametrize("raises", [False, True])
async def test_parent_dispatch_supervisor_holds_parent_during_retry_delay(raises):
    wake = asyncio.Event()
    supervisor = runtime.ParentDispatchSupervisor(
        concurrency=1,
        retry_seconds=0.05,
        wake_event=wake,
    )
    parent_id = uuid.uuid4()
    attempts = 0

    async def retryable():
        nonlocal attempts
        attempts += 1
        if attempts == 1 and raises:
            raise RuntimeError("retryable dispatch failure")
        return attempts > 1

    async def completed():
        return True

    assert supervisor.schedule(
        parent_session_id=parent_id,
        message_ids=[uuid.uuid4()],
        work=retryable,
    )
    await asyncio.sleep(0.01)
    assert not supervisor.schedule(
        parent_session_id=parent_id,
        message_ids=[uuid.uuid4()],
        work=completed,
    )
    await asyncio.wait_for(wake.wait(), timeout=0.2)
    assert attempts == 2
    assert supervisor.schedule(
        parent_session_id=parent_id,
        message_ids=[uuid.uuid4()],
        work=completed,
    )
    await supervisor.close()


async def test_get_subagent_status_returns_owned_lifecycle_and_result():
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-status",
        task="inspect status",
        mode="async",
        turn_anchor_id=anchor_id,
    )

    status = await runtime.get_subagent_status(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        subagent_id=str(run.id),
    )

    assert status["subagent_id"] == str(run.id)
    assert status["status"] == runtime.RUN_QUEUED
    assert status["pending_messages"] == 1
    assert status["result"] is None

    tool_result = json.loads(
        await execute_tool_preflight(
            "get_subagent_status",
            {"subagent_id": str(run.id)},
            agent_id,
            user_id,
            session_id=str(parent_id),
        )
    )
    assert tool_result["status"] == runtime.RUN_QUEUED

    with pytest.raises(runtime.SubagentError):
        await runtime.get_subagent_status(
            agent_id=agent_id,
            execution_user_id=user_id,
            parent_session_id=str(uuid.uuid4()),
            subagent_id=str(run.id),
        )
    with pytest.raises(runtime.SubagentError):
        await runtime.get_subagent_status(
            agent_id=agent_id,
            execution_user_id=uuid.uuid4(),
            parent_session_id=str(parent_id),
            subagent_id=str(run.id),
        )
    with pytest.raises(runtime.SubagentError):
        await runtime.get_subagent_status(
            agent_id=uuid.uuid4(),
            execution_user_id=user_id,
            parent_session_id=str(parent_id),
            subagent_id=str(run.id),
        )

    async with runtime.async_session() as db:
        stored = await db.get(runtime.SubagentRun, run.id)
        stored.status = runtime.RUN_FAILED
        db.add(
            runtime.ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="child failed visibly",
                conversation_id=str(run.id),
                message_meta={"kind": runtime.SUBAGENT_FAILURE, "attachments": []},
            )
        )
        await db.commit()

    failed = await runtime.get_subagent_status(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        subagent_id=str(run.id),
    )
    assert failed["status"] == runtime.RUN_FAILED
    assert failed["failed"] is True
    assert failed["result"] == "child failed visibly"
