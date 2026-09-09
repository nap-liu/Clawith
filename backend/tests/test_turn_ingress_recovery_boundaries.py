"""Durable entry boundaries under interrupted continuation and owner loss."""

import uuid

import pytest

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services import turn_recovery as turn_recovery
from tests.test_turn_recovery import (
    _dispose_engine_between_tests as _dispose_engine_between_tests,
    _make_agent_with_model,
)

pytestmark = pytest.mark.asyncio


async def test_background_confirmation_keeps_one_business_delivery(monkeypatch):
    from app.services import background_turns, confirmation_service, turn_runtime
    from app.services.chat_history import ingest_incoming_chat_message
    from app.services.im_delivery import IMDeliveryResult, IMDeliveryPart
    from app.services.turn_recovery_startup import resume_startup_anchor

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(agent_id=agent_id, user_id=user_id,
                              title="Confirmation trigger", source_channel="dingtalk",
                              external_conv_id="dingtalk_group_confirmation")
        db.add(session)
        await db.flush()
        admitted = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id,
            content="continue confirmed action", source_channel="dingtalk",
        )
        anchor = admitted.message
        await background_turns.initialize_background_turn(
            db, session=session, anchor=anchor, kind="trigger", reference_id=uuid.uuid4(),
            completion={"origin": True, "triggers": [{"type": "on_message", "config": {}}]},
        )
        await db.commit()
        anchor_id, conversation_id = anchor.id, str(session.id)

    deliveries = []
    model_calls = []
    async def reply(*_args, **_kwargs):
        model_calls.append(True)
        return "confirmed reply"

    async def deliver(*, runtime, message, **_kwargs):
        deliveries.append((runtime.external_conv_id, message))
        return IMDeliveryResult.sent("dingtalk", IMDeliveryPart("dingtalk", str(len(deliveries))))

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", reply)
    monkeypatch.setattr(turn_runtime, "deliver_message_with_receipt", deliver)
    await confirmation_service._reenter_loop(
        agent_id, conversation_id, user_id, turn_anchor_id=anchor_id,
    )
    async with async_session() as db:
        current = await db.get(ChatMessage, anchor_id)
    assert await resume_startup_anchor(current)
    assert deliveries == [("dingtalk_group_confirmation", "confirmed reply")]
    assert model_calls == [True]
    async with async_session() as db:
        current = await db.get(ChatMessage, anchor_id)
        assert current.message_meta["background_execution"]["finalized"] is True
        assert current.message_meta["background_execution"]["delivered"] is True


async def test_pending_confirmation_waits_then_recovers_repeated_interruption(monkeypatch):
    from sqlalchemy import select
    from app.services import confirmation_service
    from app.services.chat_history import ingest_incoming_chat_message
    from app.services.turn_interruption import TurnInterrupted
    from app.services.turn_recovery_scanner import _load_recoverable_anchors
    from app.services.turn_recovery_startup import resume_startup_anchor

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(agent_id=agent_id, user_id=user_id, title="Confirmation restart",
                              source_channel="web")
        db.add(session)
        await db.flush()
        admitted = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id,
            content="request a confirmed action", source_channel="web",
        )
        await db.commit()
        anchor_id, session_id = admitted.message.id, session.id

    calls = []
    async def interrupted_then_complete(*_args, **kwargs):
        calls.append(kwargs["turn_anchor_id"])
        if len(calls) <= 2:
            raise TurnInterrupted()
        assert "confirm" in str(kwargs["history"])
        return "resumed confirmation result"

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", interrupted_then_complete)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", interrupted_then_complete)
    call_id = await confirmation_service.suspend_for_confirmation(
        agent_id=agent_id, conversation_id=str(session_id), chat_session_id=session_id,
        source_channel="web", user_id=user_id, intro_text=None, title="Confirm action",
        summary="Confirm", action=None, risk_level="medium", turn_anchor_id=anchor_id,
        buttons=[{"text": "Confirm", "value": "confirm"}],
    )
    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
        assert anchor.message_meta["turn_status"] == "suspended"
        candidates = await _load_recoverable_anchors(db, include_legacy=False)
        assert any(row.id == anchor_id for row in candidates)
    assert not await resume_startup_anchor(anchor)
    assert calls == []
    result = await confirmation_service.resolve_confirmation(
        agent_id=agent_id, call_id=call_id, button_value="confirm", button_label="Confirm",
        resolving_user_id=user_id,
    )
    assert result
    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
        assert anchor.message_meta["turn_status"] == "running"
    with pytest.raises(TurnInterrupted):
        await resume_startup_anchor(anchor)
    assert await resume_startup_anchor(anchor)
    assert calls == [anchor_id] * 3
    async with async_session() as db:
        current = await db.get(ChatMessage, anchor_id)
        assert current.message_meta["turn_status"] == "completed"
        replies = list((await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == str(session_id), ChatMessage.role == "assistant",
            ChatMessage.message_meta["turn_status"].as_string() == "completed",
        ))).all())
        assert len(replies) == 1
        assert replies[0].content == "resumed confirmation result"


