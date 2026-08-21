from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services.active_turns import (
    cancel_active_turn,
    commit_current_turn_anchor,
    ensure_active_turn,
    finalize_active_turn_stop,
    list_active_turns,
    release_active_turn_stop,
    reserve_active_turn_stop,
    reset_active_turns_for_testing,
    set_active_turn_cancel_task,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _reset_registry():
    await reset_active_turns_for_testing()
    yield
    await reset_active_turns_for_testing()


async def test_nested_calls_share_one_cancellable_root_turn():
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    root_anchor_id = uuid.uuid4()
    nested_anchor_id = uuid.uuid4()
    nested_storage_agent_id = uuid.uuid4()
    ready = asyncio.Event()
    nested_ids: list[str] = []

    async def root():
        root_record = await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=agent_id,
            session_id="root-session",
            turn_type="web",
            turn_anchor_id=root_anchor_id,
        )

        async def nested():
            nested_record = await ensure_active_turn(
                owner_user_id=owner_id,
                agent_id=agent_id,
                session_id="nested-a2a",
                turn_type="agent",
                turn_anchor_id=nested_anchor_id,
                turn_anchor_agent_id=nested_storage_agent_id,
            )
            nested_ids.append(nested_record.turn_id)

        await nested()
        nested_ids.append(root_record.turn_id)
        ready.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(root())
    await ready.wait()
    records = await list_active_turns(owner_user_id=owner_id)

    assert len(records) == 1
    assert nested_ids == [records[0].turn_id, records[0].turn_id]
    assert {anchor.message_id for anchor in records[0].durable_anchors} == {
        root_anchor_id,
        nested_anchor_id,
    }
    nested_anchor = next(
        anchor
        for anchor in records[0].durable_anchors
        if anchor.message_id == nested_anchor_id
    )
    assert nested_anchor.agent_id == nested_storage_agent_id

    cancelled = await cancel_active_turn(records[0].turn_id, owner_user_id=owner_id)
    assert cancelled is records[0]
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await list_active_turns(owner_user_id=owner_id) == []


async def test_independent_child_task_registers_its_own_root_turn():
    owner_id = uuid.uuid4()
    ready = asyncio.Event()
    release = asyncio.Event()
    child_record_id: list[str] = []

    async def child():
        record = await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id="independent-child",
            turn_type="task",
        )
        child_record_id.append(record.turn_id)
        ready.set()
        await release.wait()

    async def root():
        root_record = await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id="root",
            turn_type="web",
        )
        child_task = asyncio.create_task(child())
        await ready.wait()
        records = await list_active_turns(owner_user_id=owner_id)
        assert len(records) == 2
        assert child_record_id[0] != root_record.turn_id
        release.set()
        await child_task

    await root()


async def test_explicitly_bound_child_task_reuses_parent_turn():
    owner_id = uuid.uuid4()
    enter = asyncio.Event()
    ready = asyncio.Event()
    release = asyncio.Event()
    child_record_id: list[str] = []

    async def child():
        await enter.wait()
        record = await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id="bound-child",
            turn_type="web",
        )
        child_record_id.append(record.turn_id)
        ready.set()
        await release.wait()

    async def root():
        root_record = await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id="root",
            turn_type="web",
        )
        child_task = asyncio.create_task(child())
        set_active_turn_cancel_task(child_task)
        enter.set()
        await ready.wait()
        assert child_record_id == [root_record.turn_id]
        assert len(await list_active_turns(owner_user_id=owner_id)) == 1
        release.set()
        await child_task

    await root()


async def test_owner_filter_prevents_cross_user_stop_but_admin_mode_can_stop():
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    ready = asyncio.Event()

    async def root():
        await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id="task-session",
            turn_type="task",
        )
        ready.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(root())
    await ready.wait()
    record = (await list_active_turns())[0]

    assert await cancel_active_turn(record.turn_id, owner_user_id=other_id) is None
    assert not task.done()
    assert await cancel_active_turn(record.turn_id, owner_user_id=None) is record
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_repeated_cancel_does_not_interrupt_first_cancel_cleanup():
    owner_id = uuid.uuid4()
    ready = asyncio.Event()
    cleaning_up = asyncio.Event()
    finish_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def root():
        await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id="double-stop",
            turn_type="task",
        )
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning_up.set()
            await finish_cleanup.wait()
            cleanup_finished.set()

    task = asyncio.create_task(root())
    await ready.wait()
    record = (await list_active_turns(owner_user_id=owner_id))[0]

    assert await cancel_active_turn(record.turn_id, owner_user_id=owner_id) is record
    await cleaning_up.wait()
    assert await cancel_active_turn(record.turn_id, owner_user_id=owner_id) is record
    assert not task.done()

    finish_cleanup.set()
    await task
    assert cleanup_finished.is_set()


async def test_stop_reservation_freezes_nested_anchors_until_released():
    owner_id = uuid.uuid4()
    root_anchor_id = uuid.uuid4()
    nested_anchor_id = uuid.uuid4()
    registered = asyncio.Event()
    add_nested = asyncio.Event()
    nested_added = asyncio.Event()
    release_root = asyncio.Event()

    async def root():
        await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id="root",
            turn_anchor_id=root_anchor_id,
        )
        registered.set()
        await add_nested.wait()
        await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id="nested",
            turn_anchor_id=nested_anchor_id,
        )
        nested_added.set()
        await release_root.wait()

    task = asyncio.create_task(root())
    await registered.wait()
    record = (await list_active_turns(owner_user_id=owner_id))[0]
    reserved, token = await reserve_active_turn_stop(
        record.turn_id,
        owner_user_id=owner_id,
    )
    assert reserved is record
    assert token is not None

    add_nested.set()
    await asyncio.sleep(0)
    assert not nested_added.is_set()
    assert [anchor.message_id for anchor in record.durable_anchors] == [root_anchor_id]

    await release_active_turn_stop(record, token)
    await asyncio.wait_for(nested_added.wait(), timeout=1)
    assert [anchor.message_id for anchor in record.durable_anchors] == [
        root_anchor_id,
        nested_anchor_id,
    ]
    release_root.set()
    await task


