"""Mechanical continuation of normalized durable Subagent runtime tests."""

import pytest

from tests.test_subagent_runtime import (
    UTC,
    Agent,
    AgentTool,
    AgentToolUpdate,
    ChatCompaction,
    ChatMessage,
    ChatSession,
    HTTPException,
    Identity,
    LLMModel,
    MCPServer,
    Participant,
    Project,
    ProjectMemberSnapshot,
    ProjectRun,
    SUBAGENT_TOOL_NAMES,
    SimpleNamespace,
    SubagentRun,
    Tenant,
    Tool,
    USER_PROJECT_TOOL_NAMES,
    USER_PROJECT_TOOL_SEEDS,
    User,
    WorkloadCapacity,
    WorkloadKind,
    _dispose_engine_between_tests,
    _has_active_subagent_event_turn,
    _make_context,
    asyncio,
    async_session,
    asynccontextmanager,
    build_project_runtime_context,
    channel_llm,
    clone_source_agent_tool_dependencies,
    datetime,
    delete,
    engine,
    freeze_run_members,
    func,
    get_agent_tools_for_llm,
    get_agent_tools_with_config,
    json,
    merge_project_member_runtime_config,
    runtime,
    seed_builtin_tools,
    select,
    timedelta,
    update_agent_tools,
    uuid,
)

pytestmark = pytest.mark.asyncio

async def test_unexpected_failure_requeues_pending_input_without_lease_delay(
    monkeypatch,
):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-exception-pending",
        task="first",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    await runtime.append_subagent_message(
        agent_id=agent_id,
        parent_session_id=str(parent_id),
        subagent_id=str(run.id),
        message="must run immediately after exception",
        execution_user_id=user_id,
        origin_tool_call_id="append-after-exception",
    )

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del execution_user_id
        return []

    async def failing_llm(*_args, **_kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", failing_llm)

    await runtime.execute_claimed_subagent(run.id)

    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        pending = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.content == "must run immediately after exception",
                )
            )
        ).scalar_one()
    assert fresh.status == "queued"
    assert fresh.lease_owner is None
    assert fresh.lease_expires_at is None
    assert pending.message_meta["subagent_input_state"] == "pending"


async def test_parent_wake_remains_busy_after_it_emits_tool_rows():
    agent_id, user_id, parent_id, _anchor_id = await _make_context()
    wake_id = uuid.uuid4()
    now = datetime.now(UTC)
    async with async_session() as db:
        db.add(
            ChatMessage(
                id=wake_id,
                agent_id=agent_id,
                user_id=user_id,
                sender_agent_id=agent_id,
                role="user",
                content="durable subagent wake",
                conversation_id=str(parent_id),
                external_event_key=f"subagent-parent:{uuid.uuid4()}",
                message_meta={
                    "kind": "subagent_event",
                    "turn_status": "running",
                    "attachments": [],
                },
                created_at=now,
            )
        )
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                sender_agent_id=agent_id,
                role="tool_call",
                content='{"name":"example","status":"running"}',
                conversation_id=str(parent_id),
                message_meta={
                    "turn_anchor_id": str(wake_id),
                    "attachments": [],
                },
                created_at=now + timedelta(microseconds=1),
            )
        )
        await db.commit()

    async with async_session() as db:
        assert await _has_active_subagent_event_turn(db, str(parent_id))
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                sender_agent_id=agent_id,
                role="assistant",
                content="wake completed",
                conversation_id=str(parent_id),
                message_meta={
                    "turn_anchor_id": str(wake_id),
                    "turn_status": "completed",
                    "attachments": [],
                },
                created_at=now + timedelta(microseconds=2),
            )
        )
        await db.commit()
        assert not await _has_active_subagent_event_turn(db, str(parent_id))


async def test_processing_reclaim_continues_durable_done_tool_tail(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-reclaim",
        task="recover me",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    claimed = await runtime._load_or_start_input(run.id)
    assert claimed is not None
    turn_anchor, recovering = claimed
    assert recovering is False
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "write_file",
                        "call_id": "stable-call-id",
                        "args": {"path": "marker.txt"},
                        "status": "done",
                        "result": "already written",
                    }
                ),
                conversation_id=str(run.id),
                message_meta={"turn_anchor_id": str(turn_anchor.id)},
            )
        )
        await db.commit()

    captured = {}

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del execution_user_id
        return []

    async def fake_llm(_db, _agent_id, user_text, **kwargs):
        captured["user_text"] = user_text
        captured.update(kwargs)
        return "recovered result"

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)

    await runtime.execute_claimed_subagent(run.id)

    assert captured["user_text"] == ""
    assert captured["continue_turn"] is True
    assert captured["recovery_mode"] is True
    assert any(row.get("role") == "tool" for row in captured["history"])
    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
    assert fresh.status == "completed"


