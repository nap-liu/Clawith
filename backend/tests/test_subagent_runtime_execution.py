"""Mechanical continuation of normalized durable Subagent runtime tests."""

import pytest

from tests.test_subagent_runtime import (
    Agent,
    ChatMessage,
    ChatSession,
    SubagentRun,
    WorkloadCapacity,
    WorkloadKind,
    _dispose_engine_between_tests,  # noqa: F401 - pytest autouse fixture
    _make_context,
    async_session,
    asynccontextmanager,
    asyncio,
    runtime,
    select,
)

pytestmark = pytest.mark.asyncio

async def test_create_uses_one_id_readable_model_and_idempotent_fork():
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, created = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-one",
        name="Research helper",
        task="focused child task",
        mode="async",
        model="  readable-test-model ",
        fork=True,
        soul=False,
        memory=False,
        turn_anchor_id=anchor_id,
    )
    replay, replay_created = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-one",
        name="ignored replay name",
        task="ignored replay text",
        mode="sync",
    )

    assert created is True
    assert replay_created is False
    assert replay.id == run.id
    assert run.model == "Readable-Test-Model"
    async with async_session() as db:
        child = await db.get(ChatSession, run.id)
        lifecycle = await db.get(SubagentRun, run.id)
        rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(run.id))
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
    assert child is not None and child.id == lifecycle.id == run.id
    assert child.source_channel == "subagent"
    assert child.title == "Research helper"
    assert lifecycle.soul is False
    assert lifecycle.memory is False
    assert [row.content for row in rows][-3:] == [
        "earlier user context",
        "earlier context",
        "focused child task",
    ]
    assert all(row.content != "parent request" for row in rows)
    assert rows[-1].message_meta["subagent_input_state"] == "pending"


async def test_round_boundary_drains_append_and_stop_wins():
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-inbox",
        task="first",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    claimed = await runtime._load_or_start_input(run.id)
    assert claimed is not None
    anchor, recovering = claimed
    assert recovering is False
    await runtime.append_subagent_message(
        agent_id=agent_id,
        parent_session_id=str(parent_id),
        subagent_id=str(run.id),
        message="second",
        execution_user_id=user_id,
        origin_tool_call_id="append-second",
    )
    # Recovery replays a still-running tool with the same provider call id.
    # The already-committed inbox side effect must remain exactly once.
    await runtime.append_subagent_message(
        agent_id=agent_id,
        parent_session_id=str(parent_id),
        subagent_id=str(run.id),
        message="second",
        execution_user_id=user_id,
        origin_tool_call_id="append-second",
    )
    assert await runtime._drain_subagent_inbox(run.id, anchor.id) == [{"role": "user", "content": "second"}]
    assert (
        await runtime.stop_subagent(
            agent_id=agent_id,
            parent_session_id=str(parent_id),
            subagent_id=str(run.id),
            execution_user_id=user_id,
        )
        == "cancelled"
    )
    assert not await runtime._finish_subagent_turn(
        run_id=run.id,
        anchor_id=anchor.id,
        reply="stale final",
        failed=False,
    )
    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        stale_final = (
            await db.execute(
                select(ChatMessage.id).where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.content == "stale final",
                )
            )
        ).scalar_one_or_none()
        current_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
    assert fresh.status == "cancelled"
    assert stale_final is None
    assert current_turn.anchor_id == anchor.id
    assert current_turn.status == "cancelled"
    assert current_turn.revision >= 2


