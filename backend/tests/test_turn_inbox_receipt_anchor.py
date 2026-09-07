"""Observable IM receipt anchoring across same-turn inbox consumption."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_compaction import ChatCompaction  # noqa: F401
from app.models.chat_session import ChatSession
from app.models.identity import IdentityProvider  # noqa: F401
from app.models.participant import Participant  # noqa: F401
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.services import channel_dispatch
from app.services.channel_reaction_recovery import (
    load_recovered_channel_reactions,
)
from app.services.chat_history import (
    build_llm_messages_from_rows,
    ingest_incoming_chat_message,
    persist_assistant_reply_row,
    persist_intermediate_assistant_reply,
)
from app.services.conversation_turn_lifecycle import (
    conversation_turn_snapshot_for_session,
    transition_conversation_turn,
)
from app.services.turn_inbox import (
    CHANNEL_RECEIPT_ANCHOR_KEY,
    CHANNEL_RECEIPT_CLEANUP_DIAGNOSTICS_KEY,
    CHANNEL_RECEIPT_PROVIDER_META_KEY,
    bind_durable_channel_receipt_anchor,
    cleanup_durable_channel_receipt_anchor,
    cleanup_stale_channel_receipt_anchors,
    drain_turn_inbox,
    durable_channel_receipt_anchor_id,
    load_durable_channel_receipt_anchor,
)

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


async def test_restart_rebuilds_reaction_hooks_from_current_durable_im_receipt(
    monkeypatch,
):
    from app.services import dingtalk_reaction

    agent_id, _user_id, session_id, root_id, _generation = await _seed_running_im_turn()
    robot_code = f"restart-robot-{uuid.uuid4().hex}"
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        root = await db.get(ChatMessage, root_id)
        assert session is not None and root is not None
        root.message_meta = {
            **dict(root.message_meta or {}),
            CHANNEL_RECEIPT_PROVIDER_META_KEY: {
                "provider_message_id": "restart-provider-message",
                "provider_conversation_id": "restart-provider-conversation",
            },
        }
        db.add(
            ChannelConfig(
                agent_id=agent_id,
                channel_type="dingtalk",
                app_id=robot_code,
                app_secret="restart-secret",
                is_configured=True,
            )
        )
        await db.commit()

    events: list[tuple[str, str]] = []

    async def cleanup(*_args) -> bool:
        events.append(("cleanup", "stale"))
        return True

    async def attach(*args) -> bool:
        events.append(("attach", args[-1]))
        return True

    async def recall(*args) -> bool:
        events.append(("recall", args[-1]))
        return True

    monkeypatch.setattr(
        dingtalk_reaction,
        "cleanup_durable_progress_reactions",
        cleanup,
    )
    monkeypatch.setattr(dingtalk_reaction, "add_reaction", attach)
    monkeypatch.setattr(dingtalk_reaction, "recall_reaction", recall)

    reactions = await load_recovered_channel_reactions(
        agent_id=agent_id,
        conversation_id=str(session_id),
    )
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        assert durable_channel_receipt_anchor_id(session) == root_id
    assert reactions.on_recover is not None
    assert reactions.on_consume is not None
    assert reactions.on_tool_call is not None
    assert reactions.on_complete is not None

    await reactions.on_recover()
    await reactions.on_consume()
    await reactions.on_tool_call(
        {"status": "running", "name": "web_search", "args": {}}
    )
    await reactions.on_complete("done")

    assert events == [
        ("cleanup", "stale"),
        ("attach", dingtalk_reaction.DEFAULT_THINKING_REACTION),
        ("recall", dingtalk_reaction.DEFAULT_THINKING_REACTION),
        ("attach", "🌐"),
        ("recall", "🌐"),
    ]
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        assert CHANNEL_RECEIPT_ANCHOR_KEY not in dict(session.im_config or {})


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
    owner_reactions = _receipt_hooks("root", events)

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
        assert owner_reactions.on_thinking is not None
        await owner_reactions.on_thinking("first is active")
        assert events[-1] == "thinking:first"
        first_drained.set()

        await second_pending.wait()
        # Durable admission alone must not steal the receipt anchor.
        assert events[-1] == "thinking:first"
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
            "thinking:first",
            "dispose:first",
            "attach:second",
        ]
        assert owner_reactions.on_thinking is not None
        await owner_reactions.on_thinking("second is active")
        assert events[-1] == "thinking:second"
        second_drained.set()

        await third_pending.wait()
        # This one stays pending and therefore must never become the receipt anchor.
        assert events[-1] == "thinking:second"
        return "done"

    owner = asyncio.create_task(
        channel_dispatch.run_channel_message(
            lock_key,
            is_command=False,
            reactions=owner_reactions,
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
        "thinking:first",
        "dispose:first",
        "attach:second",
        "thinking:second",
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
        assert session.im_config[CHANNEL_RECEIPT_ANCHOR_KEY] == {
            "message_id": str(inserted_ids[1]),
            "turn_anchor_id": str(root_id),
            "generation": snapshot.generation,
            "cleanup_message_ids": [str(inserted_ids[1])],
            "cleanup_attempts": {},
        }

    # Simulate a process restart: reaction callback bundles cannot be restored,
    # but the committed canonical message anchor must survive without either map.
    async with channel_dispatch._running_turns_guard:
        channel_dispatch._active_reactions.clear()
        channel_dispatch._pending_receipt_anchors.clear()
    assert await load_durable_channel_receipt_anchor(
        session_id=str(session_id),
        agent_id=agent_id,
    ) == inserted_ids[1]


async def _load_session(session_id: uuid.UUID) -> ChatSession:
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        return session


async def _ingest_interjection(
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    content: str,
    provider_message_id: str | None = None,
    provider_conversation_id: str | None = None,
) -> uuid.UUID:
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        message_meta = {"attachments": []}
        if provider_message_id and provider_conversation_id:
            message_meta[CHANNEL_RECEIPT_PROVIDER_META_KEY] = {
                "provider_message_id": provider_message_id,
                "provider_conversation_id": provider_conversation_id,
            }
        ingested = await ingest_incoming_chat_message(
            db,
            session=session,
            agent_id=agent_id,
            user_id=user_id,
            content=content,
            source_channel="dingtalk",
            provider_event_id=f"event-{uuid.uuid4()}",
            actor_ref="staff-receipt-tester",
            message_meta=message_meta,
        )
        assert ingested.queued_to_running_turn is True
        await db.commit()
        return ingested.message.id


async def test_late_injection_history_is_root_a1_injection_a2():
    from app.services.message_context_order import order_messages_for_context

    agent_id, user_id, session_id, root_id, _generation = await _seed_running_im_turn()
    injection_id = await _ingest_interjection(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        content="injection",
    )
    persisted_ids: list[uuid.UUID] = []

    async def persist_a1(*, created_at=None):
        persisted_ids.append(
            await persist_intermediate_assistant_reply(
                async_session,
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=str(session_id),
                content="A1",
                turn_anchor_id=root_id,
                created_at=created_at,
            )
        )

    injected = await drain_turn_inbox(
        session_id=str(session_id),
        active_turn_anchor_id=root_id,
        execution_agent_id=agent_id,
        execution_user_id=user_id,
        before_injection=persist_a1,
    )
    assert injected == [{"role": "user", "content": "injection"}]
    assert len(persisted_ids) == 1

    async with async_session() as db:
        terminal_id = await persist_assistant_reply_row(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=str(session_id),
            content="A1\n\nA2",
            turn_anchor_id=root_id,
        )
        await db.commit()

    async with async_session() as db:
        rows = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.id.in_(
                            [root_id, persisted_ids[0], injection_id, terminal_id]
                        )
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            ).scalars()
        )
    rows = order_messages_for_context(rows)
    assert [(row.role, row.content) for row in rows] == [
        ("user", "root request"),
        ("assistant", "A1"),
        ("user", "injection"),
        ("assistant", "A2"),
    ]
    assert rows[1].message_meta["artifact_role"] == "intermediate_assistant"
    assert rows[1].message_meta["turn_status"] == "running"
    assert rows[-1].message_meta["turn_status"] == "completed"
    assert [
        (message["role"], message["content"])
        for message in build_llm_messages_from_rows(rows)
    ] == [
        ("user", "root request"),
        ("assistant", "A1"),
        ("user", "injection"),
        ("assistant", "A2"),
    ]


async def test_never_returning_anchor_hook_cannot_freeze_committed_consume(
    monkeypatch,
):
    agent_id, user_id, session_id, root_id, _generation = await _seed_running_im_turn()
    session = await _load_session(session_id)
    lock_key = channel_dispatch.channel_session_lock_key(
        agent_id,
        "dingtalk",
        session.external_conv_id,
    )
    monkeypatch.setattr(
        channel_dispatch,
        "CHANNEL_REACTION_HOOK_TIMEOUT_SECONDS",
        0.01,
    )
    owner_ready = asyncio.Event()
    pending_ready = asyncio.Event()
    hook_started = asyncio.Event()
    hook_cancelled = asyncio.Event()
    inserted_id: uuid.UUID | None = None

    async def never_consume() -> None:
        hook_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            hook_cancelled.set()

    async def owner_work() -> str:
        await channel_dispatch.mark_channel_turn_admitted()
        owner_ready.set()
        await pending_ready.wait()
        drained = await drain_turn_inbox(
            session_id=str(session_id),
            active_turn_anchor_id=root_id,
            execution_agent_id=agent_id,
            execution_user_id=user_id,
        )
        assert [message["content"] for message in drained] == ["never hook"]
        return "done"

    owner = asyncio.create_task(
        channel_dispatch.run_channel_message(
            lock_key,
            is_command=False,
            reactions=channel_dispatch.ChannelReactions(),
            work=owner_work,
        )
    )
    await asyncio.wait_for(owner_ready.wait(), timeout=1)

    async def interjection_work() -> str:
        nonlocal inserted_id
        inserted_id = await _ingest_interjection(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            content="never hook",
        )
        return "queued"

    await channel_dispatch.run_channel_message(
        lock_key,
        is_command=False,
        reactions=channel_dispatch.ChannelReactions(on_consume=never_consume),
        work=interjection_work,
    )
    pending_ready.set()

    assert await asyncio.wait_for(owner, timeout=0.5) == "done"
    assert inserted_id is not None
    assert hook_started.is_set()
    await asyncio.wait_for(hook_cancelled.wait(), timeout=0.1)
    assert await load_durable_channel_receipt_anchor(
        session_id=str(session_id),
        agent_id=agent_id,
    ) == inserted_id

    # Opposite race order: once consume commits first, a later terminal commit
    # keeps the same generation's durable receipt anchor available for cleanup.
    async with async_session() as db:
        await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(session_id),
            turn_anchor_id=root_id,
            status="completed",
        )
        await db.commit()
    assert await load_durable_channel_receipt_anchor(
        session_id=str(session_id),
        agent_id=agent_id,
    ) == inserted_id


async def test_cancel_after_drain_commit_recovers_old_dingtalk_reaction_from_durable_marker(
    monkeypatch,
):
    """The exact commit/hook cancellation gap remains cleanup-capable after restart."""

    from app.services import dingtalk_reaction

    agent_id, user_id, session_id, root_id, generation = await _seed_running_im_turn()
    provider_conversation_id = f"cid-{uuid.uuid4().hex[:8]}"
    old_provider_message_id = f"old-{uuid.uuid4().hex[:8]}"
    new_provider_message_id = f"new-{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        root = await db.get(ChatMessage, root_id)
        assert session is not None and root is not None
        root.message_meta = {
            **dict(root.message_meta or {}),
            CHANNEL_RECEIPT_PROVIDER_META_KEY: {
                "provider_message_id": old_provider_message_id,
                "provider_conversation_id": provider_conversation_id,
            },
        }
        assert bind_durable_channel_receipt_anchor(session, root_id) is True
        db.add(
            ChannelConfig(
                agent_id=agent_id,
                channel_type="dingtalk",
                app_id=f"robot-code-{agent_id}",
                app_secret="robot-secret",
                is_configured=True,
            )
        )
        await db.commit()

    session = await _load_session(session_id)
    lock_key = channel_dispatch.channel_session_lock_key(
        agent_id,
        "dingtalk",
        session.external_conv_id,
    )
    owner_ready = asyncio.Event()
    pending_ready = asyncio.Event()
    old_dispose_started = asyncio.Event()
    events: list[str] = []
    inserted_id: uuid.UUID | None = None

    async def old_consume() -> None:
        events.append("old_attach")

    async def old_complete(_reply: str) -> None:
        events.append("old_dispose_start")
        old_dispose_started.set()
        await asyncio.Event().wait()

    async def new_consume() -> None:
        events.append("new_attach")

    async def owner_work() -> str:
        await channel_dispatch.mark_channel_turn_admitted()
        owner_ready.set()
        await pending_ready.wait()
        await drain_turn_inbox(
            session_id=str(session_id),
            active_turn_anchor_id=root_id,
            execution_agent_id=agent_id,
            execution_user_id=user_id,
        )
        return "should-not-complete"

    owner = asyncio.create_task(
        channel_dispatch.run_channel_message(
            lock_key,
            is_command=False,
            reactions=channel_dispatch.ChannelReactions(
                on_consume=old_consume,
                on_complete=old_complete,
            ),
            work=owner_work,
        )
    )
    await asyncio.wait_for(owner_ready.wait(), timeout=1)

    async def interjection_work() -> str:
        nonlocal inserted_id
        inserted_id = await _ingest_interjection(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            content="consume then crash",
            provider_message_id=new_provider_message_id,
            provider_conversation_id=provider_conversation_id,
        )
        return "queued"

    await channel_dispatch.run_channel_message(
        lock_key,
        is_command=False,
        reactions=channel_dispatch.ChannelReactions(on_consume=new_consume),
        work=interjection_work,
    )
    pending_ready.set()
    await asyncio.wait_for(old_dispose_started.wait(), timeout=1)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    assert inserted_id is not None
    assert events == ["old_attach", "old_dispose_start"]
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        interjection = await db.get(ChatMessage, inserted_id)
        assert session is not None and interjection is not None
        assert interjection.message_meta["turn_inbox_state"] == "delivered"
        assert session.im_config[CHANNEL_RECEIPT_ANCHOR_KEY] == {
            "message_id": str(inserted_id),
            "turn_anchor_id": str(root_id),
            "generation": generation,
            "cleanup_message_ids": [str(root_id), str(inserted_id)],
            "cleanup_attempts": {},
        }

    from app.services.chat_history import load_recoverable_history_for_turn
    from app.services.turn_recovery import _find_turn_anchor_for_latest

    async with async_session() as db:
        latest = await db.get(ChatMessage, inserted_id)
        assert latest is not None
        selected_root = await _find_turn_anchor_for_latest(db, latest)
        assert selected_root is not None and selected_root.id == root_id
        recoverable_history = await load_recoverable_history_for_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(session_id),
            turn_anchor_id=root_id,
            ctx_size=100,
        )
    assert [
        message["content"] for message in recoverable_history if message["role"] == "user"
    ][-2:] == ["root request", "consume then crash"]

    # Process memory is gone; only persisted provider coordinates remain.
    async with channel_dispatch._running_turns_guard:
        channel_dispatch._active_reactions.clear()
        channel_dispatch._pending_receipt_anchors.clear()

    recalls: list[tuple[str, str, str]] = []

    async def fake_post_reaction(**kwargs) -> bool:
        recalls.append(
            (
                kwargs["message_id"],
                kwargs["conversation_id"],
                kwargs["reaction_name"],
            )
        )
        return kwargs["message_id"] == old_provider_message_id

    monkeypatch.setattr(dingtalk_reaction, "_post_reaction", fake_post_reaction)
    assert await cleanup_stale_channel_receipt_anchors() == 0
    assert recalls == []
    assert await load_durable_channel_receipt_anchor(
        session_id=str(session_id),
        agent_id=agent_id,
    ) == inserted_id

    async with async_session() as db:
        await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(session_id),
            turn_anchor_id=root_id,
            status="completed",
        )
        await db.commit()
    assert await cleanup_stale_channel_receipt_anchors() == 1
    assert (
        old_provider_message_id,
        provider_conversation_id,
        dingtalk_reaction.DEFAULT_THINKING_REACTION,
    ) in recalls
    assert any(item[0] == new_provider_message_id for item in recalls)
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        assert CHANNEL_RECEIPT_ANCHOR_KEY not in dict(session.im_config or {})
        diagnostics = session.im_config[CHANNEL_RECEIPT_CLEANUP_DIAGNOSTICS_KEY]
        assert diagnostics["exhausted_count"] == 1
        assert diagnostics["recent_message_ids"] == [str(inserted_id)]
