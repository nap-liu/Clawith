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

async def test_active_parent_turn_drains_subagent_events_once_and_tool_tail_recovery_keeps_root():
    from app.services.turn_recovery import _find_turn_anchor_for_latest

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-active-parent-wake",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    for index in range(2):
        await runtime.send_subagent_message_to_parent(
            agent_id=agent_id,
            execution_user_id=user_id,
            origin_tool_call_id=f"active-child-message-{index}",
            subagent_session_id=str(run.id),
            message=f"late result {index}",
        )

    first = await runtime.drain_parent_subagent_events(
        parent_session_id=str(parent_id),
        active_turn_anchor_id=anchor_id,
        execution_agent_id=agent_id,
        execution_user_id=user_id,
    )
    second = await runtime.drain_parent_subagent_events(
        parent_session_id=str(parent_id),
        active_turn_anchor_id=anchor_id,
        execution_agent_id=agent_id,
        execution_user_id=user_id,
    )
    assert len(first) == 2
    assert "late result 0" in first[0]["content"]
    assert "late result 1" in first[1]["content"]
    assert second == []

    async with async_session() as db:
        projections = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(parent_id),
                        ChatMessage.message_meta["subagent_turn_anchor_id"].as_string()
                        == str(anchor_id),
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        tool_tail = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            role="tool_call",
            content=json.dumps(
                {
                    "name": "read_file",
                    "call_id": "active-parent-tool-tail",
                    "args": {"path": "evidence.txt"},
                    "status": "done",
                    "result": "durable evidence",
                }
            ),
            conversation_id=str(parent_id),
            message_meta={
                "turn_anchor_id": str(anchor_id),
                "attachments": [],
            },
            created_at=projections[-1].created_at + timedelta(microseconds=1),
        )
        db.add(tool_tail)
        await db.commit()
        recovered_from_projection = await _find_turn_anchor_for_latest(db, projections[-1])
        recovered_from_tool_tail = await _find_turn_anchor_for_latest(db, tool_tail)
    assert len(projections) == 2
    assert recovered_from_projection.id == anchor_id
    assert recovered_from_tool_tail.id == anchor_id


async def test_channel_llm_composes_parent_event_drain_with_existing_round_hook(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context(parent_channel="dingtalk")
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-channel-parent-hook",
        task="child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="channel-parent-hook-result",
        subagent_session_id=str(run.id),
        message="channel late result",
    )
    captured: list[dict] = []

    async def upstream(_round_i: int) -> list[dict]:
        return [{"role": "user", "content": "existing round input"}]

    async def fake_provider_dispatch(**kwargs):
        captured.extend(await kwargs["before_round"](0))
        return "merged channel answer"

    monkeypatch.setattr(
        "app.services.llm.call_llm_with_failover",
        fake_provider_dispatch,
    )
    async with async_session() as db:
        parent = await db.get(ChatSession, parent_id)
        reply = await channel_llm._call_agent_llm(
            db,
            agent_id,
            "parent request",
            session_id=str(parent_id),
            user_id=user_id,
            prepared_tools=[],
            include_soul=False,
            include_memory=False,
            broadcast_web=False,
            runtime_session=parent,
            turn_anchor_id=anchor_id,
            before_round=upstream,
        )
    assert reply == "merged channel answer"
    assert captured[0] == {"role": "user", "content": "existing round input"}
    assert "channel late result" in captured[1]["content"]


async def test_project_parent_event_stays_on_special_materialization_path(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context(
        parent_channel="project",
        project=True,
    )
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-project-special-parent-wake",
        task="project child",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="project-special-result",
        subagent_session_id=str(run.id),
        message="project result",
    )
    async with async_session() as db:
        event = await db.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(run.id),
                ChatMessage.message_meta["kind"].as_string()
                == runtime.SUBAGENT_PARENT_MESSAGE,
            )
            .limit(1)
        )

    batches, special_events = await runtime._pending_parent_event_groups([event.id])
    assert batches == []
    assert special_events == [event.id]

    async def fail_batch(_message_ids):
        raise AssertionError("project parent event must not enter ordinary batching")

    monkeypatch.setattr(runtime, "_dispatch_parent_event_batch", fail_batch)
    assert await runtime._dispatch_parent_event(event.id)
    async with async_session() as db:
        projected = await db.scalar(
            select(ChatMessage).where(
                ChatMessage.external_event_key == f"project-subagent:{event.id}"
            )
        )
        ordinary = await db.scalar(
            select(ChatMessage.id).where(
                ChatMessage.external_event_key == f"subagent-parent:{event.id}"
            )
        )
    assert projected.role == "assistant"
    assert projected.message_meta["kind"] == "project_subagent_reply"
    assert ordinary is None