async def test_async_parent_event_is_deduplicated_and_keeps_execution_agent(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-wake",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="child-message-one",
        subagent_session_id=str(run.id),
        message="interim",
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="child-message-one",
        subagent_session_id=str(run.id),
        message="interim replay",
    )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="parent idle",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        child_event = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_PARENT_MESSAGE,
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(1)
            )
        ).scalar_one()

    captured = []
    dispatch_kwargs = []

    async def fake_resume(anchor):
        captured.append(anchor)
        return True

    async def fake_run_channel_message(_lock_key, *, work, **kwargs):
        dispatch_kwargs.append(kwargs)
        return await work()

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    monkeypatch.setattr(
        "app.services.channel_dispatch.run_channel_message",
        fake_run_channel_message,
    )
    assert await runtime._dispatch_parent_event(child_event.id)
    assert await runtime._dispatch_parent_event(child_event.id)

    async with async_session() as db:
        anchors = (
            (
                await db.execute(
                    select(ChatMessage).where(ChatMessage.external_event_key == f"subagent-parent:{child_event.id}")
                )
            )
            .scalars()
            .all()
        )
    assert len(anchors) == 1
    assert anchors[0].agent_id == agent_id
    assert anchors[0].sender_agent_id == agent_id
    assert anchors[0].message_meta["execution_agent_id"] == str(agent_id)
    assert captured
    assert dispatch_kwargs
    assert all(kwargs["workload_kind"] is WorkloadKind.PROJECT for kwargs in dispatch_kwargs)
    assert all(kwargs["tenant_id"] != f"web:{parent_id}" for kwargs in dispatch_kwargs)


async def test_idle_parent_batches_multiple_subagent_events_into_one_resume(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-batched-parent-wake",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    for index in range(3):
        await runtime.send_subagent_message_to_parent(
            agent_id=agent_id,
            execution_user_id=user_id,
            origin_tool_call_id=f"batched-child-message-{index}",
            subagent_session_id=str(run.id),
            message=f"result {index}",
        )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="parent idle",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        events = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(run.id),
                        ChatMessage.message_meta["kind"].as_string()
                        == runtime.SUBAGENT_PARENT_MESSAGE,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )

    resumed = []

    async def fake_resume(batch_anchor):
        resumed.append(batch_anchor.id)
        async with async_session() as db:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="one merged parent response",
                    conversation_id=str(parent_id),
                    message_meta={
                        "turn_anchor_id": str(batch_anchor.id),
                        "turn_status": "completed",
                        "attachments": [],
                    },
                )
            )
            await db.commit()
        return True

    async def fake_run_channel_message(_lock_key, *, work, **_kwargs):
        return await work()

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    monkeypatch.setattr(
        "app.services.channel_dispatch.run_channel_message",
        fake_run_channel_message,
    )
    batches, special_events = await runtime._pending_parent_event_groups(
        [event.id for event in events]
    )
    assert special_events == []
    assert batches == [(parent_id, [event.id for event in events])]
    assert await runtime._dispatch_parent_event_batch(batches[0][1])
    assert len(resumed) == 1

    async with async_session() as db:
        projections = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.external_event_key.in_(
                            [f"subagent-parent:{event.id}" for event in events]
                        )
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(projections) == 3
    assert projections[0].id == resumed[0]
    assert projections[0].message_meta["subagent_event_batch"] is True
    assert all(
        row.message_meta["subagent_turn_anchor_id"] == str(resumed[0])
        for row in projections[1:]
    )
    pending = await runtime._pending_parent_events(debounce_seconds=0)
    assert not {event.id for event in events}.intersection(pending)

    async with async_session() as db:
        stored_events = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.id.in_([event.id for event in events])
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {
        row.message_meta["subagent_dispatch_state"] for row in stored_events
    } == {runtime.SUBAGENT_DISPATCH_DELIVERED}


async def test_parent_event_retries_without_duplicate_projection_when_resume_is_busy(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-parent-retry",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="parent-retry-message",
        subagent_session_id=str(run.id),
        message="retry me",
    )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="parent idle",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        event = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string()
                    == runtime.SUBAGENT_PARENT_MESSAGE,
                )
            )
        ).scalar_one()

    resume_calls = 0

    async def fake_resume(batch_anchor):
        nonlocal resume_calls
        resume_calls += 1
        if resume_calls == 1:
            return False
        async with async_session() as db:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="recovered",
                    conversation_id=str(parent_id),
                    message_meta={
                        "turn_anchor_id": str(batch_anchor.id),
                        "turn_status": "completed",
                        "attachments": [],
                    },
                )
            )
            await db.commit()
        return True

    async def fake_run_channel_message(_lock_key, *, work, **_kwargs):
        return await work()

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    monkeypatch.setattr(
        "app.services.channel_dispatch.run_channel_message",
        fake_run_channel_message,
    )

    assert await runtime._dispatch_parent_event_batch([event.id]) is False
    async with async_session() as db:
        pending_event = await db.get(ChatMessage, event.id)
        projection_count = await db.scalar(
            select(func.count(ChatMessage.id)).where(
                ChatMessage.external_event_key == f"subagent-parent:{event.id}"
            )
        )
    assert pending_event.message_meta["subagent_dispatch_state"] == runtime.SUBAGENT_DISPATCH_PENDING
    assert projection_count == 1

    assert await runtime._dispatch_parent_event_batch([event.id]) is True
    async with async_session() as db:
        delivered_event = await db.get(ChatMessage, event.id)
        projection_count = await db.scalar(
            select(func.count(ChatMessage.id)).where(
                ChatMessage.external_event_key == f"subagent-parent:{event.id}"
            )
        )
    assert delivered_event.message_meta["subagent_dispatch_state"] == runtime.SUBAGENT_DISPATCH_DELIVERED
    assert projection_count == 1
    assert resume_calls == 2


