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

async def test_company_delete_helper_releases_subagent_session_tree():
    from app.api.admin import _delete_company_subagent_runs

    agent_id, user_id, parent_id, anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-company-delete",
        task="company purge child",
        mode="async",
        turn_anchor_id=anchor_id,
    )

    async with async_session() as db:
        await _delete_company_subagent_runs(db, [agent_id])
        await db.execute(delete(ChatMessage).where(ChatMessage.conversation_id.in_([str(run.id), str(parent_id)])))
        await db.execute(delete(ChatSession).where(ChatSession.id.in_([run.id, parent_id])))
        await db.commit()
        assert await db.get(SubagentRun, run.id) is None
        assert await db.get(ChatSession, run.id) is None
        assert await db.get(ChatSession, parent_id) is None


async def test_round_inbox_survives_first_dispatch_context_recovery(monkeypatch):
    from app.services.llm import caller

    captured = {}

    async def fake_turn_context(**_kwargs):
        return "system", "dynamic"

    async def fake_call_llm(_model, _messages, _name, _role, **kwargs):
        assert await kwargs["before_round"](0) == [{"role": "user", "content": "late message"}]
        captured["recovered"] = await kwargs["context_recovery"](_model, None)
        return "ok"

    async def recover(_model, _budget):
        return [{"role": "user", "content": "compacted current"}]

    delivered = False

    async def before_round(_round):
        nonlocal delivered
        if delivered:
            return []
        delivered = True
        return [{"role": "user", "content": "late message"}]

    monkeypatch.setattr(caller, "_build_turn_context", fake_turn_context)
    monkeypatch.setattr(caller, "call_llm", fake_call_llm)
    model = SimpleNamespace(id=uuid.uuid4(), provider="test", model="primary")
    result = await caller.call_llm_with_failover(
        primary_model=model,
        fallback_model=None,
        messages=[{"role": "user", "content": "current"}],
        agent_name="Agent",
        role_description="",
        prepared_tools=[],
        context_recovery=recover,
        before_round=before_round,
    )
    assert result == "ok"
    assert captured["recovered"] == [
        {"role": "user", "content": "compacted current"},
        {"role": "user", "content": "late message"},
    ]


async def test_round_inbox_recovery_deduplicates_empty_attachment_shape_by_count(
    monkeypatch,
):
    from app.services.llm import caller

    captured = {}

    async def fake_turn_context(**_kwargs):
        return "system", "dynamic"

    async def fake_call_llm(_model, _messages, _name, _role, **kwargs):
        live = await kwargs["before_round"](0)
        assert live == [
            {"role": "user", "content": "same late message"},
            {"role": "user", "content": "same late message"},
        ]
        captured["recovered"] = await kwargs["context_recovery"](_model, None)
        return "ok"

    async def recover(_model, _budget):
        return [
            {"role": "user", "content": "compacted current", "attachments": []},
            {"role": "user", "content": "same late message", "attachments": []},
            {"role": "user", "content": "same late message", "attachments": []},
        ]

    async def before_round(_round):
        return [
            {"role": "user", "content": "same late message"},
            {"role": "user", "content": "same late message"},
        ]

    monkeypatch.setattr(caller, "_build_turn_context", fake_turn_context)
    monkeypatch.setattr(caller, "call_llm", fake_call_llm)
    model = SimpleNamespace(id=uuid.uuid4(), provider="test", model="primary")

    result = await caller.call_llm_with_failover(
        primary_model=model,
        fallback_model=None,
        messages=[{"role": "user", "content": "current"}],
        agent_name="Agent",
        role_description="",
        prepared_tools=[],
        context_recovery=recover,
        before_round=before_round,
    )

    assert result == "ok"
    assert captured["recovered"] == [
        {"role": "user", "content": "compacted current", "attachments": []},
        {"role": "user", "content": "same late message", "attachments": []},
        {"role": "user", "content": "same late message", "attachments": []},
    ]


async def test_round_inbox_recovery_does_not_match_injection_against_same_text_root(
    monkeypatch,
):
    from app.services.llm import caller

    captured = {}

    async def fake_turn_context(**_kwargs):
        return "system", "dynamic"

    async def fake_call_llm(_model, _messages, _name, _role, **kwargs):
        assert await kwargs["before_round"](0) == [
            {"role": "user", "content": "continue"}
        ]
        captured["recovered"] = await kwargs["context_recovery"](_model, None)
        return "ok"

    async def recover(_model, _budget):
        # The durable snapshot contains the pre-existing root but a read race
        # has not exposed the trailing, identically-worded injection yet.
        return [{"role": "user", "content": "continue", "attachments": []}]

    async def before_round(_round):
        return [{"role": "user", "content": "continue"}]

    monkeypatch.setattr(caller, "_build_turn_context", fake_turn_context)
    monkeypatch.setattr(caller, "call_llm", fake_call_llm)
    model = SimpleNamespace(id=uuid.uuid4(), provider="test", model="primary")

    result = await caller.call_llm_with_failover(
        primary_model=model,
        fallback_model=None,
        messages=[{"role": "user", "content": "continue"}],
        agent_name="Agent",
        role_description="",
        prepared_tools=[],
        context_recovery=recover,
        before_round=before_round,
    )

    assert result == "ok"
    assert captured["recovered"] == [
        {"role": "user", "content": "continue", "attachments": []},
        {"role": "user", "content": "continue"},
    ]