async def test_committed_stop_cancels_frozen_turn_without_accepting_new_anchor():
    owner_id = uuid.uuid4()
    root_anchor_id = uuid.uuid4()
    nested_anchor_id = uuid.uuid4()
    registered = asyncio.Event()
    add_nested = asyncio.Event()

    async def root():
        await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id="root",
            turn_anchor_id=root_anchor_id,
        )
        registered.set()
        await add_nested.wait()
        await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id="nested",
            turn_anchor_id=nested_anchor_id,
        )

    task = asyncio.create_task(root())
    await registered.wait()
    record = (await list_active_turns(owner_user_id=owner_id))[0]
    _, token = await reserve_active_turn_stop(
        record.turn_id,
        owner_user_id=owner_id,
    )
    assert token is not None

    add_nested.set()
    await asyncio.sleep(0)
    assert await finalize_active_turn_stop(record, token) is record
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [anchor.message_id for anchor in record.durable_anchors] == [root_anchor_id]


async def test_stop_snapshot_waits_for_inflight_durable_anchor_commit():
    owner_id = uuid.uuid4()
    storage_agent_id = uuid.uuid4()
    anchor_id = uuid.uuid4()
    registered = asyncio.Event()
    begin_commit = asyncio.Event()
    commit_started = asyncio.Event()
    allow_commit = asyncio.Event()

    async def durable_commit():
        commit_started.set()
        await allow_commit.wait()

    async def root():
        await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id="root",
        )
        registered.set()
        await begin_commit.wait()
        await commit_current_turn_anchor(
            durable_commit,
            agent_id=storage_agent_id,
            session_id="nested-a2a",
            message_id=anchor_id,
        )
        await asyncio.Event().wait()

    task = asyncio.create_task(root())
    await registered.wait()
    record = (await list_active_turns(owner_user_id=owner_id))[0]
    begin_commit.set()
    await commit_started.wait()

    stop_task = asyncio.create_task(
        reserve_active_turn_stop(record.turn_id, owner_user_id=owner_id)
    )
    await asyncio.sleep(0)
    assert not stop_task.done()
    allow_commit.set()
    reserved, token = await stop_task
    assert reserved is record
    assert token is not None
    assert [(anchor.agent_id, anchor.session_id, anchor.message_id) for anchor in record.durable_anchors] == [
        (storage_agent_id, "nested-a2a", anchor_id)
    ]

    await finalize_active_turn_stop(record, token)
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_reserved_stop_rejects_uncommitted_nested_anchor():
    owner_id = uuid.uuid4()
    registered = asyncio.Event()
    begin_commit = asyncio.Event()
    commit_called = asyncio.Event()

    async def root():
        await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id="root",
        )
        registered.set()
        await begin_commit.wait()

        async def must_not_commit():
            commit_called.set()

        await commit_current_turn_anchor(
            must_not_commit,
            agent_id=uuid.uuid4(),
            session_id="nested-a2a",
            message_id=uuid.uuid4(),
        )

    task = asyncio.create_task(root())
    await registered.wait()
    record = (await list_active_turns(owner_user_id=owner_id))[0]
    _, token = await reserve_active_turn_stop(
        record.turn_id,
        owner_user_id=owner_id,
    )
    assert token is not None

    begin_commit.set()
    await asyncio.sleep(0)
    assert not commit_called.is_set()
    await finalize_active_turn_stop(record, token)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert record.durable_anchors == []


async def test_channel_dispatch_bounds_turn_on_reusable_consumer_task():
    from app.services.channel_dispatch import ChannelReactions, run_channel_message

    owner_id = uuid.uuid4()
    ready = asyncio.Event()
    release = asyncio.Event()

    async def work():
        await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=uuid.uuid4(),
            session_id=str(uuid.uuid4()),
            turn_type="feishu",
        )
        ready.set()
        await release.wait()
        return "ok"

    task = asyncio.create_task(
        run_channel_message(
            "test-channel-turn",
            is_command=False,
            reactions=ChannelReactions(),
            work=work,
        )
    )
    await ready.wait()
    assert len(await list_active_turns(owner_user_id=owner_id)) == 1
    release.set()
    assert await task == "ok"
    assert await list_active_turns(owner_user_id=owner_id) == []


async def test_llm_client_guard_closes_client_after_owner_cancellation():
    from app.services.llm.client import LLMClientCloseGuard

    closed = asyncio.Event()

    class FakeClient:
        async def close(self):
            closed.set()

    async def owner():
        LLMClientCloseGuard(FakeClient())
        await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(closed.wait(), timeout=1)


async def test_llm_client_guard_finishes_close_when_explicit_await_is_cancelled():
    from app.services.llm.client import LLMClientCloseGuard

    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    closed = asyncio.Event()

    class FakeClient:
        async def close(self):
            close_started.set()
            await allow_close.wait()
            closed.set()

    async def owner():
        guard = LLMClientCloseGuard(FakeClient())
        await guard.close()

    task = asyncio.create_task(owner())
    await close_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    allow_close.set()
    await asyncio.wait_for(closed.wait(), timeout=1)