async def test_control_plane_cancel_is_terminal_not_requeued(monkeypatch):
    from app.services import channel_llm
    from app.services.active_turns import (
        cancel_active_turn,
        ensure_active_turn,
        list_active_turns,
        reset_active_turns_for_testing,
    )
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    await reset_active_turns_for_testing()

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-control-cancel",
        task="long child task",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    ready = asyncio.Event()

    async def fake_call(*args, **kwargs):
        await ensure_active_turn(
            owner_user_id=user_id,
            agent_id=agent_id,
            session_id=str(run.id),
            turn_type="subagent",
            turn_anchor_id=kwargs["turn_anchor_id"],
        )
        ready.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(channel_llm, "_call_agent_llm", fake_call)
    worker = asyncio.create_task(runtime.execute_claimed_subagent(run.id))
    await asyncio.wait_for(ready.wait(), timeout=5)
    record = (await list_active_turns(owner_user_id=user_id))[0]
    await cancel_active_turn(record.turn_id, owner_user_id=user_id)
    with pytest.raises(asyncio.CancelledError):
        await worker

    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        input_rows = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == str(run.id),
                        ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_INPUT,
                    )
                )
            )
            .scalars()
            .all()
        )
        current_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
    assert fresh.status == runtime.RUN_CANCELLED
    assert all(row.message_meta["subagent_input_state"] == runtime.INPUT_CANCELLED for row in input_rows)
    assert current_turn.anchor_id == input_rows[0].id
    assert current_turn.status == "cancelled"
    assert current_turn.revision >= 2
    await reset_active_turns_for_testing()


async def test_fresh_turn_uses_exact_prefix_when_later_input_is_already_queued(
    monkeypatch,
):
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-concurrent-prefix",
        task="first queued project input",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.append_subagent_message(
        agent_id=agent_id,
        parent_session_id=str(parent_id),
        subagent_id=str(run.id),
        message="later queued project input",
        execution_user_id=user_id,
        origin_tool_call_id="append-concurrent-prefix",
    )
    assert await runtime._claim_subagent(run.id) == run.id

    captured: dict = {}

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del _agent_id, _session_id, execution_user_id
        return []

    async def fake_llm(_db, _agent_id, user_text, **kwargs):
        captured["user_text"] = user_text
        captured["history"] = list(kwargs["history"])
        captured["inbox"] = await kwargs["before_round"](0)
        return "handled queued project inputs"

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)

    await runtime.execute_claimed_subagent(run.id)

    assert captured["user_text"] == "first queued project input"
    assert all(row.get("content") != "later queued project input" for row in captured["history"])
    assert captured["inbox"] == [{"role": "user", "content": "later queued project input"}]
    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        inputs = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(run.id),
                        ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_INPUT,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        current_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
    assert fresh.status == runtime.RUN_COMPLETED
    assert [row.message_meta["subagent_input_state"] for row in inputs] == [
        runtime.INPUT_DONE,
        runtime.INPUT_DONE,
    ]
    assert current_turn.anchor_id == inputs[0].id
    assert current_turn.status == "completed"
    assert current_turn.revision >= 2


async def test_provider_round_releases_preflight_database_transaction(
    monkeypatch,
):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-release-provider-connection",
        task="perform one long provider request",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    transaction_state: list[bool] = []

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del _agent_id, _session_id, execution_user_id
        return []

    async def fake_llm(db, _agent_id, _user_text, **kwargs):
        # Mirror the unified channel preflight, which resolves Agent/model/scene
        # records before the first provider round.
        await db.execute(select(ChatSession.id).where(ChatSession.id == run.id))
        transaction_state.append(bool(db.in_transaction()))
        await kwargs["before_round"](0)
        transaction_state.append(bool(db.in_transaction()))
        return "provider request completed"

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)

    await runtime.execute_claimed_subagent(run.id)

    assert transaction_state == [True, False]
    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
    assert fresh.status == runtime.RUN_COMPLETED