async def test_round_inbox_recovery_does_not_reserve_compacted_old_same_text(
    monkeypatch,
):
    from app.services.llm import caller

    captured = {}

    async def fake_turn_context(**_kwargs):
        return "system", "dynamic"

    async def fake_call_llm(_model, _messages, _name, _role, **kwargs):
        await kwargs["before_round"](0)
        captured["recovered"] = await kwargs["context_recovery"](_model, None)
        return "ok"

    async def recover(_model, _budget):
        return [
            {"role": "system", "content": "compacted old history"},
            {"role": "user", "content": "different current root", "attachments": []},
            {"role": "user", "content": "continue", "attachments": []},
        ]

    async def before_round(_round):
        return [{"role": "user", "content": "continue"}]

    monkeypatch.setattr(caller, "_build_turn_context", fake_turn_context)
    monkeypatch.setattr(caller, "call_llm", fake_call_llm)
    model = SimpleNamespace(id=uuid.uuid4(), provider="test", model="primary")

    result = await caller.call_llm_with_failover(
        primary_model=model,
        fallback_model=None,
        messages=[
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": "different current root"},
        ],
        agent_name="Agent",
        role_description="",
        prepared_tools=[],
        context_recovery=recover,
        before_round=before_round,
    )

    assert result == "ok"
    assert captured["recovered"] == [
        {"role": "system", "content": "compacted old history"},
        {"role": "user", "content": "different current root", "attachments": []},
        {"role": "user", "content": "continue", "attachments": []},
    ]


async def test_round_inbox_recovery_does_not_reserve_protected_old_same_text(
    monkeypatch,
):
    from app.services.llm import caller

    captured = {}

    async def fake_turn_context(**_kwargs):
        return "system", "dynamic"

    async def fake_call_llm(_model, _messages, _name, _role, **kwargs):
        await kwargs["before_round"](0)
        captured["recovered"] = await kwargs["context_recovery"](_model, None)
        return "ok"

    async def recover(_model, _budget):
        # The protected baseline suffix survives intact, but the racing read
        # does not yet contain the identically-worded trailing injection.
        return [
            {"role": "user", "content": "continue", "attachments": []},
            {"role": "assistant", "content": "older reply"},
            {"role": "user", "content": "current root", "attachments": []},
        ]

    async def before_round(_round):
        return [{"role": "user", "content": "continue"}]

    monkeypatch.setattr(caller, "_build_turn_context", fake_turn_context)
    monkeypatch.setattr(caller, "call_llm", fake_call_llm)
    model = SimpleNamespace(id=uuid.uuid4(), provider="test", model="primary")

    result = await caller.call_llm_with_failover(
        primary_model=model,
        fallback_model=None,
        messages=[
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "older reply"},
            {"role": "user", "content": "current root"},
        ],
        agent_name="Agent",
        role_description="",
        prepared_tools=[],
        context_recovery=recover,
        before_round=before_round,
    )

    assert result == "ok"
    assert captured["recovered"] == [
        {"role": "user", "content": "continue", "attachments": []},
        {"role": "assistant", "content": "older reply"},
        {"role": "user", "content": "current root", "attachments": []},
        {"role": "user", "content": "continue"},
    ]


