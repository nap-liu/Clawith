"""Terminal receipt-cleanup and session-lock race tests."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.test_turn_inbox_receipt_anchor import (
    CHANNEL_RECEIPT_ANCHOR_KEY,
    CHANNEL_RECEIPT_CLEANUP_DIAGNOSTICS_KEY,
    CHANNEL_RECEIPT_PROVIDER_META_KEY,
    ChannelConfig,
    ChatMessage,
    ChatSession,
    _dispose_engine_between_tests,
    _seed_running_im_turn,
    async_session,
    bind_durable_channel_receipt_anchor,
    cleanup_durable_channel_receipt_anchor,
    conversation_turn_snapshot_for_session,
    drain_turn_inbox,
    transition_conversation_turn,
)

pytestmark = pytest.mark.asyncio


async def test_durable_cleanup_batches_without_dropping_unprocessed_anchors(
    monkeypatch,
):
    from app.services import dingtalk_reaction

    agent_id, _user_id, session_id, root_id, generation = await _seed_running_im_turn()
    message_ids = [root_id]
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        root = await db.get(ChatMessage, root_id)
        assert session is not None and root is not None
        root.message_meta = {
            **dict(root.message_meta or {}),
            CHANNEL_RECEIPT_PROVIDER_META_KEY: {
                "provider_message_id": f"provider-{root_id}",
                "provider_conversation_id": "cid-batch",
            },
        }
        for index in range(9):
            row = ChatMessage(
                agent_id=agent_id,
                user_id=root.user_id,
                role="user",
                content=f"follow-up-{index}",
                conversation_id=str(session_id),
                message_meta={
                    "attachments": [],
                    CHANNEL_RECEIPT_PROVIDER_META_KEY: {
                        "provider_message_id": f"provider-{index}",
                        "provider_conversation_id": "cid-batch",
                    },
                },
            )
            db.add(row)
            await db.flush()
            message_ids.append(row.id)
        session.im_config = {
            **dict(session.im_config or {}),
            CHANNEL_RECEIPT_ANCHOR_KEY: {
                "message_id": str(message_ids[-1]),
                "turn_anchor_id": str(root_id),
                "generation": generation,
                "cleanup_message_ids": [str(item) for item in message_ids],
                "cleanup_attempts": {},
            },
        }
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

    calls: list[str] = []

    async def fail_cleanup(
        _app_key: str,
        _app_secret: str,
        message_id: str,
        _conversation_id: str,
    ) -> bool:
        calls.append(message_id)
        return False

    monkeypatch.setattr(
        dingtalk_reaction,
        "cleanup_durable_progress_reactions",
        fail_cleanup,
    )
    assert await cleanup_durable_channel_receipt_anchor(
        session_id=str(session_id),
        agent_id=agent_id,
    ) is False
    assert len(calls) == 8
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        marker = session.im_config[CHANNEL_RECEIPT_ANCHOR_KEY]
        assert marker["cleanup_message_ids"] == [str(item) for item in message_ids]
        assert set(marker["cleanup_attempts"]) == {
            str(item) for item in message_ids[:8]
        }
        assert all(value == 1 for value in marker["cleanup_attempts"].values())

    for _attempt in range(2):
        assert await cleanup_durable_channel_receipt_anchor(
            session_id=str(session_id),
            agent_id=agent_id,
        ) is False
    assert len(calls) == 24
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        marker = session.im_config[CHANNEL_RECEIPT_ANCHOR_KEY]
        assert marker["cleanup_message_ids"] == [
            str(item) for item in message_ids[8:]
        ]
        assert marker["cleanup_attempts"] == {}
        diagnostics = session.im_config[CHANNEL_RECEIPT_CLEANUP_DIAGNOSTICS_KEY]
        assert diagnostics["exhausted_count"] == 8
        assert set(diagnostics["recent_message_ids"]) == {
            str(item) for item in message_ids[:8]
        }

    assert await cleanup_durable_channel_receipt_anchor(
        session_id=str(session_id),
        agent_id=agent_id,
    ) is False
    assert len(calls) == 26
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        marker = session.im_config[CHANNEL_RECEIPT_ANCHOR_KEY]
        assert marker["cleanup_message_ids"] == [
            str(item) for item in message_ids[8:]
        ]
        assert marker["cleanup_attempts"] == {
            str(item): 1 for item in message_ids[8:]
        }
        diagnostics = session.im_config[CHANNEL_RECEIPT_CLEANUP_DIAGNOSTICS_KEY]
        assert diagnostics["exhausted_count"] == 8
        assert set(diagnostics["recent_message_ids"]) == {
            str(item) for item in message_ids[:8]
        }


async def test_failed_terminal_cleanup_survives_next_turn_binding(monkeypatch):
    from app.services import dingtalk_reaction

    agent_id, user_id, session_id, root_id, _generation = await _seed_running_im_turn()
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        root = await db.get(ChatMessage, root_id)
        assert session is not None and root is not None
        root.message_meta = {
            **dict(root.message_meta or {}),
            CHANNEL_RECEIPT_PROVIDER_META_KEY: {
                "provider_message_id": "old-provider-message",
                "provider_conversation_id": "provider-conversation",
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

    async def fail_cleanup(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(
        dingtalk_reaction,
        "cleanup_durable_progress_reactions",
        fail_cleanup,
    )
    async with async_session() as db:
        await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(session_id),
            turn_anchor_id=root_id,
            status="completed",
        )
        await db.commit()
    assert await cleanup_durable_channel_receipt_anchor(
        session_id=str(session_id),
        agent_id=agent_id,
        require_terminal=True,
    ) is False

    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        next_root = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            role="user",
            content="next turn",
            conversation_id=str(session_id),
            message_meta={
                "attachments": [],
                CHANNEL_RECEIPT_PROVIDER_META_KEY: {
                    "provider_message_id": "new-provider-message",
                    "provider_conversation_id": "provider-conversation",
                },
            },
        )
        db.add(next_root)
        await db.flush()
        snapshot = await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(session_id),
            turn_anchor_id=next_root.id,
            status="running",
        )
        assert bind_durable_channel_receipt_anchor(session, next_root.id) is True
        await db.commit()
        next_root_id = next_root.id

    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        marker = session.im_config[CHANNEL_RECEIPT_ANCHOR_KEY]
        assert marker["turn_anchor_id"] == str(next_root_id)
        assert marker["generation"] == snapshot.generation
        assert marker["cleanup_message_ids"] == [str(root_id), str(next_root_id)]
        assert marker["cleanup_attempts"] == {str(root_id): 1}


async def test_one_startup_drains_ten_successful_terminal_receipt_anchors(
    monkeypatch,
):
    from app.services import dingtalk_reaction, turn_recovery

    agent_id, user_id, session_id, root_id, _generation = await _seed_running_im_turn()
    message_ids = [root_id]
    provider_ids = ["provider-0"]
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        root = await db.get(ChatMessage, root_id)
        assert session is not None and root is not None
        root.message_meta = {
            **dict(root.message_meta or {}),
            CHANNEL_RECEIPT_PROVIDER_META_KEY: {
                "provider_message_id": provider_ids[0],
                "provider_conversation_id": "startup-conversation",
            },
        }
        assert bind_durable_channel_receipt_anchor(session, root_id) is True
        for index in range(1, 10):
            provider_id = f"provider-{index}"
            row = ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="user",
                content=f"terminal-follow-up-{index}",
                conversation_id=str(session_id),
                message_meta={
                    "attachments": [],
                    CHANNEL_RECEIPT_PROVIDER_META_KEY: {
                        "provider_message_id": provider_id,
                        "provider_conversation_id": "startup-conversation",
                    },
                },
            )
            db.add(row)
            await db.flush()
            message_ids.append(row.id)
            provider_ids.append(provider_id)
        marker = dict(session.im_config or {}).get(CHANNEL_RECEIPT_ANCHOR_KEY)
        assert isinstance(marker, dict)
        session.im_config = {
            **dict(session.im_config or {}),
            CHANNEL_RECEIPT_ANCHOR_KEY: {
                **marker,
                "message_id": str(message_ids[-1]),
                "cleanup_message_ids": [str(message_id) for message_id in message_ids],
                "cleanup_attempts": {},
            },
        }
        db.add(
            ChannelConfig(
                agent_id=agent_id,
                channel_type="dingtalk",
                app_id=f"robot-code-{agent_id}",
                app_secret="robot-secret",
                is_configured=True,
            )
        )
        await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(session_id),
            turn_anchor_id=root_id,
            status="completed",
        )
        await db.commit()

    recalled_provider_ids: list[str] = []

    async def successful_cleanup(
        _app_key: str,
        _app_secret: str,
        provider_message_id: str,
        _provider_conversation_id: str,
    ) -> bool:
        recalled_provider_ids.append(provider_message_id)
        return True

    class NoopRecoveryLease:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def no_recoverable_anchors(_db):
        return []

    monkeypatch.setattr(
        dingtalk_reaction,
        "cleanup_durable_progress_reactions",
        successful_cleanup,
    )
    monkeypatch.setattr(turn_recovery, "RedisLeaseLock", NoopRecoveryLease)
    monkeypatch.setattr(
        turn_recovery,
        "_load_recoverable_anchors",
        no_recoverable_anchors,
    )

    stats = await turn_recovery.startup_turn_resume_once()
    assert stats.scanned == 0
    assert recalled_provider_ids == provider_ids
    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        assert session is not None
        assert CHANNEL_RECEIPT_ANCHOR_KEY not in dict(session.im_config or {})


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled"])
async def test_terminal_or_cancel_winning_session_lock_cannot_commit_stale_receipt_anchor(
    terminal_status: str,
):
    agent_id, user_id, session_id, root_id, generation = await _seed_running_im_turn()
    pending_id = uuid.uuid4()
    async with async_session() as db:
        db.add(
            ChatMessage(
                id=pending_id,
                agent_id=agent_id,
                user_id=user_id,
                role="user",
                content="racing input",
                conversation_id=str(session_id),
                message_meta={
                    "attachments": [],
                    "turn_inbox_state": "pending",
                    "turn_inbox_anchor_id": str(root_id),
                    "turn_inbox_generation": generation,
                    "turn_inbox_mode": "current_turn",
                },
            )
        )
        await db.commit()

    terminal_locked = asyncio.Event()
    allow_terminal_commit = asyncio.Event()

    async def terminal_owner() -> None:
        async with async_session() as db:
            await transition_conversation_turn(
                db,
                agent_id=agent_id,
                conversation_id=str(session_id),
                turn_anchor_id=root_id,
                status=terminal_status,
            )
            terminal_locked.set()
            await allow_terminal_commit.wait()
            await db.commit()

    terminal_task = asyncio.create_task(terminal_owner())
    await asyncio.wait_for(terminal_locked.wait(), timeout=1)
    drain_task = asyncio.create_task(
        drain_turn_inbox(
            session_id=str(session_id),
            active_turn_anchor_id=root_id,
            execution_agent_id=agent_id,
            execution_user_id=user_id,
        )
    )
    await asyncio.sleep(0.02)
    assert not drain_task.done()
    allow_terminal_commit.set()
    await asyncio.wait_for(terminal_task, timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(drain_task, timeout=1)

    async with async_session() as db:
        session = await db.get(ChatSession, session_id)
        pending = await db.get(ChatMessage, pending_id)
        assert session is not None and pending is not None
        assert CHANNEL_RECEIPT_ANCHOR_KEY not in dict(session.im_config or {})
        assert pending.message_meta["turn_inbox_state"] == "pending"
        assert conversation_turn_snapshot_for_session(session).status == terminal_status