async def test_reclaimed_subagent_rejects_old_owner_finalization():
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from app.models.subagent_run import SubagentRun
    from app.services import subagent_runtime as runtime
    from app.services.subagent_runtime_shared import _current_subagent_lease_owner
    from tests.test_subagent_runtime import _make_context

    agent_id, user_id, parent_id, parent_anchor = await _make_context()
    run, _ = await runtime.create_subagent(
        agent_id=agent_id, execution_user_id=user_id, parent_session_id=str(parent_id),
        origin_tool_call_id="reclaimed-owner", task="only current owner may finish", mode="async",
        turn_anchor_id=parent_anchor,
    )
    _, old_owner = await runtime._claim_subagent(run.id, with_token=True)
    token = _current_subagent_lease_owner.set(old_owner)
    try:
        anchor, _ = await runtime._load_or_start_input(run.id)
        async with async_session() as db:
            current = await db.get(SubagentRun, run.id)
            current.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        _, new_owner = await runtime._claim_subagent(run.id, with_token=True)
        assert new_owner != old_owner
        assert not await runtime._finish_subagent_turn(
            run_id=run.id, anchor_id=anchor.id, reply="stale owner reply", failed=False,
        )
        async with async_session() as db:
            current = await db.get(SubagentRun, run.id)
            assert current.lease_owner == new_owner
            replies = list((await db.scalars(select(ChatMessage).where(
                ChatMessage.conversation_id == str(run.id), ChatMessage.role == "assistant",
            ))).all())
            assert not replies
            current_anchor = await db.get(ChatMessage, anchor.id)
            assert current_anchor.message_meta["turn_status"] == "running"
    finally:
        _current_subagent_lease_owner.reset(token)


