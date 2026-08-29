"""Observable IM receipt anchoring across same-turn inbox consumption."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_compaction import ChatCompaction  # noqa: F401
from app.models.chat_session import ChatSession
from app.models.identity import IdentityProvider  # noqa: F401
from app.models.participant import Participant  # noqa: F401
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services import channel_dispatch
from app.services.chat_history import ingest_incoming_chat_message
from app.services.conversation_turn_lifecycle import (
    conversation_turn_snapshot_for_session,
    transition_conversation_turn,
)
from app.services.turn_inbox import drain_turn_inbox

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


async def _seed_running_im_turn() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, int]:
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        tenant = Tenant(name=f"receipt-{suffix}", slug=f"receipt-{suffix}")
        db.add(tenant)
        await db.flush()
        identity = Identity(
            username=f"receipt_{suffix}",
            email=f"receipt_{suffix}@test.local",
            password_hash="x",
        )
        db.add(identity)
        await db.flush()
        user = User(
            identity_id=identity.id,
            tenant_id=tenant.id,
            display_name="Receipt Tester",
            role="member",
            is_active=True,
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name=f"Receipt Agent {suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            status="idle",
        )
        db.add(agent)
        await db.flush()
        session = ChatSession(
            agent_id=agent.id,
            user_id=user.id,
            title="DingTalk receipt progression",
            source_channel="dingtalk",
            external_conv_id=f"dingtalk_p2p_{suffix}",
            is_group=False,
        )
        db.add(session)
        await db.flush()
        root = ChatMessage(
            agent_id=agent.id,
            user_id=user.id,
            role="user",
            content="root request",
            conversation_id=str(session.id),
            message_meta={"source_channel": "dingtalk", "attachments": []},
        )
        db.add(root)
        await db.flush()
        snapshot = await transition_conversation_turn(
            db,
            agent_id=agent.id,
            conversation_id=str(session.id),
            turn_anchor_id=root.id,
            status="running",
        )
        await db.commit()
        return agent.id, user.id, session.id, root.id, snapshot.generation


def _receipt_hooks(tag: str, events: list[str]) -> channel_dispatch.ChannelReactions:
    async def consume() -> None:
        events.append(f"attach:{tag}")

    async def complete(_reply: str) -> None:
        events.append(f"dispose:{tag}")

    async def thinking(_text: str) -> None:
        events.append(f"thinking:{tag}")

    return channel_dispatch.ChannelReactions(
        on_consume=consume,
        on_complete=complete,
        on_thinking=thinking,
    )


async def test_receipt_anchor_advances_only_after_each_interjected_message_is_consumed():
    agent_id, user_id, session_id, root_id, _generation = await _seed_running_im_turn()
    lock_key = channel_dispatch.channel_session_lock_key(
        agent_id,
        "dingtalk",
        (await _load_session(session_id)).external_conv_id,
    )
    events: list[str] = []
    owner_ready = asyncio.Event()
    first_pending = asyncio.Event()
    first_drained = asyncio.Event()
    second_pending = asyncio.Event()
    second_drained = asyncio.Event()
    third_pending = asyncio.Event()
    inserted_ids: list[uuid.UUID] = []

    async def insert_pending(text: str) -> str:
        async with async_session() as db:
            session = await db.get(ChatSession, session_id)
            assert session is not None
            ingested = await ingest_incoming_chat_message(
                db,
                session=session,
                agent_id=agent_id,
                user_id=user_id,
                content=text,
                source_channel="dingtalk",
                provider_event_id=f"event-{uuid.uuid4()}",
                actor_ref="staff-receipt-tester",
                message_meta={
                    "attachments": [],
                },
            )
            assert ingested.queued_to_running_turn is True
            assert ingested.consumed_by_onmessage is True
            await db.commit()
            inserted_ids.append(ingested.message.id)
        return "queued"

    async def owner_work() -> str:
        await channel_dispatch.mark_channel_turn_admitted()
        owner_ready.set()

        await first_pending.wait()
        assert events == ["attach:root"]
        first = await drain_turn_inbox(
            session_id=str(session_id),
            active_turn_anchor_id=root_id,
            execution_agent_id=agent_id,
            execution_user_id=user_id,
        )
        assert [message["content"] for message in first] == ["first interjection"]
        assert events == ["attach:root", "dispose:root", "attach:first"]
        first_drained.set()

        await second_pending.wait()
        # Durable admission alone must not steal the receipt anchor.
        assert events == ["attach:root", "dispose:root", "attach:first"]
        second = await drain_turn_inbox(
            session_id=str(session_id),
            active_turn_anchor_id=root_id,
            execution_agent_id=agent_id,
            execution_user_id=user_id,
        )
        assert [message["content"] for message in second] == ["second interjection"]
        assert events == [
            "attach:root",
            "dispose:root",
            "attach:first",
            "dispose:first",
            "attach:second",
        ]
        second_drained.set()

        await third_pending.wait()
        # This one stays pending and therefore must never become the receipt anchor.
        assert events[-1] == "attach:second"
        return "done"

    owner = asyncio.create_task(
        channel_dispatch.run_channel_message(
            lock_key,
            is_command=False,
            reactions=_receipt_hooks("root", events),
            work=owner_work,
        )
    )
    await asyncio.wait_for(owner_ready.wait(), timeout=1)

    await channel_dispatch.run_channel_message(
        lock_key,
        is_command=False,
        reactions=_receipt_hooks("first", events),
        work=lambda: insert_pending("first interjection"),
    )
    first_pending.set()
    await asyncio.wait_for(first_drained.wait(), timeout=1)

    await channel_dispatch.run_channel_message(
        lock_key,
        is_command=False,
        reactions=_receipt_hooks("second", events),
        work=lambda: insert_pending("second interjection"),
    )
    second_pending.set()
    await asyncio.wait_for(second_drained.wait(), timeout=1)

    await channel_dispatch.run_channel_message(
        lock_key,
        is_command=False,
        reactions=_receipt_hooks("third", events),
        work=lambda: insert_pending("third interjection"),
    )
    third_pending.set()
    assert await asyncio.wait_for(owner, timeout=1) == "done"

    assert events == [
        "attach:root",
        "dispose:root",
        "attach:first",
        "dispose:first",
        "attach:second",
        "dispose:second",
    ]
    async with async_session() as db:
        rows = {
            row.id: row
            for row in (
                await db.execute(select(ChatMessage).where(ChatMessage.id.in_(inserted_ids)))
            ).scalars()
        }
        assert [rows[row_id].message_meta["turn_inbox_state"] for row_id in inserted_ids] == [
            "delivered",
            "delivered",
            "pending",
        ]
        session = await db.get(ChatSession, session_id)
        snapshot = conversation_turn_snapshot_for_session(session)
        assert snapshot.anchor_id == root_id
        assert snapshot.status == "running"


async def _load_session(session_id: uuid.UUID) -> ChatSession:
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        return session
