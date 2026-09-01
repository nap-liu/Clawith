"""Mechanical continuation of startup turn-recovery tests."""

import pytest

from tests.test_turn_recovery import (
    UTC,
    Agent,
    ChannelConfig,
    ChatMessage,
    ChatSession,
    IdentityProvider,
    OrgMember,
    SimpleNamespace,
    User,
    _dispose_engine_between_tests,
    _make_agent_with_model,
    _make_user_anchor,
    asyncio,
    async_session,
    datetime,
    delete,
    engine,
    json,
    select,
    text,
    timedelta,
    timezone,
    uuid,
)

pytestmark = pytest.mark.asyncio
async def test_resume_turn_does_not_continue_when_recovered_tool_result_persist_fails(monkeypatch):
    """A fail-closed recovery result must be durable before LLM continuation."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    conv = f"running_tool_persist_fail_{uuid.uuid4().hex}"
    async with async_session() as db:
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="read the file",
        )
        anchor_id = anchor.id
        anchor.created_at = datetime.now(timezone.utc) - timedelta(seconds=2)
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "read_file",
                        "call_id": "call_result_persist_fail",
                        "args": {"path": "a.txt"},
                        "status": "running",
                        "result": "",
                        "turn_anchor_id": str(anchor_id),
                    },
                    ensure_ascii=False,
                ),
                conversation_id=conv,
                created_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        await db.commit()

    async def fake_execute_tool(*_args, **_kwargs):
        return "file body"

    async def fail_done_persist(*_args, **_kwargs):
        raise RuntimeError("done persist failed")

    async def fail_if_llm_called(*_args, **_kwargs):
        raise AssertionError("LLM must not continue without durable recovered tool result")

    monkeypatch.setattr(turn_recovery, "execute_tool", fake_execute_tool, raising=False)
    monkeypatch.setattr(turn_recovery, "persist_tool_call_row", fail_done_persist)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fail_if_llm_called)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    with pytest.raises(RuntimeError, match="done persist failed"):
        await turn_recovery.resume_turn(anchor)

    async with async_session() as db:
        tool_rows = (
            (
                await db.execute(
                    select(ChatMessage).where(ChatMessage.conversation_id == conv, ChatMessage.role == "tool_call")
                )
            )
            .scalars()
            .all()
        )

    assert [json.loads(row.content)["status"] for row in tool_rows] == ["running"]


async def test_resume_turn_does_not_execute_running_tool_call_from_later_turn(monkeypatch):
    """Recovery for one anchor must stop at the next user turn boundary."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    conv = f"later_turn_{uuid.uuid4().hex}"
    async with async_session() as db:
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="old interrupted",
        )
        anchor_id = anchor.id
        anchor.created_at = datetime.now(timezone.utc) - timedelta(seconds=3)
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="user",
                content="newer turn",
                conversation_id=conv,
                created_at=datetime.now(timezone.utc) - timedelta(seconds=2),
            )
        )
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "read_file",
                        "call_id": "later_call",
                        "args": {"path": "later.txt"},
                        "status": "running",
                        "result": "",
                        "turn_anchor_id": str(uuid.uuid4()),
                    },
                    ensure_ascii=False,
                ),
                conversation_id=conv,
                created_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        await db.commit()

    async def fail_execute_tool(*_args, **_kwargs):
        raise AssertionError("later turn tool must not execute while recovering older anchor")

    captured = {}

    async def fake_call_agent_llm(*_args, **kwargs):
        captured.update(kwargs)
        return "old reply"

    async def fake_deliver(*_args, **_kwargs):
        return True

    monkeypatch.setattr(turn_recovery, "execute_tool", fail_execute_tool, raising=False)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert captured["history"] == [
        {
            "role": "user",
            "content": "old interrupted",
            "attachments": [],
        }
    ]