@pytest.mark.parametrize("background", [False, True])
async def test_peer_confirmation_keeps_storage_and_execution_identity(monkeypatch, background):
    import json
    from unittest.mock import AsyncMock
    from sqlalchemy import select
    from app.models.agent import Agent
    from app.models.gateway_message import GatewayMessage
    from app.models.trigger import AgentTrigger
    from app.models.trigger_execution import TriggerExecution
    from app.services import background_turns, confirmation_service
    from app.services.chat_history import ingest_incoming_chat_message
    from app.services.llm.caller_streaming_support import CallLlmState
    from app.services.llm.caller_streaming_rounds import _call_llm_execute_tool_round
    from app.services.llm.client import LLMResponse

    storage_id, user_id = await _make_agent_with_model()
    outbox_id = uuid.uuid4()
    async with async_session() as db:
        storage = await db.get(Agent, storage_id)
        peer = Agent(name="Peer executor", creator_id=user_id, tenant_id=storage.tenant_id,
                     primary_model_id=storage.primary_model_id)
        db.add(peer)
        await db.flush()
        session = ChatSession(agent_id=storage_id, peer_agent_id=peer.id,
                              title="Peer confirmation", source_channel="agent")
        db.add(session)
        await db.flush()
        admitted = await ingest_incoming_chat_message(
            db, session=session, agent_id=storage_id, user_id=user_id,
            content="request peer confirmation", source_channel="agent",
            message_meta={"execution_agent_id": str(peer.id)},
        )
        anchor = admitted.message
        anchor.sender_user_id = None
        anchor.sender_agent_id = storage_id if not background else None
        if background:
            trigger = AgentTrigger(agent_id=peer.id, name="Peer subscription", type="on_message",
                                   execution_user_id=user_id)
            db.add(trigger)
            await db.flush()
            execution = TriggerExecution(
                agent_id=peer.id, trigger_id=trigger.id, execution_user_id=user_id,
                conversation_id=session.id, source="on_message", status="running",
                idempotency_key=uuid.uuid4().hex, payload={"_origin_session_id": str(session.id)},
            )
            db.add(execution)
            await db.flush()
            anchor.message_meta = {**anchor.message_meta, "kind": "on_message_event",
                                   "trigger_id": str(trigger.id), "trigger_execution_id": str(execution.id)}
            await background_turns.initialize_background_turn(
                db, session=session, anchor=anchor, kind="trigger", reference_id=execution.id,
                settings={"execution_agent_id": str(peer.id)},
                completion={"origin": True, "execution_ids": [str(execution.id)]},
            )
        else:
            anchor.message_meta = {**anchor.message_meta, "gateway_direct_reply": {
                "message_id": str(outbox_id), "agent_id": str(storage_id), "sender_agent_id": str(peer.id),
            }}
        await db.commit()
        peer_id, session_id, anchor_id = peer.id, session.id, anchor.id

    state = CallLlmState(model=None, agent_name="Peer executor", role_description="",
                         agent_id=peer_id, user_id=user_id, session_id=str(session_id),
                         turn_anchor_id=anchor_id, turn_anchor_agent_id=storage_id,
                         client_guard=AsyncMock())
    response = LLMResponse(content="", tool_calls=[{
        "id": "confirm-peer", "type": "function", "function": {
            "name": "request_confirmation", "arguments": json.dumps({
                "title": "Confirm", "summary": "Continue peer action", "risk_level": "medium",
                "buttons": [{"text": "Confirm", "value": "confirm"}],
            }),
        },
    }], finish_reason="tool_calls")
    assert await _call_llm_execute_tool_round(state, response, [], 0) == ""
    async with async_session() as db:
        card = await db.scalar(select(ChatMessage).where(
            ChatMessage.conversation_id == str(session_id), ChatMessage.role == "tool_call",
        ))
        assert card.agent_id == storage_id
        assert card.message_meta["turn_anchor_id"] == str(anchor_id)
        card_id = card.id

    calls = []
    async def reply(_db, agent_id, _text, **kwargs):
        calls.append((agent_id, kwargs["user_id"], kwargs["storage_agent_id"]))
        return "peer confirmed reply"

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", reply)
    assert await confirmation_service.resolve_confirmation(
        agent_id=storage_id, call_id=card_id, button_value="confirm", button_label="Confirm",
        resolving_user_id=user_id,
    )
    assert await confirmation_service.resolve_confirmation(
        agent_id=storage_id, call_id=card_id, button_value="confirm", button_label="Confirm",
        resolving_user_id=user_id,
    ) is None
    assert calls == [(peer_id, user_id, storage_id)]
    async with async_session() as db:
        finals = list((await db.scalars(select(ChatMessage).where(
            ChatMessage.conversation_id == str(session_id), ChatMessage.role == "assistant",
            ChatMessage.message_meta["turn_status"].as_string() == "completed",
        ))).all())
        assert len(finals) == 1
        final = finals[0]
        assert final.agent_id == storage_id
        assert final.sender_agent_id == peer_id
        current = await db.get(ChatMessage, anchor_id)
        assert current.sender_agent_id == (None if background else storage_id)
        if background:
            assert current.message_meta["background_execution"]["delivered"] is True
        else:
            outbox = await db.get(GatewayMessage, outbox_id)
            assert outbox is not None and outbox.content == "peer confirmed reply"