async def test_project_capacity_wait_does_not_retain_database_session(
    monkeypatch,
):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-project-capacity",
        task="wait for project capacity",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    async with async_session() as db:
        tenant_id = (await db.get(Agent, agent_id)).tenant_id

    capacity = WorkloadCapacity(
        global_limit=1,
        tenant_limit=1,
        category_limits={kind: 1 for kind in WorkloadKind},
        default_timeout_seconds=5,
        instance_id="subagent-capacity-test",
    )
    blocker = await capacity.acquire(WorkloadKind.PROJECT, tenant_id)
    monkeypatch.setattr(runtime, "get_workload_capacity", lambda: capacity)

    real_async_session = runtime.async_session
    open_sessions: set[int] = set()

    @asynccontextmanager
    async def tracking_session():
        async with real_async_session() as db:
            marker = id(db)
            open_sessions.add(marker)
            try:
                yield db
            finally:
                open_sessions.discard(marker)

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del _agent_id, _session_id, execution_user_id
        return []

    async def fake_llm(_db, _agent_id, _user_text, **kwargs):
        snapshot = await capacity.snapshot()
        assert snapshot.categories[WorkloadKind.PROJECT.value].active == 1
        assert snapshot.tenants[str(tenant_id)].active == 1
        await kwargs["before_round"](0)
        return "project capacity admitted"

    monkeypatch.setattr(runtime, "async_session", tracking_session)
    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)

    worker = asyncio.create_task(runtime.execute_claimed_subagent(run.id))
    try:
        for _ in range(100):
            snapshot = await capacity.snapshot()
            if snapshot.categories[WorkloadKind.PROJECT.value].waiting == 1:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("subagent did not wait for project capacity")

        assert not open_sessions
        await blocker.release()
        await worker
    finally:
        await blocker.release()
        if not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    snapshot = await capacity.snapshot()
    project_metrics = snapshot.categories[WorkloadKind.PROJECT.value]
    tenant_metrics = snapshot.tenants[str(tenant_id)]
    assert project_metrics.admitted_total == 2
    assert project_metrics.completed_total == 2
    assert tenant_metrics.admitted_total == 2
    assert tenant_metrics.completed_total == 2


async def test_project_capacity_timeout_requeues_claimed_turn(monkeypatch):
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-project-capacity-timeout",
        task="retry after project admission timeout",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    async with async_session() as db:
        tenant_id = (await db.get(Agent, agent_id)).tenant_id

    capacity = WorkloadCapacity(
        global_limit=1,
        tenant_limit=1,
        category_limits={kind: 1 for kind in WorkloadKind},
        default_timeout_seconds=0.01,
        instance_id="subagent-capacity-timeout-test",
    )
    blocker = await capacity.acquire(WorkloadKind.PROJECT, tenant_id)
    monkeypatch.setattr(runtime, "get_workload_capacity", lambda: capacity)
    try:
        await runtime.execute_claimed_subagent(run.id)
    finally:
        await blocker.release()

    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        input_row = await db.scalar(
            select(ChatMessage).where(
                ChatMessage.conversation_id == str(run.id),
                ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_INPUT,
            )
        )
        current_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
        exact_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
            turn_anchor_id=input_row.id,
        )
    assert fresh.status == runtime.RUN_QUEUED
    assert fresh.lease_owner is None
    assert input_row.message_meta["subagent_input_state"] == runtime.INPUT_PENDING
    assert current_turn == exact_turn
    assert current_turn.anchor_id == input_row.id
    assert current_turn.status == "running"
    snapshot = await capacity.snapshot()
    assert snapshot.categories[WorkloadKind.PROJECT.value].rejected_total == 1
    assert snapshot.tenants[str(tenant_id)].rejected_total == 1


async def test_failed_turn_continues_when_parent_input_is_pending():
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-failed-pending",
        task="first",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    claimed = await runtime._load_or_start_input(run.id)
    assert claimed is not None
    first_anchor, recovering = claimed
    assert recovering is False
    await runtime.append_subagent_message(
        agent_id=agent_id,
        parent_session_id=str(parent_id),
        subagent_id=str(run.id),
        message="continue despite failure",
        execution_user_id=user_id,
        origin_tool_call_id="append-after-failure",
    )

    assert not await runtime._finish_subagent_turn(
        run_id=run.id,
        anchor_id=first_anchor.id,
        reply="[LLM Error] first turn failed",
        failed=True,
    )
    next_claim = await runtime._load_or_start_input(run.id)
    assert next_claim is not None
    next_anchor, next_recovering = next_claim
    assert next_recovering is False
    assert next_anchor.content == "continue despite failure"

    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        first = await db.get(ChatMessage, first_anchor.id)
        current_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
    assert fresh.status == "running"
    assert first.message_meta["subagent_input_state"] == "done"
    assert first.message_meta["turn_status"] == "failed"
    assert current_turn.anchor_id == next_anchor.id
    assert current_turn.status == "running"
    assert current_turn.generation > int(first.message_meta["turn_generation"])


