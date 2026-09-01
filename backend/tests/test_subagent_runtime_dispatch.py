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

async def test_idle_dispatch_repairs_projection_whose_root_is_missing():
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-repair-missing-root",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="repair-missing-root-result",
        subagent_session_id=str(run.id),
        message="repair missing root",
    )
    missing_root_id = uuid.uuid4()
    async with async_session() as db:
        event = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string()
                    == runtime.SUBAGENT_PARENT_MESSAGE,
                )
            )
        ).scalar_one()
        broken_projection = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            sender_agent_id=agent_id,
            role="user",
            content="projection with missing root",
            conversation_id=str(parent_id),
            external_event_key=f"subagent-parent:{event.id}",
            message_meta={
                "kind": runtime.SUBAGENT_PARENT_EVENT,
                "execution_agent_id": str(agent_id),
                "subagent_id": str(run.id),
                "child_message_id": str(event.id),
                "subagent_turn_anchor_id": str(missing_root_id),
                "attachments": [],
            },
        )
        db.add_all(
            [
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="parent idle",
                    conversation_id=str(parent_id),
                    message_meta={"attachments": []},
                ),
                broken_projection,
            ]
        )
        await db.commit()
        broken_projection_id = broken_projection.id

    repaired_root, injected, status = await runtime._materialize_parent_event_batch(
        parent_session_id=parent_id,
        execution_agent_id=agent_id,
        execution_user_id=user_id,
        candidate_ids=[event.id],
    )
    assert status == "materialized"
    assert repaired_root is not None and repaired_root.id != missing_root_id
    assert injected and "repair missing root" in injected[0]["content"]
    async with async_session() as db:
        assert await db.get(ChatMessage, broken_projection_id) is None
    await runtime._finish_parent_events_for_root(repaired_root.id, [event.id])
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
        await db.commit()


async def test_parent_event_terminal_crash_window_converges_without_second_resume(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-parent-terminal-window",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="parent-terminal-window-message",
        subagent_session_id=str(run.id),
        message="finish before state update",
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
        async with async_session() as db:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="already durable",
                    conversation_id=str(parent_id),
                    message_meta={
                        "turn_anchor_id": str(batch_anchor.id),
                        "turn_status": "completed",
                        "attachments": [],
                    },
                )
            )
            await db.commit()
        return False

    async def fake_run_channel_message(_lock_key, *, work, **_kwargs):
        return await work()

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    monkeypatch.setattr(
        "app.services.channel_dispatch.run_channel_message",
        fake_run_channel_message,
    )

    assert await runtime._dispatch_parent_event_batch([event.id]) is False
    assert await runtime._dispatch_parent_event_batch([event.id]) is True
    async with async_session() as db:
        stored_event = await db.get(ChatMessage, event.id)
    assert stored_event.message_meta["subagent_dispatch_state"] == runtime.SUBAGENT_DISPATCH_DELIVERED
    assert resume_calls == 1