async def test_idle_dispatch_repairs_legacy_projection_without_durable_owner():
    from app.services.conversation_turn_lifecycle import (
        conversation_turn_snapshot_for_session,
    )

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-repair-unowned-projection",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    for index in range(2):
        await runtime.send_subagent_message_to_parent(
            agent_id=agent_id,
            execution_user_id=user_id,
            origin_tool_call_id=f"repair-unowned-projection-result-{index}",
            subagent_session_id=str(run.id),
            message=f"repair me {index}",
        )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="parent idle",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.flush()
        events = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string()
                    == runtime.SUBAGENT_PARENT_MESSAGE,
                )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )
        stale_root = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            sender_agent_id=agent_id,
            role="user",
            content="legacy projection without admission",
            conversation_id=str(parent_id),
            external_event_key=f"subagent-parent:{events[0].id}",
            message_meta={
                "kind": runtime.SUBAGENT_PARENT_EVENT,
                "execution_agent_id": str(agent_id),
                "subagent_id": str(run.id),
                "child_message_id": str(events[0].id),
                "turn_status": "running",
                "subagent_event_batch": True,
                "attachments": [],
            },
        )
        db.add(stale_root)
        await db.flush()
        stale_child = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            sender_agent_id=agent_id,
            role="user",
            content="legacy child projection without admission",
            conversation_id=str(parent_id),
            external_event_key=f"subagent-parent:{events[1].id}",
            message_meta={
                "kind": runtime.SUBAGENT_PARENT_EVENT,
                "execution_agent_id": str(agent_id),
                "subagent_id": str(run.id),
                "child_message_id": str(events[1].id),
                "subagent_turn_anchor_id": str(stale_root.id),
                "attachments": [],
            },
        )
        db.add(stale_child)
        await db.commit()
        stale_root_id = stale_root.id
        stale_child_id = stale_child.id

    repaired_root, injected, status = await runtime._materialize_parent_event_batch(
        parent_session_id=parent_id,
        execution_agent_id=agent_id,
        execution_user_id=user_id,
        candidate_ids=[events[0].id],
    )

    assert status == "materialized"
    assert repaired_root is not None and repaired_root.id != stale_root_id
    assert len(injected) == 1 and "repair me 0" in injected[0]["content"]
    async with async_session() as db:
        assert await db.get(ChatMessage, stale_root_id) is None
        assert await db.get(ChatMessage, stale_child_id) is None
        projections = list(
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.external_event_key
                        == f"subagent-parent:{events[0].id}"
                    )
                )
            ).scalars()
        )
        parent = await db.get(ChatSession, parent_id)
    assert [row.id for row in projections] == [repaired_root.id]
    snapshot = conversation_turn_snapshot_for_session(parent)
    assert snapshot.anchor_id == repaired_root.id
    assert snapshot.status == "running"
    await runtime._finish_parent_events_for_root(repaired_root.id, [events[0].id])
    async with async_session() as db:
        from app.services.conversation_turn_lifecycle import (
            transition_conversation_turn,
        )

        await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(parent_id),
            turn_anchor_id=repaired_root.id,
            status="completed",
        )
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="assistant",
                content="first repaired batch complete",
                conversation_id=str(parent_id),
                message_meta={
                    "turn_anchor_id": str(repaired_root.id),
                    "turn_status": "completed",
                    "attachments": [],
                },
            )
        )
        await db.commit()

    second_root, second_injected, second_status = (
        await runtime._materialize_parent_event_batch(
            parent_session_id=parent_id,
            execution_agent_id=agent_id,
            execution_user_id=user_id,
            candidate_ids=[events[1].id],
        )
    )
    assert second_status == "materialized"
    assert second_root is not None and second_root.id != repaired_root.id
    assert len(second_injected) == 1 and "repair me 1" in second_injected[0]["content"]
    await runtime._finish_parent_events_for_root(second_root.id, [events[1].id])
    async with async_session() as db:
        from app.services.conversation_turn_lifecycle import (
            transition_conversation_turn,
        )

        await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(parent_id),
            turn_anchor_id=second_root.id,
            status="completed",
        )
        await db.commit()