async def test_subagent_finish_waits_for_reserved_stop_before_mutating_anchor():
    from app.services.active_turns import (
        ensure_active_turn,
        finalize_active_turn_stop,
        list_active_turns,
        reserve_active_turn_stop,
        reset_active_turns_for_testing,
    )
    from app.services.chat_history import mark_turn_cancelled

    await reset_active_turns_for_testing()
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-stop-finish-race",
        task="must stop cleanly",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    claimed = await runtime._load_or_start_input(run.id)
    assert claimed is not None
    turn_anchor, _ = claimed
    registered = asyncio.Event()
    finish_now = asyncio.Event()

    async def finishing_turn():
        await ensure_active_turn(
            owner_user_id=user_id,
            agent_id=agent_id,
            session_id=str(run.id),
            turn_type="subagent",
            turn_anchor_id=turn_anchor.id,
        )
        registered.set()
        await finish_now.wait()
        await runtime._finish_subagent_turn(
            run_id=run.id,
            anchor_id=turn_anchor.id,
            reply="must not be committed",
            failed=False,
        )

    worker = asyncio.create_task(finishing_turn())
    await registered.wait()
    record = (await list_active_turns(owner_user_id=user_id))[0]
    _, stop_token = await reserve_active_turn_stop(
        record.turn_id,
        owner_user_id=user_id,
    )
    assert stop_token is not None

    finish_now.set()
    await asyncio.sleep(0)
    assert not worker.done()
    async with async_session() as db:
        assert (
            await mark_turn_cancelled(
                db,
                agent_id=agent_id,
                conversation_id=str(run.id),
                turn_anchor_id=turn_anchor.id,
                reason="test control-plane stop",
            )
            == turn_anchor.id
        )
        await db.commit()
    await finalize_active_turn_stop(record, stop_token)
    with pytest.raises(asyncio.CancelledError):
        await worker

    async with async_session() as db:
        fresh_anchor = await db.get(ChatMessage, turn_anchor.id)
        terminal_reply = await db.scalar(
            select(ChatMessage.id).where(
                ChatMessage.conversation_id == str(run.id),
                ChatMessage.role == "assistant",
                ChatMessage.message_meta["turn_anchor_id"].as_string() == str(turn_anchor.id),
                ChatMessage.message_meta["turn_status"].as_string() == "completed",
            )
        )
    assert fresh_anchor.message_meta["turn_status"] == "cancelled"
    assert fresh_anchor.message_meta["subagent_input_state"] == "processing"
    assert terminal_reply is None
    await reset_active_turns_for_testing()


async def test_execution_process_loss_requeues_same_subagent_turn(monkeypatch):
    from app.services.turn_interruption import TurnInterrupted

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id, execution_user_id=user_id,
        parent_session_id=str(parent_id), origin_tool_call_id="interrupted-child",
        task="resume original child", mode="async", turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id

    async def no_tools(*_args, **_kwargs):
        return []

    async def interrupted(*_args, **_kwargs):
        raise TurnInterrupted()

    monkeypatch.setattr(runtime, "prepare_subagent_tools", no_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", interrupted)
    await runtime.execute_claimed_subagent(run.id)
    async with async_session() as db:
        stored = await db.get(SubagentRun, run.id)
        assert stored.status == runtime.RUN_QUEUED
        assert stored.lease_owner is None
        child = await db.get(ChatSession, run.id)
        current = child.im_config["conversation_turn"]
        assert current["status"] == "running"
        original_anchor = current["turn_anchor_id"]
    assert await runtime._claim_subagent(run.id) == run.id
    async with async_session() as db:
        child = await db.get(ChatSession, run.id)
        assert child.im_config["conversation_turn"]["turn_anchor_id"] == original_anchor