async def test_committed_parent_event_wakes_dispatcher_within_batch_window(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-event-driven-parent",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
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
                    content="event-driven reply",
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

    startup_scan_complete = asyncio.Event()
    original_pending_parent_events = runtime._pending_parent_events

    async def observed_pending_parent_events(**kwargs):
        result = await original_pending_parent_events(**kwargs)
        if kwargs.get("project_scope") is False:
            startup_scan_complete.set()
        return result

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    monkeypatch.setattr(
        "app.services.channel_dispatch.run_channel_message",
        fake_run_channel_message,
    )
    monkeypatch.setattr(runtime, "PARENT_EVENT_BATCH_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(runtime, "DISPATCH_RETRY_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(runtime, "DISPATCH_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "LEGACY_PARENT_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "_pending_parent_events", observed_pending_parent_events)

    monkeypatch.setattr(runtime, "_dispatch_wakeup", asyncio.Event())
    daemon = asyncio.create_task(runtime._subagent_parent_dispatch_loop())
    try:
        await asyncio.wait_for(startup_scan_complete.wait(), timeout=1)
        await asyncio.sleep(0.02)
        started_at = asyncio.get_running_loop().time()
        await runtime.send_subagent_message_to_parent(
            agent_id=agent_id,
            execution_user_id=user_id,
            origin_tool_call_id="event-driven-parent-message",
            subagent_session_id=str(run.id),
            message="wake now",
        )
        projection = None
        source = None
        while asyncio.get_running_loop().time() - started_at < 1.5:
            async with async_session() as db:
                projection = (
                    await db.execute(
                        select(ChatMessage).where(
                            ChatMessage.external_event_key.like("subagent-parent:%"),
                            ChatMessage.conversation_id == str(parent_id),
                        )
                    )
                ).scalar_one_or_none()
                source = (
                    await db.execute(
                        select(ChatMessage).where(
                            ChatMessage.conversation_id == str(run.id),
                            ChatMessage.message_meta["kind"].as_string()
                            == runtime.SUBAGENT_PARENT_MESSAGE,
                        )
                    )
                ).scalar_one()
            if (
                projection is not None
                and source.message_meta.get("subagent_dispatch_state")
                == runtime.SUBAGENT_DISPATCH_DELIVERED
            ):
                break
            await asyncio.sleep(0.05)
        assert projection is not None
        assert asyncio.get_running_loop().time() - started_at < 1.5
        assert source.message_meta["subagent_dispatch_state"] == runtime.SUBAGENT_DISPATCH_DELIVERED
        assert resume_calls == 2
        async with async_session() as db:
            projection_count = await db.scalar(
                select(func.count(ChatMessage.id)).where(
                    ChatMessage.external_event_key.like("subagent-parent:%"),
                    ChatMessage.conversation_id == str(parent_id),
                )
            )
        assert projection_count == 1
    finally:
        daemon.cancel()
        with pytest.raises(asyncio.CancelledError):
            await daemon


async def test_idle_parent_dispatcher_does_not_poll_between_recovery_passes(monkeypatch):
    scan_scopes = []
    leader_scan_calls = 0
    recovery_calls = 0

    async def no_parent_events(**_kwargs):
        scan_scopes.append(_kwargs.get("project_scope"))
        return []

    async def no_parent_groups(_pending):
        return [], []

    async def no_leader_groups(**_kwargs):
        nonlocal leader_scan_calls
        leader_scan_calls += 1
        return []

    async def no_project_recovery():
        nonlocal recovery_calls
        recovery_calls += 1
        return False

    monkeypatch.setattr(runtime, "PARENT_EVENT_BATCH_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(runtime, "DISPATCH_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "LEGACY_PARENT_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "_pending_parent_events", no_parent_events)
    monkeypatch.setattr(runtime, "_pending_parent_event_groups", no_parent_groups)
    monkeypatch.setattr(runtime, "_pending_project_leader_groups", no_leader_groups)
    monkeypatch.setattr(runtime, "_recover_project_dispatch_outbox_once", no_project_recovery)

    monkeypatch.setattr(runtime, "_dispatch_wakeup", asyncio.Event())
    monkeypatch.setattr(runtime, "_project_dispatch_wakeup", asyncio.Event())
    daemons = [
        asyncio.create_task(runtime._subagent_parent_dispatch_loop()),
        asyncio.create_task(runtime._project_dispatch_loop()),
    ]
    try:
        await asyncio.sleep(0.1)
        assert scan_scopes.count(False) == 1
        assert scan_scopes.count(True) == 1
        assert leader_scan_calls == 1
        assert recovery_calls == 1
    finally:
        for daemon in daemons:
            daemon.cancel()
        await asyncio.gather(*daemons, return_exceptions=True)


async def test_project_dispatch_signal_wakes_stable_leader_loop(monkeypatch):
    broken_group_id = uuid.uuid4()
    group_id = uuid.uuid4()
    startup_scan_complete = asyncio.Event()
    dispatched = asyncio.Event()
    leader_ready = False
    dispatch_calls = 0

    async def no_parent_events(**_kwargs):
        return []

    async def no_parent_groups(_pending):
        return [], []

    async def leader_groups(**_kwargs):
        startup_scan_complete.set()
        return [broken_group_id, group_id] if leader_ready else []

    async def dispatch_leader(candidate_group_id, **_kwargs):
        nonlocal dispatch_calls
        if candidate_group_id == broken_group_id:
            raise RuntimeError("isolated project failure")
        assert candidate_group_id == group_id
        dispatch_calls += 1
        dispatched.set()
        return True

    async def no_project_recovery():
        return False

    monkeypatch.setattr(runtime, "PARENT_EVENT_BATCH_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(runtime, "DISPATCH_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "LEGACY_PARENT_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "_pending_parent_events", no_parent_events)
    monkeypatch.setattr(runtime, "_pending_parent_event_groups", no_parent_groups)
    monkeypatch.setattr(runtime, "_pending_project_leader_groups", leader_groups)
    monkeypatch.setattr(runtime, "_dispatch_project_leader_batch", dispatch_leader)
    monkeypatch.setattr(runtime, "_recover_project_dispatch_outbox_once", no_project_recovery)

    monkeypatch.setattr(runtime, "_project_dispatch_wakeup", asyncio.Event())
    daemon = asyncio.create_task(runtime._project_dispatch_loop())
    try:
        await asyncio.wait_for(startup_scan_complete.wait(), timeout=1)
        await asyncio.sleep(0.02)
        leader_ready = True
        runtime._signal_project_dispatch_work()
        await asyncio.wait_for(dispatched.wait(), timeout=1)
        assert dispatch_calls == 1
    finally:
        daemon.cancel()
        with pytest.raises(asyncio.CancelledError):
            await daemon


async def test_stalled_project_dispatch_does_not_block_ordinary_parent_wake(monkeypatch):
    ordinary_scans = 0
    startup_complete = asyncio.Event()
    project_dispatch_started = asyncio.Event()
    hold_project_dispatch = asyncio.Event()
    project_ready = False
    group_id = uuid.uuid4()

    async def no_parent_events(**kwargs):
        nonlocal ordinary_scans
        if kwargs.get("project_scope") is False:
            ordinary_scans += 1
        if ordinary_scans and kwargs.get("project_scope") is True:
            startup_complete.set()
        return []

    async def no_parent_groups(_pending):
        return [], []

    async def leader_groups(**_kwargs):
        return [group_id] if project_ready else []

    async def stalled_leader_dispatch(_group_id, **_kwargs):
        project_dispatch_started.set()
        await hold_project_dispatch.wait()
        return True

    async def no_project_recovery():
        return False

    monkeypatch.setattr(runtime, "PARENT_EVENT_BATCH_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(runtime, "DISPATCH_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "LEGACY_PARENT_RECOVERY_INTERVAL_SECONDS", 10.0)
    monkeypatch.setattr(runtime, "_pending_parent_events", no_parent_events)
    monkeypatch.setattr(runtime, "_pending_parent_event_groups", no_parent_groups)
    monkeypatch.setattr(runtime, "_pending_project_leader_groups", leader_groups)
    monkeypatch.setattr(runtime, "_dispatch_project_leader_batch", stalled_leader_dispatch)
    monkeypatch.setattr(runtime, "_recover_project_dispatch_outbox_once", no_project_recovery)
    monkeypatch.setattr(runtime, "_dispatch_wakeup", asyncio.Event())
    monkeypatch.setattr(runtime, "_project_dispatch_wakeup", asyncio.Event())

    daemons = [
        asyncio.create_task(runtime._subagent_parent_dispatch_loop()),
        asyncio.create_task(runtime._project_dispatch_loop()),
    ]
    try:
        await asyncio.wait_for(startup_complete.wait(), timeout=1)
        project_ready = True
        runtime._signal_project_dispatch_work()
        await asyncio.wait_for(project_dispatch_started.wait(), timeout=1)
        scans_before_wake = ordinary_scans
        runtime._signal_dispatch_work()
        while ordinary_scans == scans_before_wake:
            await asyncio.sleep(0.01)
        assert ordinary_scans == scans_before_wake + 1
    finally:
        for daemon in daemons:
            daemon.cancel()
        await asyncio.gather(*daemons, return_exceptions=True)


async def test_concurrent_parent_dispatchers_resume_one_batch_once(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-concurrent-parent-wake",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    for index in range(3):
        await runtime.send_subagent_message_to_parent(
            agent_id=agent_id,
            execution_user_id=user_id,
            origin_tool_call_id=f"concurrent-child-message-{index}",
            subagent_session_id=str(run.id),
            message=f"concurrent result {index}",
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
        event_ids = list(
            (
                await db.execute(
                    select(ChatMessage.id)
                    .where(
                        ChatMessage.conversation_id == str(run.id),
                        ChatMessage.message_meta["kind"].as_string()
                        == runtime.SUBAGENT_PARENT_MESSAGE,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )

    resumed: list[uuid.UUID] = []

    async def fake_resume(batch_anchor):
        resumed.append(batch_anchor.id)
        await asyncio.sleep(0.1)
        async with async_session() as db:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="one concurrent parent response",
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

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    await asyncio.gather(
        runtime._dispatch_parent_event_batch(event_ids),
        runtime._dispatch_parent_event_batch(event_ids),
    )

    async with async_session() as db:
        projection_count = await db.scalar(
            select(func.count(ChatMessage.id)).where(
                ChatMessage.external_event_key.in_(
                    [f"subagent-parent:{event_id}" for event_id in event_ids]
                )
            )
        )
    assert len(resumed) == 1
    assert projection_count == 3


async def test_compat_single_event_dispatch_resolves_materialized_batch_root_once(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-compat-parent-wake",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    for index in range(3):
        await runtime.send_subagent_message_to_parent(
            agent_id=agent_id,
            execution_user_id=user_id,
            origin_tool_call_id=f"compat-child-message-{index}",
            subagent_session_id=str(run.id),
            message=f"compat result {index}",
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
        event_ids = list(
            (
                await db.execute(
                    select(ChatMessage.id)
                    .where(
                        ChatMessage.conversation_id == str(run.id),
                        ChatMessage.message_meta["kind"].as_string()
                        == runtime.SUBAGENT_PARENT_MESSAGE,
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )

    root, _injected, status = await runtime._materialize_parent_event_batch(
        parent_session_id=parent_id,
        execution_agent_id=agent_id,
        execution_user_id=user_id,
        candidate_ids=event_ids,
    )
    assert status == "materialized"
    resumed: list[uuid.UUID] = []

    async def fake_resume(batch_root):
        resumed.append(batch_root.id)
        async with async_session() as db:
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    role="assistant",
                    content="compat completed",
                    conversation_id=str(parent_id),
                    message_meta={
                        "turn_anchor_id": str(batch_root.id),
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
    assert await runtime._dispatch_parent_event(event_ids[1])
    assert await runtime._dispatch_parent_event(event_ids[2])
    assert resumed == [root.id]
    pending = await runtime._pending_parent_events(debounce_seconds=0)
    assert not set(event_ids).intersection(pending)
