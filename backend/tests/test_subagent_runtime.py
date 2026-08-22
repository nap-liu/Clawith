"""Behavior tests for the normalized durable Subagent runtime."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from app.api.tools import AgentToolUpdate, update_agent_tools
from app.api.websocket import _has_active_subagent_event_turn
from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction  # noqa: F401
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.mcp_server import MCPServer  # noqa: F401 - register Tool FK target
from app.models.participant import Participant  # noqa: F401
from app.models.project import Project, ProjectMemberSnapshot  # noqa: F401 - project FK targets
from app.models.subagent_run import SubagentRun
from app.models.tenant import Tenant
from app.models.tool import AgentTool, Tool
from app.models.user import Identity, User
from app.services import subagent_runtime as runtime
from app.services.tool_enablement import SUBAGENT_TOOL_NAMES
from app.services.tool_seeder import seed_builtin_tools

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    yield
    await engine.dispose()


async def _make_context(*, parent_channel: str = "web"):
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        tenant = Tenant(name=f"subagent-{suffix}", slug=f"subagent-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"subagent-{suffix}",
            email=f"subagent-{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            display_name="Subagent Tester",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()
        model = LLMModel(
            tenant_id=tenant.id,
            provider="openai",
            model="Readable-Test-Model",
            api_key_encrypted="unused",
            label="Readable model",
            enabled=True,
            context_window=128000,
        )
        db.add(model)
        await db.flush()
        agent = Agent(
            name=f"Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            primary_model_id=model.id,
            context_window_size=128000,
            status="idle",
        )
        db.add(agent)
        await db.flush()
        parent = ChatSession(
            agent_id=agent.id,
            user_id=user.id if parent_channel == "web" else None,
            title="Parent",
            source_channel=parent_channel,
            is_primary=False,
            is_group=False,
        )
        db.add(parent)
        await db.flush()
        message_time = datetime.now(UTC)
        prior_user = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_user_id=user.id,
            role="user",
            content="earlier user context",
            conversation_id=str(parent.id),
            message_meta={"attachments": []},
            created_at=message_time,
        )
        db.add(prior_user)
        await db.flush()
        prior = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_agent_id=agent.id,
            role="assistant",
            content="earlier context",
            conversation_id=str(parent.id),
            message_meta={"attachments": []},
            created_at=message_time + timedelta(microseconds=1),
        )
        db.add(prior)
        await db.flush()
        anchor = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            sender_user_id=user.id,
            role="user",
            content="parent request",
            conversation_id=str(parent.id),
            message_meta={"attachments": []},
            created_at=message_time + timedelta(microseconds=2),
        )
        db.add(anchor)
        await db.commit()
        return agent.id, user.id, parent.id, anchor.id


async def test_child_parent_message_tool_respects_standard_agent_tool_toggle(monkeypatch):
    agent_id, _, _, _ = await _make_context()
    # The suite must be self-contained on a fresh disposable database; do not
    # depend on another test process having seeded the built-in tools first.
    await seed_builtin_tools()

    async def normal_tools(_agent_id):
        assert _agent_id == agent_id
        return [
            {
                "type": "function",
                "function": {
                    "name": "ordinary_tool",
                    "description": "ordinary",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    monkeypatch.setattr(
        "app.services.agent_tools.get_agent_tools_for_llm",
        normal_tools,
    )

    async with async_session() as db:
        tool = (await db.execute(select(Tool).where(Tool.name == "send_message_to_parent"))).scalar_one()
        tool_id = tool.id
        assignment = AgentTool(agent_id=agent_id, tool_id=tool_id, enabled=False)
        db.add(assignment)
        await db.commit()

    # Startup seeding must preserve an explicit manual opt-out.
    await seed_builtin_tools()
    disabled_names = {item["function"]["name"] for item in await runtime.prepare_subagent_tools(agent_id)}
    assert disabled_names == {"ordinary_tool"}

    async with async_session() as db:
        assignment = (
            await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.tool_id == tool_id,
                )
            )
        ).scalar_one()
        assignment.enabled = True
        await db.commit()

    enabled_names = {item["function"]["name"] for item in await runtime.prepare_subagent_tools(agent_id)}
    assert enabled_names == {"ordinary_tool", "send_message_to_parent"}


async def test_subagent_panel_group_toggle_updates_all_four_tools():
    agent_id, user_id, _, _ = await _make_context()
    await seed_builtin_tools()

    async with async_session() as db:
        user = await db.get(User, user_id)
        tools = (await db.execute(select(Tool).where(Tool.name.in_(SUBAGENT_TOOL_NAMES)))).scalars().all()
        assert {tool.name for tool in tools} == set(SUBAGENT_TOOL_NAMES)
        assert {tool.category for tool in tools} == {"subagent"}
        run_tool = next(tool for tool in tools if tool.name == "run_subagent")
        run_schema = run_tool.parameters_schema
        assert run_schema["required"] == ["name", "task"]
        assert run_schema["properties"]["soul"]["default"] is True
        assert run_schema["properties"]["memory"]["default"] is True
        assert "load_soul" not in run_schema["properties"]
        assert "load_memory" not in run_schema["properties"]

        await update_agent_tools(
            agent_id,
            [AgentToolUpdate(tool_id=str(run_tool.id), enabled=False)],
            current_user=user,
            db=db,
        )
        disabled = (
            (
                await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.tool_id.in_([tool.id for tool in tools]),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(disabled) == 4
        assert all(assignment.enabled is False for assignment in disabled)

        await update_agent_tools(
            agent_id,
            [AgentToolUpdate(tool_id=str(run_tool.id), enabled=True)],
            current_user=user,
            db=db,
        )
        enabled = (
            (
                await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.tool_id.in_([tool.id for tool in tools]),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(enabled) == 4
        assert all(assignment.enabled is True for assignment in enabled)


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
    assert fresh.status == "cancelled"
    assert stale_final is None


async def test_control_plane_cancel_is_terminal_not_requeued(monkeypatch):
    from app.services import channel_llm
    from app.services.active_turns import (
        cancel_active_turn,
        ensure_active_turn,
        list_active_turns,
        reset_active_turns_for_testing,
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
    await ready.wait()
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
    assert fresh.status == runtime.RUN_CANCELLED
    assert all(row.message_meta["subagent_input_state"] == runtime.INPUT_CANCELLED for row in input_rows)
    await reset_active_turns_for_testing()


async def test_failed_turn_continues_when_parent_input_is_pending():
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
    assert fresh.status == "running"
    assert first.message_meta["subagent_input_state"] == "done"
    assert first.message_meta["turn_status"] == "failed"


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

    async def must_not_reexecute(*_args, **_kwargs):
        raise AssertionError("a durable done tool must never be replayed")

    monkeypatch.setattr(runtime, "prepare_subagent_tools", fake_tools)
    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_llm)
    monkeypatch.setattr("app.services.turn_recovery.execute_tool", must_not_reexecute)

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

    async def fake_resume(anchor):
        captured.append(anchor)
        return True

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fake_resume)
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
                action={"tool": "project_write_file", "args": {"path": "ok.txt"}},
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
    assert completed.status == runtime.RUN_COMPLETED
    assert json.loads(confirmation.content)["status"] == "done"
    assert final.content == "confirmed child result"
    assert final.thinking == "resumed hidden reasoning"


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
    assert "ExecutionIdentityError" in failure.content


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
    agent_id, user_id, parent_id, _anchor_id = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id,
        execution_user_id=user_id,
        parent_session_id=str(parent_id),
        origin_tool_call_id="call-revoked-parent-wake",
        task="revoked wake",
        mode="async",
    )
    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                sender_agent_id=agent_id,
                role="assistant",
                content="parent turn completed",
                conversation_id=str(parent_id),
                message_meta={"attachments": []},
            )
        )
        await db.commit()
    await runtime.send_subagent_message_to_parent(
        agent_id=agent_id,
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

    monkeypatch.setattr("app.services.turn_recovery.resume_turn", fail_resume)
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
    assert anchor.message_meta["turn_status"] == "failed"
    assert "执行身份已失效" in final.content