async def test_a2a_parent_wake_distinguishes_execution_and_storage_agent(monkeypatch):
    first_agent_id, user_id, _parent_id, _anchor_id = await _make_context()
    second_agent_id = uuid.uuid4()
    storage_agent_id, execution_agent_id = sorted(
        [first_agent_id, second_agent_id],
        key=str,
    )
    async with async_session() as db:
        first_agent = await db.get(Agent, first_agent_id)
        if second_agent_id != first_agent_id:
            db.add(
                Agent(
                    id=second_agent_id,
                    name="A2A peer",
                    creator_id=user_id,
                    tenant_id=first_agent.tenant_id,
                    primary_model_id=first_agent.primary_model_id,
                    status="idle",
                )
            )
        parent = ChatSession(
            agent_id=storage_agent_id,
            peer_agent_id=execution_agent_id,
            user_id=None,
            title="A2A parent",
            source_channel="agent",
            is_primary=False,
            is_group=False,
        )
        db.add(parent)
        await db.flush()
        db.add(
            ChatMessage(
                agent_id=storage_agent_id,
                user_id=user_id,
                sender_agent_id=execution_agent_id,
                role="assistant",
                content="idle",
                conversation_id=str(parent.id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        parent_id = parent.id

    run, _ = await runtime.create_subagent(
        agent_id=execution_agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-a2a-wake",
        task="A2A child",
        mode="async",
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=execution_agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="a2a-message-one",
        subagent_session_id=str(run.id),
        message="A2A interim",
    )
    async with async_session() as db:
        event = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(run.id),
                    ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_PARENT_MESSAGE,
                )
                .limit(1)
            )
        ).scalar_one()

    captured = []

    async def fake_resume(anchor):
        captured.append(anchor)
        return True

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
    assert await runtime._dispatch_parent_event(event.id)
    assert captured[0].agent_id == storage_agent_id
    assert captured[0].sender_agent_id == execution_agent_id
    assert captured[0].message_meta["execution_agent_id"] == str(execution_agent_id)

    from app.api.chat_sessions import _build_session_detail_out, _load_accessible_session

    async with async_session() as db:
        user = await db.get(User, user_id)
        _route_agent, child_session, scope = await _load_accessible_session(
            db,
            user,
            storage_agent_id,
            run.id,
        )
        detail = await _build_session_detail_out(db, child_session, scope)
    assert detail.agent_id == str(execution_agent_id)
    assert detail.runtime is not None
    assert detail.runtime.execution_agent_id == str(execution_agent_id)


async def test_parent_wake_does_not_resume_with_revoked_execution_user(monkeypatch):
    from app.services.conversation_turn_lifecycle import (
        get_conversation_turn_snapshot,
    )

    first_agent_id, user_id, _parent_id, _anchor_id = await _make_context()
    second_agent_id = uuid.uuid4()
    storage_agent_id, execution_agent_id = sorted(
        [first_agent_id, second_agent_id],
        key=str,
    )
    async with async_session() as db:
        first_agent = await db.get(Agent, first_agent_id)
        db.add(
            Agent(
                id=second_agent_id,
                name="Revoked wake peer",
                creator_id=user_id,
                tenant_id=first_agent.tenant_id,
                primary_model_id=first_agent.primary_model_id,
                status="idle",
            )
        )
        parent = ChatSession(
            agent_id=storage_agent_id,
            peer_agent_id=execution_agent_id,
            user_id=None,
            title="Revoked A2A parent",
            source_channel="agent",
            is_primary=False,
            is_group=False,
        )
        db.add(parent)
        await db.flush()
        db.add(
            ChatMessage(
                agent_id=storage_agent_id,
                user_id=user_id,
                sender_agent_id=execution_agent_id,
                role="assistant",
                content="parent turn completed",
                conversation_id=str(parent.id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
        parent_id = parent.id
    run, _ = await runtime.create_subagent(
        agent_id=execution_agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-revoked-parent-wake",
        task="revoked wake",
        mode="async",
    )
    await runtime.send_subagent_message_to_parent(
        agent_id=execution_agent_id,
        execution_user_id=user_id,
        origin_tool_call_id="revoked-parent-message",
        subagent_session_id=str(run.id),
        message="must not resume",
    )
    async with async_session() as db:
        event = await db.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(run.id),
                ChatMessage.message_meta["kind"].as_string() == runtime.SUBAGENT_PARENT_MESSAGE,
            )
            .limit(1)
        )
        user = await db.get(User, user_id)
        user.is_active = False
        await db.commit()

    async def fail_resume(_anchor):
        raise AssertionError("revoked execution identity must not resume parent LLM")

    broadcasts: list[tuple[str, str, dict]] = []

    async def capture_broadcast(owner_agent_id, conversation_id, payload):
        broadcasts.append((str(owner_agent_id), str(conversation_id), payload))

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fail_resume)
    monkeypatch.setattr("app.api.websocket.manager.send_to_session", capture_broadcast)
    assert await runtime._dispatch_parent_event(event.id)

    async with async_session() as db:
        anchor = await db.scalar(
            select(ChatMessage).where(ChatMessage.external_event_key == f"subagent-parent:{event.id}")
        )
        final = await db.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == str(parent_id),
                ChatMessage.role == "assistant",
                ChatMessage.message_meta["turn_anchor_id"].as_string() == str(anchor.id),
            )
            .limit(1)
        )
        current_turn = await get_conversation_turn_snapshot(
            db,
            agent_id=storage_agent_id,
            conversation_id=str(parent_id),
        )
    assert anchor.message_meta["turn_status"] == "failed"
    assert final.content == "协作任务未能继续，请检查资源访问权限后重试。"
    assert final.agent_id == storage_agent_id
    assert final.sender_agent_id == execution_agent_id
    assert current_turn.anchor_id == anchor.id
    assert current_turn.status == "failed"
    assert current_turn.revision >= 1
    assert broadcasts
    assert {row[0] for row in broadcasts} == {str(storage_agent_id)}
    assert {row[1] for row in broadcasts} == {str(parent_id)}