async def test_project_background_confirmation_preserves_workspace_and_pause(monkeypatch):
    from app.models.agent import Agent
    from app.models.project import Project
    from app.services import background_turns, confirmation_service
    from app.services.chat_history import ingest_incoming_chat_message
    from app.services.agent_runtime_workspace import current_agent_runtime_workspace

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        project = Project(tenant_id=agent.tenant_id, owner_user_id=user_id, execution_user_id=user_id,
                          name="Confirmed project background", status="paused")
        db.add(project)
        await db.flush()
        agent.scope, agent.project_id = "project", project.id
        agent.agent_dir = f".agents/{agent_id}"
        session = ChatSession(agent_id=agent_id, user_id=user_id, project_id=project.id,
                              title="Project heartbeat confirmation", source_channel="trigger")
        db.add(session)
        await db.flush()
        admitted = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id,
            content="confirmed project heartbeat", source_channel="trigger",
        )
        anchor = admitted.message
        await background_turns.initialize_background_turn(
            db, session=session, anchor=anchor, kind="oneshot", reference_id=uuid.uuid4(),
        )
        await db.commit()
        project_id, anchor_id, conversation_id = project.id, anchor.id, str(session.id)

    provider_calls = []
    async def provider(**kwargs):
        workspace = current_agent_runtime_workspace(kwargs["agent_id"])
        assert workspace.agent_id == agent_id and workspace.project_id == project_id
        assert workspace.is_project
        provider_calls.append(kwargs["turn_anchor_id"])
        return "project confirmed result"

    monkeypatch.setattr("app.services.llm.call_llm_with_failover", provider)
    await confirmation_service._reenter_loop(
        agent_id, conversation_id, user_id, turn_anchor_id=anchor_id,
    )
    assert provider_calls == []
    async with async_session() as db:
        current = await db.get(ChatMessage, anchor_id)
        assert current.message_meta["turn_status"] == "running"
        project = await db.get(Project, project_id)
        project.status = "running"
        await db.commit()
    await confirmation_service._reenter_loop(
        agent_id, conversation_id, user_id, turn_anchor_id=anchor_id,
    )
    assert provider_calls == [anchor_id]
    async with async_session() as db:
        current = await db.get(ChatMessage, anchor_id)
        assert current.message_meta["turn_status"] == "completed"
        assert current.message_meta["background_execution"]["finalized"] is True
        assert current.message_meta["background_execution"]["delivered"] is True


@pytest.mark.parametrize("status", ["completed", "cancelled"])
async def test_rollback_helper_finishes_background_terminal_tail(tmp_path, monkeypatch, status):
    from app.models.agent import Agent
    from app.scripts.resume_turns_after_rollback import snapshot, apply
    from app.services.background_task_admission import create_background_turn
    from app.services.chat_history import persist_assistant_reply_row
    from app.services.conversation_turn_lifecycle import transition_conversation_turn

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        anchor = await create_background_turn(
            db, agent=agent, kind="oneshot", reference_id=uuid.uuid4(),
            user_prompt="finish only the committed business result", execution_user_id=user_id,
        )
        await db.commit()
        if status == "completed":
            await persist_assistant_reply_row(
                db, agent_id=agent_id, user_id=user_id, conversation_id=anchor.conversation_id,
                content="committed background result", turn_anchor_id=anchor.id,
            )
        else:
            await transition_conversation_turn(
                db, agent_id=agent_id, conversation_id=anchor.conversation_id,
                turn_anchor_id=anchor.id, status="cancelled",
            )
        await db.commit()
        anchor_id = anchor.id

    async def no_model(*_args, **_kwargs):
        raise AssertionError("rollback terminal reconciliation must never call the model")

    monkeypatch.setattr("app.services.turn_recovery._call_agent_llm", no_model)
    path = tmp_path / "rollback.json"
    assert (await snapshot(path))["anchors"] == 1
    report = await apply(path)
    assert report["resumed"] == 1 and report["pending"] == report["failed"] == 0
    async with async_session() as db:
        current = await db.get(ChatMessage, anchor_id)
        assert current.message_meta["background_execution"]["finalized"] is True
        assert current.message_meta["background_execution"]["delivered"] is True
    assert (await apply(path))["already_finished"] == 1


