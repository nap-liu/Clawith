"""Concurrent admission must use the committed turn, even with a cached session."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.database import async_session, engine
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.services.chat_history import ingest_incoming_chat_message
from app.services.conversation_turn_lifecycle import transition_conversation_turn
from app.services.turn_inbox import drain_turn_inbox
from test_turn_inbox_receipt_anchor import _seed_running_im_turn


@pytest.fixture(autouse=True)
async def _dispose_connections():
    await engine.dispose()
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_cached_session_cannot_enqueue_into_an_old_turn_generation():
    agent_id, user_id, session_id, old_root, _ = await _seed_running_im_turn()
    async with async_session() as incoming_db:
        cached_session = await incoming_db.get(ChatSession, session_id)
        # The adapter has loaded the conversation, but another transaction
        # completes its old turn and admits a new one before message ingestion.
        async with async_session() as owner_db:
            await transition_conversation_turn(
                owner_db, agent_id=agent_id, conversation_id=str(session_id),
                turn_anchor_id=old_root, status="completed",
            )
            new_root = ChatMessage(
                agent_id=agent_id, user_id=user_id, role="user",
                content="new running task", conversation_id=str(session_id),
            )
            owner_db.add(new_root)
            await owner_db.flush()
            current = await transition_conversation_turn(
                owner_db, agent_id=agent_id, conversation_id=str(session_id),
                turn_anchor_id=new_root.id, status="running",
            )
            await owner_db.commit()

        incoming = await ingest_incoming_chat_message(
            incoming_db, session=cached_session, agent_id=agent_id,
            user_id=user_id, source_channel="dingtalk", content="also check this",
            provider_event_id=str(uuid.uuid4()),
        )
        await incoming_db.commit()

    assert incoming.queued_to_running_turn is True
    assert incoming.message.message_meta["turn_inbox_anchor_id"] == str(new_root.id)
    assert incoming.message.message_meta["turn_inbox_generation"] == current.generation
    injected = await drain_turn_inbox(
        session_id=str(session_id), active_turn_anchor_id=new_root.id,
        execution_agent_id=agent_id, execution_user_id=user_id,
    )
    assert injected == [{"role": "user", "content": "also check this"}]
    assert await drain_turn_inbox(
        session_id=str(session_id), active_turn_anchor_id=new_root.id,
        execution_agent_id=agent_id, execution_user_id=user_id,
    ) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_backlog", [False, True])
async def test_group_senders_share_the_running_turn_in_fifo_order(legacy_backlog):
    from app.models.user import Identity, User
    from app.services.sender_attribution import wrap_with_sender

    agent_id, owner_id, session_id, root_id, generation = await _seed_running_im_turn()
    async with async_session() as db:
        owner = await db.get(User, owner_id)
        identity = Identity(username=f"followup-{uuid.uuid4().hex}", password_hash="x")
        db.add(identity)
        await db.flush()
        sender = User(identity_id=identity.id, tenant_id=owner.tenant_id,
                      display_name="Another group sender", role="member")
        db.add(sender)
        await db.flush()
        sender_id = sender.id
        session = await db.get(ChatSession, session_id)
        session.is_group = True
        queued = []
        inputs = [
            (sender_id, "Another group sender", "correct the end date"),
            (owner_id, "Receipt Tester", "also exclude short outages"),
            (sender_id, "Another group sender", "apply both corrections"),
        ]
        for offset, (user_id, name, content) in enumerate(inputs):
            result = await ingest_incoming_chat_message(
                db, session=session, agent_id=agent_id, user_id=user_id,
                content=content, source_channel="dingtalk",
                provider_event_id=str(uuid.uuid4()),
                created_at=datetime.now(UTC) + timedelta(seconds=offset),
                message_meta={"sender_display_name": name},
            )
            assert result.queued_to_running_turn is True
            assert result.message.message_meta["turn_inbox_mode"] == "current_turn"
            if legacy_backlog:
                result.message.message_meta = {
                    **result.message.message_meta,
                    "turn_inbox_mode": "next_turn",
                    "turn_inbox_anchor_id": str(uuid.uuid4()),
                    "turn_inbox_generation": generation - 1,
                }
            queued.append(result.message.id)
        await db.commit()

    assert await drain_turn_inbox(
        session_id=str(session_id), active_turn_anchor_id=root_id,
        execution_agent_id=agent_id, execution_user_id=owner_id,
    ) == [
        {"role": "user", "content": wrap_with_sender(content, user_id, name)}
        for user_id, name, content in inputs
    ]
    async with async_session() as db:
        for message_id in queued:
            followup = await db.get(ChatMessage, message_id)
            assert followup.message_meta["turn_inbox_state"] == "delivered"
            assert followup.message_meta["turn_inbox_anchor_id"] == str(root_id)
            assert followup.message_meta["turn_inbox_generation"] == generation


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_owner", ["agent", "anchor", "cancelled"])
async def test_wrong_or_cancelled_consumer_cannot_claim_pending_input(invalid_owner):
    agent_id, user_id, session_id, root_id, _ = await _seed_running_im_turn()
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        incoming = await ingest_incoming_chat_message(
            db, session=session, agent_id=agent_id, user_id=user_id,
            source_channel="dingtalk", content="must remain unconsumed",
            provider_event_id=str(uuid.uuid4()),
        )
        if invalid_owner == "cancelled":
            await transition_conversation_turn(
                db, agent_id=agent_id, conversation_id=str(session_id),
                turn_anchor_id=root_id, status="cancelled",
            )
        await db.commit()
        message_id = incoming.message.id

    kwargs = dict(
        session_id=str(session_id), execution_agent_id=agent_id,
        active_turn_anchor_id=root_id, execution_user_id=user_id,
    )
    if invalid_owner == "agent":
        kwargs["execution_agent_id"] = uuid.uuid4()
        assert await drain_turn_inbox(**kwargs) == []
    else:
        if invalid_owner == "anchor":
            kwargs["active_turn_anchor_id"] = uuid.uuid4()
        with pytest.raises(asyncio.CancelledError):
            await drain_turn_inbox(**kwargs)
    async with async_session() as db:
        message = await db.get(ChatMessage, message_id)
        assert message.message_meta["turn_inbox_state"] == "pending"


@pytest.mark.asyncio
async def test_conversation_inbox_does_not_consume_another_tenants_input():
    first = await _seed_running_im_turn()
    second = await _seed_running_im_turn()
    pending_ids = []
    for agent_id, user_id, session_id, root_id, _ in [first, second]:
        async with async_session() as db:
            session = await db.get(ChatSession, session_id)
            incoming = await ingest_incoming_chat_message(
                db, session=session, agent_id=agent_id, user_id=user_id,
                source_channel="dingtalk", content=f"private input {session_id}",
                provider_event_id=str(uuid.uuid4()),
            )
            await db.commit()
            pending_ids.append(incoming.message.id)
    agent_id, user_id, session_id, root_id, _ = first
    assert await drain_turn_inbox(
        session_id=str(second[2]), execution_agent_id=agent_id,
        active_turn_anchor_id=root_id, execution_user_id=user_id,
    ) == []
    assert await drain_turn_inbox(
        session_id=str(session_id), execution_agent_id=agent_id,
        active_turn_anchor_id=root_id, execution_user_id=user_id,
    ) == [{"role": "user", "content": f"private input {session_id}"}]
    async with async_session() as db:
        first_row = await db.get(ChatMessage, pending_ids[0])
        second_row = await db.get(ChatMessage, pending_ids[1])
        assert first_row.message_meta["turn_inbox_state"] == "delivered"
        assert second_row.message_meta["turn_inbox_state"] == "pending"