async def test_resume_turn_skips_existing_assistant_without_redelivery(monkeypatch):
    """Direct recovery calls must treat persisted assistant output as completed."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_assistant_reply_row, persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk",
            source_channel="dingtalk",
            external_conv_id="dingtalk_p2p_staff-3",
        )
        db.add(session)
        await db.flush()
        conv = str(session.id)
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="deliver retry",
        )
        anchor_id = anchor.id
        anchor.created_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await persist_assistant_reply_row(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="reply persisted before crash",
        )
        await db.commit()

    async def fail_if_llm_called(*_args, **_kwargs):
        raise AssertionError("delivery retry must not rerun the LLM")

    deliveries = []

    async def fake_deliver(*, agent_id, conversation_id, reply):
        deliveries.append((agent_id, conversation_id, reply))
        return True

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fail_if_llm_called)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    first = await turn_recovery.resume_turn(anchor)

    assert first is False

    second = await turn_recovery.resume_turn(anchor)

    assert second is False
    assert deliveries == []
    async with async_session() as db:
        assistant_rows = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == conv,
                        ChatMessage.role == "assistant",
                        ChatMessage.content == "reply persisted before crash",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(assistant_rows) == 1


async def test_dingtalk_natural_entry_creates_and_completes_recoverable_turn(monkeypatch):
    """The real DingTalk entry path must create the same recoverable turn anchor."""
    from app.api.dingtalk import process_dingtalk_message
    from app.models.chat_session import ChatSession
    from app.services.im_delivery import IMDeliveryPart, IMDeliveryResult

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    sender_staff_id = f"staff_{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        provider = IdentityProvider(
            tenant_id=agent.tenant_id,
            name="DingTalk",
            provider_type="dingtalk",
            is_active=True,
        )
        db.add(provider)
        await db.flush()
        db.add(
            OrgMember(
                provider_id=provider.id,
                external_id=sender_staff_id,
                name="DingTalk Tester",
                status="active",
                tenant_id=agent.tenant_id,
                user_id=user_id,
            )
        )
        await db.commit()

    async def fake_call_agent_llm(*_args, **kwargs):
        assert kwargs["turn_anchor_id"] is not None
        return "natural dingtalk reply"

    deliveries = []

    async def fake_deliver_message_with_receipt(*, agent_id, runtime, message, **_kwargs):
        deliveries.append((agent_id, runtime, message))
        return IMDeliveryResult.sent(
            "dingtalk",
            IMDeliveryPart(
                transport="dingtalk_openapi_oto",
                provider_message_id="process-query-key",
                conversation_ref=runtime.external_conv_id,
            ),
        )

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(
        "app.services.turn_runtime.deliver_message_with_receipt",
        fake_deliver_message_with_receipt,
    )

    await process_dingtalk_message(
        agent_id=agent_id,
        sender_staff_id=sender_staff_id,
        user_text="natural dingtalk user message",
        conversation_id="open-conv-1",
        conversation_type="1",
        sender_nick="DingTalk Tester",
        message_id=f"msg-{uuid.uuid4().hex}",
    )

    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.external_conv_id == f"dingtalk_p2p_{sender_staff_id}",
                )
            )
        ).scalar_one()
        user_msg = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(session.id),
                    ChatMessage.role == "user",
                    ChatMessage.content == "natural dingtalk user message",
                )
            )
        ).scalar_one()
        assistant_msg = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(session.id),
                    ChatMessage.role == "assistant",
                    ChatMessage.content == "natural dingtalk reply",
                )
            )
        ).scalar_one()

    assert user_msg.content == "natural dingtalk user message"
    assert assistant_msg.content == "natural dingtalk reply"
    assert len(deliveries) == 1
    delivered_agent_id, runtime, delivered_message = deliveries[0]
    assert delivered_agent_id == agent_id
    assert runtime.external_conv_id == f"dingtalk_p2p_{sender_staff_id}"
    assert runtime.is_group is False
    assert delivered_message == "natural dingtalk reply"


async def test_dingtalk_cancelled_turn_preserves_recovery_anchor(monkeypatch):
    """Backend shutdown cancellation must leave the persisted user row recoverable."""
    from app.api.dingtalk import process_dingtalk_message
    from app.models.chat_session import ChatSession

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    sender_staff_id = f"staff_{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        provider = IdentityProvider(
            tenant_id=agent.tenant_id,
            name="DingTalk",
            provider_type="dingtalk",
            is_active=True,
        )
        db.add(provider)
        await db.flush()
        db.add(
            OrgMember(
                provider_id=provider.id,
                external_id=sender_staff_id,
                name="DingTalk Tester",
                status="active",
                tenant_id=agent.tenant_id,
                user_id=user_id,
            )
        )
        await db.commit()

    async def cancelled_call_agent_llm(*_args, **kwargs):
        assert kwargs["turn_anchor_id"] is not None
        raise asyncio.CancelledError()

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", cancelled_call_agent_llm)

    with pytest.raises(asyncio.CancelledError):
        await process_dingtalk_message(
            agent_id=agent_id,
            sender_staff_id=sender_staff_id,
            user_text="recover me after shutdown",
            conversation_id="open-conv-cancel",
            conversation_type="1",
            sender_nick="DingTalk Tester",
            message_id=f"msg-{uuid.uuid4().hex}",
        )

    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.external_conv_id == f"dingtalk_p2p_{sender_staff_id}",
                )
            )
        ).scalar_one()
        rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(session.id))
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            )
            .scalars()
            .all()
        )

    assert [(row.role, row.content) for row in rows] == [("user", "recover me after shutdown")]


async def test_resume_turn_keeps_processing_when_final_persist_fails(monkeypatch):
    """A failed durable final write must not complete the anchor."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=1)
    conv = f"resume_{uuid.uuid4().hex}"
    async with async_session() as db:
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="interrupted",
        )
        anchor_id = anchor.id
        await db.commit()

    async def fake_call_agent_llm(*_args, **_kwargs):
        return "resumed reply"

    async def fail_finalizer(*_args, **_kwargs):
        raise RuntimeError("persist failed")

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "persist_assistant_reply_row", fail_finalizer)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    with pytest.raises(RuntimeError, match="persist failed"):
        await turn_recovery.resume_turn(anchor)

    async with async_session() as db:
        assistant_rows = (
            (
                await db.execute(
                    select(ChatMessage).where(ChatMessage.conversation_id == conv, ChatMessage.role == "assistant")
                )
            )
            .scalars()
            .all()
        )
    assert assistant_rows == []


async def test_resume_turn_suspends_processing_anchor_with_pending_confirmation():
    """Old partial states with a pending confirmation should become suspended, not resumed."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message, persist_pending_confirmation

    agent_id, user_id = await _make_agent_with_model(context_window_size=1)
    conv = f"pending_{uuid.uuid4().hex}"
    async with async_session() as db:
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="interrupted",
        )
        anchor_id = anchor.id
        await db.commit()

    await persist_pending_confirmation(
        async_session,
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=conv,
        name="request_confirmation",
        args={"title": "确认", "summary": "等待用户点击"},
        turn_anchor_id=anchor_id,
    )

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is False
    async with async_session() as db:
        pending_rows = (
            (
                await db.execute(
                    select(ChatMessage).where(ChatMessage.conversation_id == conv, ChatMessage.role == "tool_call")
                )
            )
            .scalars()
            .all()
        )
    assert len(pending_rows) == 1
    assert json.loads(pending_rows[0].content)["status"] == "pending"