async def test_rollback_helper_follows_fixed_root_reply_after_next_turn(tmp_path, monkeypatch):
    from app.api.websocket import manager
    from app.scripts.resume_turns_after_rollback import snapshot, apply
    from app.services.chat_history import ingest_incoming_chat_message, persist_assistant_reply_row

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(agent_id=agent_id, user_id=user_id, source_channel="web", title="Rollback tail")
        db.add(session)
        await db.flush()
        admitted = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id, content="original", source_channel="web",
        )
        await db.commit()
        root_id, session_id = admitted.message.id, session.id
    path = tmp_path / "rollback.json"
    assert (await snapshot(path))["anchors"] == 1
    async with async_session() as db:
        reply_id = await persist_assistant_reply_row(
            db, agent_id=agent_id, user_id=user_id, conversation_id=str(session_id),
            content="original final", turn_anchor_id=root_id,
        )
        await db.commit()
        session = await db.get(ChatSession, session_id)
        following = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id, content="following", source_channel="web",
        )
        await db.commit()
        following_id = following.message.id
    sends = []
    async def publish(_agent, _session, payload):
        sends.append(payload["message_id"])

    async def no_model(*_args, **_kwargs):
        raise AssertionError("rollback must never adopt the new turn")

    monkeypatch.setattr(manager, "send_to_session", publish)
    monkeypatch.setattr("app.services.turn_recovery._call_agent_llm", no_model)
    report = await apply(path)
    assert report["resumed"] == 1 and report["superseded"] == report["pending"] == 0
    assert sends == [str(reply_id)]
    async with async_session() as db:
        following = await db.get(ChatMessage, following_id)
        assert following.message_meta["turn_status"] == "running"
    assert (await apply(path))["already_finished"] == 1
    assert sends == [str(reply_id)]