async def test_sync_execution_reuses_unified_llm_and_persists_terminal_result(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-sync",
        task="do it",
        mode="sync",
        model="Readable-Test-Model",
        turn_anchor_id=anchor_id,
    )
    captured = {}

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del execution_user_id
        return [{"type": "function", "function": {"name": "safe", "parameters": {}}}]

    async def fake_llm(_db, _agent_id, user_text, **kwargs):
        captured.update(kwargs)
        captured["user_text"] = user_text
        assert await kwargs["before_round"](0) == []
        await kwargs["on_thinking"]("child hidden reasoning")
        await runtime.send_subagent_message_to_parent(
            agent_id=_agent_id,
            execution_user_id=user_id,
            origin_tool_call_id="sync-message-one",
            subagent_session_id=kwargs["session_id"],
            message="sync interim",
        )
        return "child result"

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)
    status, result, parent_messages = await runtime.run_subagent_sync(run.id)

    assert (status, result) == ("completed", "child result")
    assert parent_messages == ["sync interim"]
    assert captured["model_name"] == "Readable-Test-Model"
    assert captured["broadcast_web"] is True
    assert captured["turn_anchor_id"] is not None
    assert captured["continue_turn"] is False
    assert captured["recovery_mode"] is False
    assert captured["prepared_tools"][0]["function"]["name"] == "safe"
    async with async_session() as db:
        final = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_COMPLETION,
                )
                .limit(1)
            )
        ).scalar_one()
    assert final.content == "child result"
    assert final.thinking == "child hidden reasoning"
    assert final.message_meta["subagent_wake"] is False


async def test_subagent_confirmation_suspends_and_resumes_durable_turn(monkeypatch):
    from app.services import confirmation_service
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-confirmation",
        task="confirm before continuing",
        mode="async",
        model="Readable-Test-Model",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id
    invocations = []
    pending_call_id = None

    async def fake_tools(_agent_id, _session_id=None, execution_user_id=None):
        del _agent_id, _session_id, execution_user_id
        return []

    async def fake_llm(_db, _agent_id, user_text, **kwargs):
        nonlocal pending_call_id
        invocations.append(dict(kwargs, user_text=user_text))
        if len(invocations) == 1:
            pending_call_id = await confirmation_service.suspend_for_confirmation(
                agent_id=_agent_id,
                conversation_id=kwargs["session_id"],
                chat_session_id=uuid.UUID(kwargs["session_id"]),
                source_channel="subagent",
                user_id=user_id,
                intro_text="需要确认",
                title="继续执行",
                summary="确认后继续当前项目任务",
                action={
                    "tool": "write_file",
                    "args": {"workspace": "project", "path": "ok.txt"},
                },
                risk_level="medium",
                buttons=[{"label": "继续", "value": "continue"}],
                force_confirmation=True,
                turn_anchor_id=kwargs["turn_anchor_id"],
            )
            return ""
        assert kwargs["continue_turn"] is True
        assert kwargs["recovery_mode"] is True
        assert any(row.get("role") == "tool" for row in kwargs["history"])
        await kwargs["on_thinking"]("resumed hidden reasoning")
        return "confirmed child result"

    async def no_origin_card(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)
    monkeypatch.setattr(confirmation_service, "_update_origin_card", no_origin_card)

    await runtime.execute_claimed_subagent(run.id)
    assert pending_call_id is not None
    async with async_session() as db:
        parked = await db.get(SubagentRun, run.id)
        processing = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_INPUT,
                )
            )
        ).scalar_one()
        assert parked.status == runtime.RUN_WAITING
        assert processing.message_meta["subagent_input_state"] == runtime.INPUT_PROCESSING
        parked_current = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
        parked_exact = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
            turn_anchor_id=processing.id,
        )
        assert parked_current == parked_exact
        assert parked_current.status == "suspended"

    resolved = await confirmation_service.resolve_confirmation(
        agent_id=agent_id,
        call_id=pending_call_id,
        button_value="continue",
        button_label="继续",
        resolving_user_id=user_id,
    )
    assert resolved and "继续" in resolved
    assert len(invocations) == 2
    async with async_session() as db:
        completed = await db.get(SubagentRun, run.id)
        confirmation = await db.get(ChatMessage, pending_call_id)
        final = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_COMPLETION,
                )
                .limit(1)
            )
        ).scalar_one()
        completed_current = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
        )
        completed_exact = await get_conversation_turn_snapshot(
            db,
            agent_id=agent_id,
            conversation_id=str(run.id),
            turn_anchor_id=processing.id,
        )
    assert completed.status == runtime.RUN_COMPLETED
    assert json.loads(confirmation.content)["status"] == "done"
    assert final.content == "confirmed child result"
    assert final.thinking == "resumed hidden reasoning"
    assert completed_current == completed_exact
    assert completed_current.status == "completed"


async def test_revoked_execution_user_fails_before_llm_or_tool_side_effect(monkeypatch):
    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-revoked-before-run",
        task="must not execute",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    assert await runtime._claim_subagent(run.id) == run.id

    async with async_session() as db:
        user = await db.get(User, user_id)
        user.is_active = False
        await db.commit()

    async def fail_llm(*_args, **_kwargs):
        raise AssertionError("revoked execution identity must fail before LLM dispatch")

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fail_llm)
    await runtime.execute_claimed_subagent(run.id)

    async with async_session() as db:
        fresh = await db.get(SubagentRun, run.id)
        failure = await db.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(run.id),
                ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_FAILURE,
            )
            .limit(1)
        )
    assert fresh.status == "failed"
    assert failure.content == "本次执行未完成，请稍后重试或查看项目状态。"


async def test_child_is_hidden_from_lists_but_direct_web_detail_is_accessible():
    from app.api.chat_sessions import (
        _build_session_detail_out,
        _get_session_messages_page,
        _load_accessible_session,
    )
    from app.services.tools.session_introspection import handle_list_sessions

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-session-visibility",
        task="visible to session tools",
        mode="async",
        turn_anchor_id=anchor_id,
    )
    raw = await handle_list_sessions(
        agent_id,
        user_id,
        str(parent_id),
        {"raw": True, "limit": 50},
    )
    assert str(run.id) in raw

    async with async_session() as db:
        user = await db.get(User, user_id)
        _agent, direct_child, view_scope = await _load_accessible_session(db, user, agent_id, run.id)
        detail = await _build_session_detail_out(db, direct_child, view_scope)
        child_rows = await _get_session_messages_page(
            agent_id=agent_id,
            session_id=run.id,
            limit=100,
            turn_limit=None,
            before=None,
            current_user=user,
            db=db,
            response=None,
        )
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                sender_agent_id=agent_id,
                role="user",
                content="internal wake anchor",
                conversation_id=str(parent_id),
                message_meta={"kind": runtime.SUBAGENT_PARENT_EVENT},
            )
        )
        await db.commit()
        web_rows = await _get_session_messages_page(
            agent_id=agent_id,
            session_id=parent_id,
            limit=100,
            turn_limit=None,
            before=None,
            current_user=user,
            db=db,
            response=None,
        )
    assert direct_child.id == run.id
    assert view_scope == "mine"
    assert detail.runtime is not None
    assert detail.runtime.kind == "subagent"
    assert detail.runtime.status == "queued"
    assert detail.runtime.execution_agent_id == str(agent_id)
    assert any(row.get("content") == "visible to session tools" for row in child_rows)
    assert all(row.get("content") != "internal wake anchor" for row in web_rows)


async def test_nonhuman_parent_child_uses_execution_owner_for_standard_access():
    from app.api.chat_sessions import (
        PatchSessionIn,
        _load_accessible_session,
        delete_session,
        rename_session,
    )
    from app.services.tools.session_introspection import handle_list_sessions

    agent_id, user_id, parent_id, anchor_id = await _make_context(parent_channel="trigger")
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-trigger-owner",
        task="trigger-owned child",
        mode="async",
        turn_anchor_id=anchor_id,
    )

    raw = await handle_list_sessions(
        agent_id,
        user_id,
        str(parent_id),
        {"raw": True, "limit": 50},
    )
    assert str(run.id) in raw

    async with async_session() as db:
        user = await db.get(User, user_id)
        child = await db.get(ChatSession, run.id)
        assert child is not None and child.user_id is None
        _agent, direct_child, view_scope = await _load_accessible_session(db, user, agent_id, run.id)
        assert direct_child.id == run.id
        assert view_scope == "mine"

        with pytest.raises(HTTPException) as rename_error:
            await rename_session(
                agent_id,
                run.id,
                PatchSessionIn(title="must not change"),
                current_user=user,
                db=db,
            )
        assert rename_error.value.status_code == 409

        with pytest.raises(HTTPException) as delete_error:
            await delete_session(
                agent_id,
                run.id,
                current_user=user,
                db=db,
            )
        assert delete_error.value.status_code == 409


async def test_parent_with_subagent_audit_record_cannot_be_deleted():
    from app.api.chat_sessions import delete_session

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-parent-delete-guard",
        task="retain child audit",
        mode="async",
        turn_anchor_id=anchor_id,
    )

    async with async_session() as db:
        user = await db.get(User, user_id)
        with pytest.raises(HTTPException) as delete_error:
            await delete_session(
                agent_id,
                parent_id,
                current_user=user,
                db=db,
            )
        assert delete_error.value.status_code == 409
        assert await db.get(ChatSession, parent_id) is not None
        assert await db.get(ChatSession, run.id) is not None
        assert await db.get(SubagentRun, run.id) is not None


async def test_agent_with_subagent_audit_record_cannot_be_deleted():
    from app.api.agents import delete_agent

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-agent-delete-guard",
        task="retain agent audit",
        mode="async",
        turn_anchor_id=anchor_id,
    )

    async with async_session() as db:
        user = await db.get(User, user_id)
        with pytest.raises(HTTPException) as delete_error:
            await delete_agent(
                agent_id,
                current_user=user,
                db=db,
            )
        assert delete_error.value.status_code == 409
        assert await db.get(Agent, agent_id) is not None
        assert await db.get(ChatSession, run.id) is not None
        assert await db.get(SubagentRun, run.id) is not None