async def test_rollback_helper_waiting_confirmation_is_not_success(tmp_path):
    import asyncio
    import json
    import os
    import sys
    from app.scripts.resume_turns_after_rollback import snapshot, apply
    from app.services import confirmation_service
    from app.services.chat_history import ingest_incoming_chat_message

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(agent_id=agent_id, user_id=user_id, source_channel="web", title="Rollback confirmation")
        db.add(session)
        await db.flush()
        admitted = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id, content="confirm", source_channel="web",
        )
        await db.commit()
    await confirmation_service.suspend_for_confirmation(
        agent_id=agent_id, conversation_id=str(session.id), chat_session_id=session.id,
        source_channel="web", user_id=user_id, intro_text=None, title="Confirm", summary="Confirm",
        action=None, risk_level="medium", turn_anchor_id=admitted.message.id,
    )
    path = tmp_path / "rollback.json"
    assert (await snapshot(path))["anchors"] == 1
    report = await apply(path)
    assert report["waiting_confirmation"] == 1 and report["already_finished"] == 0
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.scripts.resume_turns_after_rollback", "apply", str(path),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 1, stderr.decode()
    assert json.loads(stdout.decode().splitlines()[-1])["waiting_confirmation"] == 1


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("fixed_snapshot", [False, True])
async def test_rollback_running_root_does_not_start_later_inbox(
    tmp_path, monkeypatch, background, fixed_snapshot,
):
    from app.models.agent import Agent
    from app.scripts.resume_turns_after_rollback import snapshot, apply
    from app.services.background_turns import initialize_background_turn
    from app.services.chat_history import ingest_incoming_chat_message
    from app.services.turn_recovery_startup import resume_startup_anchor

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        owner = await db.get(Agent, agent_id)
        peer = Agent(name="Deferred executor", creator_id=user_id, tenant_id=owner.tenant_id,
                     primary_model_id=owner.primary_model_id, status="idle")
        db.add(peer)
        session = ChatSession(agent_id=agent_id, user_id=user_id, source_channel="web", title="Frozen roots")
        db.add(session)
        await db.flush()
        admitted = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id, content="original", source_channel="web",
        )
        root = admitted.message
        if background:
            await initialize_background_turn(
                db, session=session, anchor=root, kind="trigger", reference_id=uuid.uuid4(),
                completion={"origin": True, "triggers": [{"type": "on_message", "config": {}}]},
            )
        await db.commit()
        session_id, peer_id = session.id, peer.id
    path = tmp_path / "rollback.json"
    assert (await snapshot(path))["anchors"] == 1
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        queued = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id, content="later peer input",
            source_channel="web", message_meta={"execution_agent_id": str(peer_id)},
        )
        assert queued.queued_to_running_turn
        assert queued.message.message_meta["turn_inbox_mode"] == "next_turn"
        await db.commit()
        next_id = queued.message.id
    model_calls, scheduled = [], []
    async def provider(*_args, **kwargs):
        model_calls.append(kwargs["turn_anchor_id"])
        return "original result"

    async def schedule(anchor):
        scheduled.append(anchor.id)

    monkeypatch.setattr("app.services.turn_recovery._call_agent_llm", provider)
    monkeypatch.setattr("app.services.turn_inbox.schedule_durable_turn_resume", schedule)
    if fixed_snapshot:
        report = await apply(path)
        assert report["resumed"] == 1 and report["pending"] == report["failed"] == 0
        assert scheduled == []
    else:
        assert await resume_startup_anchor(root)
        assert scheduled and set(scheduled) == {next_id}
    assert model_calls == [root.id]
    async with async_session() as db:
        next_row = await db.get(ChatMessage, next_id)
        assert next_row.message_meta["turn_inbox_state"] == "promoted"
        assert next_row.message_meta["turn_status"] == "running"
        if background:
            current = await db.get(ChatMessage, root.id)
            assert current.message_meta["background_execution"]["delivered"] is True


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_rollback_terminal_without_reply_is_not_false_success(tmp_path, status):
    from app.scripts.resume_turns_after_rollback import snapshot, apply
    from app.services.chat_history import ingest_incoming_chat_message
    from app.services.conversation_turn_lifecycle import transition_conversation_turn

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(agent_id=agent_id, user_id=user_id, source_channel="web", title="Incomplete terminal")
        db.add(session)
        await db.flush()
        admitted = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id, content="original", source_channel="web",
        )
        await db.commit()
    path = tmp_path / "rollback.json"
    await snapshot(path)
    async with async_session() as db:
        await transition_conversation_turn(
            db, agent_id=agent_id, conversation_id=str(session.id), turn_anchor_id=admitted.message.id,
            status=status,
        )
        await db.commit()
    report = await apply(path)
    assert report["already_finished"] == int(status == "cancelled")
    assert report["pending"] == int(status != "cancelled")


async def test_rollback_model_failure_is_reported_without_retry(tmp_path, monkeypatch):
    from app.scripts.resume_turns_after_rollback import snapshot, apply
    from app.services.chat_history import ingest_incoming_chat_message
    from app.services.llm.failure_outcome import LLMFailure

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(agent_id=agent_id, user_id=user_id, source_channel="web", title="Failure truth")
        db.add(session)
        await db.flush()
        admitted = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id, content="original", source_channel="web",
        )
        await db.commit()
    calls = []
    async def failure(*_args, **kwargs):
        calls.append(kwargs["turn_anchor_id"])
        return LLMFailure("provider rejected", code="test_failure", message_key="test.failure",
                          retryable=False, allow_failover=False)

    monkeypatch.setattr("app.services.turn_recovery._call_agent_llm", failure)
    path = tmp_path / "rollback.json"
    await snapshot(path)
    report = await apply(path)
    assert report["business_failed"] == 1 and report["failed"] == report["pending"] == 0
    again = await apply(path)
    assert again["business_failed"] == again["already_finished"] == 1
    assert calls == [admitted.message.id]
