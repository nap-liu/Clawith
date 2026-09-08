"""Mechanical continuation of startup turn-recovery tests."""

import pytest

from tests.test_turn_recovery import (
    ChatMessage,
    ChatSession,
    _dispose_engine_between_tests,  # noqa: F401 - pytest autouse fixture
    _make_agent_with_model,
    _make_user_anchor,
    async_session,
    asyncio,
    datetime,
    json,
    select,
    timedelta,
    timezone,
    uuid,
)

pytestmark = pytest.mark.asyncio
async def test_scanner_shutdown_cancels_children_and_releases_global_lock(monkeypatch):
    """Application shutdown collects recovery children and releases batch ownership."""
    from types import SimpleNamespace

    from app.services import turn_recovery

    anchor = SimpleNamespace(id=uuid.uuid4())
    recovery_started = asyncio.Event()

    async def fake_load(_db):
        return [anchor]

    async def fake_resume(_anchor):
        recovery_started.set()
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(turn_recovery, "_load_recoverable_anchors", fake_load)
    monkeypatch.setattr(turn_recovery, "resume_turn", fake_resume)

    scanner = asyncio.create_task(turn_recovery.startup_turn_resume_once(limit=1))
    await asyncio.wait_for(recovery_started.wait(), timeout=1)
    scanner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await scanner

    async def resumed_after_shutdown(_anchor):
        return True

    monkeypatch.setattr(turn_recovery, "resume_turn", resumed_after_shutdown)
    retry_stats = await asyncio.wait_for(
        turn_recovery.startup_turn_resume_once(limit=1),
        timeout=1,
    )
    assert retry_stats.scanned == 1
    assert retry_stats.resumed == 1


async def test_concurrent_startup_scanners_resume_one_durable_turn_once(monkeypatch):
    """The global batch lock and durable assistant boundary prevent duplicate recovery."""
    from app.services import turn_recovery

    agent_id, user_id = await _make_agent_with_model()
    conversation_id = f"dual_scanner_{uuid.uuid4().hex}"
    anchor_id = await _make_user_anchor(
        agent_id,
        user_id,
        conv=conversation_id,
        content="recover exactly once",
    )
    deliveries: list[uuid.UUID] = []

    async def fake_llm(*_args, **_kwargs):
        await asyncio.sleep(0)
        return "one recovered reply"

    async def fake_deliver(*, message_id, **_kwargs):
        deliveries.append(uuid.UUID(str(message_id)))
        return True

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_llm)
    monkeypatch.setattr(
        turn_recovery,
        "deliver_recovered_reply_to_origin",
        fake_deliver,
    )

    first, second = await asyncio.gather(
        turn_recovery.startup_turn_resume_once(limit=10),
        turn_recovery.startup_turn_resume_once(limit=10),
    )

    assert sorted((first.scanned, second.scanned)) == [0, 1]
    assert first.resumed + second.resumed == 1
    assert len(deliveries) == 1
    async with async_session() as db:
        assistant_rows = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conversation_id,
                    ChatMessage.role == "assistant",
                )
            )
        ).scalars().all()
    assert len(assistant_rows) == 1
    assert assistant_rows[0].message_meta["turn_anchor_id"] == str(anchor_id)


async def test_stop_command_cancels_recovery_without_local_running_task(monkeypatch):
    """A startup-recovered turn is stoppable even though it is not in _running_turns."""
    from app.services import channel_commands, turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model()
    external_conv_id = f"dingtalk_group_stop_recovery_{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk recovering",
            source_channel="dingtalk",
            external_conv_id=external_conv_id,
        )
        db.add(session)
        await db.flush()
        conv = str(session.id)
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="recovering turn",
        )
        anchor_id = anchor.id
        await db.commit()

    async def no_local_task(_lock_key: str) -> bool:
        return False

    monkeypatch.setattr(channel_commands, "cancel_running_turn", no_local_task)
    async with async_session() as db:
        result = await channel_commands.handle_channel_command(
            db=db,
            command="/stop",
            agent_id=agent_id,
            user_id=user_id,
            external_conv_id=external_conv_id,
            source_channel="dingtalk",
        )
        await db.commit()

    assert "已请求停止" in result["message"]
    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
    assert anchor.message_meta["turn_status"] == "cancelled"
    assert await turn_recovery.resume_turn(anchor) is False


async def test_stop_command_leaves_pending_confirmation_suspended(monkeypatch):
    """A pending confirmation is waiting for input, not a running recovery turn."""
    from app.services import channel_commands
    from app.services.chat_history import (
        persist_incoming_user_message,
        persist_pending_confirmation_row,
    )

    agent_id, user_id = await _make_agent_with_model()
    external_conv_id = f"dingtalk_group_pending_{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk pending confirmation",
            source_channel="dingtalk",
            external_conv_id=external_conv_id,
        )
        db.add(session)
        await db.flush()
        conv = str(session.id)
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="ask before action",
        )
        anchor_id = anchor.id
        pending_id = await persist_pending_confirmation_row(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            name="request_confirmation",
            args={"title": "确认", "summary": "是否继续"},
            turn_anchor_id=anchor_id,
        )
        await db.commit()

    async def no_local_task(_lock_key: str) -> bool:
        return False

    monkeypatch.setattr(channel_commands, "cancel_running_turn", no_local_task)
    async with async_session() as db:
        result = await channel_commands.handle_channel_command(
            db=db,
            command="/stop",
            agent_id=agent_id,
            user_id=user_id,
            external_conv_id=external_conv_id,
            source_channel="dingtalk",
        )
        await db.commit()

    assert "没有正在执行" in result["message"]
    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
        pending = await db.get(ChatMessage, pending_id)
    assert anchor.message_meta.get("turn_status") != "cancelled"
    assert json.loads(pending.content)["status"] == "pending"


async def test_stop_command_leaves_onmessage_owned_turn_unchanged(monkeypatch):
    """TriggerExecution-owned event turns are outside channel recovery ownership."""
    from app.services import channel_commands
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model()
    external_conv_id = f"dingtalk_group_onmessage_{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk on_message",
            source_channel="dingtalk",
            external_conv_id=external_conv_id,
        )
        db.add(session)
        await db.flush()
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=str(session.id),
            content="owned by trigger execution",
            message_meta={"consumed_by_onmessage": True},
        )
        anchor_id = anchor.id
        await db.commit()

    async def no_local_task(_lock_key: str) -> bool:
        return False

    monkeypatch.setattr(channel_commands, "cancel_running_turn", no_local_task)
    async with async_session() as db:
        result = await channel_commands.handle_channel_command(
            db=db,
            command="/stop",
            agent_id=agent_id,
            user_id=user_id,
            external_conv_id=external_conv_id,
            source_channel="dingtalk",
        )
        await db.commit()

    assert "没有正在执行" in result["message"]
    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
    assert anchor.message_meta.get("turn_status") != "cancelled"


async def test_startup_scan_skips_archived_channel_session(monkeypatch):
    """A /new generation boundary must make the old session unrecoverable."""
    from app.services import turn_recovery

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk archived",
            source_channel="dingtalk",
            external_conv_id=f"dingtalk_group_old__archived_{uuid.uuid4().hex[:8]}",
        )
        db.add(session)
        await db.flush()
        conv = str(session.id)
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conv,
                role="user",
                content="old generation",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        await db.commit()

    async def fail_if_resumed(_anchor):
        raise AssertionError("archived sessions must not be resumed after restart")

    monkeypatch.setattr(turn_recovery, "resume_turn", fail_if_resumed)

    stats = await turn_recovery.startup_turn_resume_once(limit=10)

    assert stats.scanned == 0
    assert stats.resumed == 0


async def test_startup_scan_recovers_any_channel_tail_without_adapter(monkeypatch):
    """Startup recovery must resume every channel's unfinished turn tail."""
    from app.services import turn_recovery

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="Feishu",
            source_channel="feishu",
            external_conv_id="feishu_p2p_open-id",
        )
        db.add(session)
        await db.flush()
        conv = str(session.id)
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conv,
                role="user",
                content="feishu should recover too",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        await db.commit()

    async def fake_llm(*_args, **kwargs):
        assert kwargs["session_id"] == conv
        return "recovered feishu tail"

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_llm)

    stats = await turn_recovery.startup_turn_resume_once(limit=10)

    assert stats.scanned == 1
    assert stats.resumed == 1
    async with async_session() as db:
        reply = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv,
                    ChatMessage.role == "assistant",
                    ChatMessage.content == "recovered feishu tail",
                )
            )
        ).scalar_one_or_none()
    assert reply is not None


async def test_startup_scan_skips_recent_dingtalk_assistant_tail(monkeypatch):
    """A completed DingTalk turn must not be delivered again after a restart."""
    from app.services import turn_recovery

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk",
            source_channel="dingtalk",
            external_conv_id="dingtalk_p2p_staff-redeliver",
        )
        db.add(session)
        await db.flush()
        conv = str(session.id)
        db.add_all(
            [
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    conversation_id=conv,
                    role="user",
                    content="before crash",
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
                ),
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    conversation_id=conv,
                    role="assistant",
                    content="already delivered",
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                ),
            ]
        )
        await db.commit()

    async def fail_if_llm_called(*_args, **_kwargs):
        raise AssertionError("a completed assistant tail must not rerun the LLM")

    delivered = []

    async def fake_deliver(*, agent_id, conversation_id, reply):
        delivered.append((agent_id, conversation_id, reply))
        return True

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fail_if_llm_called)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver)

    stats = await turn_recovery.startup_turn_resume_once(limit=10)

    assert stats.scanned == 0
    assert stats.resumed == 0
    assert delivered == []


async def test_resume_turn_continues_from_recoverable_history_and_marks_completed(monkeypatch):
    """A processing user anchor is resumed via the normal channel LLM path."""
    from app.services import turn_recovery
    from app.services.channel_dispatch import ChannelReactions
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

    captured = {}
    reaction_events: list[str] = []

    async def reaction_prepare():
        reaction_events.append("prepare")

    async def reaction_consume():
        reaction_events.append("consume")

    async def reaction_tool(_event):
        reaction_events.append("tool")

    async def reaction_complete(_reply):
        reaction_events.append("complete")

    async def fake_reactions(**_kwargs):
        return ChannelReactions(
            on_recover=reaction_prepare,
            on_consume=reaction_consume,
            on_tool_call=reaction_tool,
            on_complete=reaction_complete,
        )

    async def fake_call_agent_llm(*args, **kwargs):
        captured.update(kwargs)
        await kwargs["on_tool_call"]({"status": "running", "name": "web_search"})
        return "resumed reply"

    monkeypatch.setattr(
        turn_recovery,
        "load_recovered_channel_reactions",
        fake_reactions,
    )
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert captured["continue_turn"] is True
    assert captured["recovery_mode"] is True
    assert reaction_events == ["prepare", "consume", "tool", "complete"]
    assert captured["turn_anchor_id"] == anchor_id
    assert captured["history"][-1] == {
        "role": "user",
        "content": "interrupted",
        "attachments": [],
    }
    async with async_session() as db:
        replies = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == conv,
                        ChatMessage.role == "assistant",
                        ChatMessage.content == "resumed reply",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(replies) == 1


async def test_recovered_turn_cancellation_runs_rebuilt_reaction_error_hook(
    monkeypatch,
):
    """A stop or shutdown during recovery clears provider progress feedback."""
    from app.services import turn_recovery
    from app.services.channel_dispatch import ChannelReactions

    agent_id, user_id = await _make_agent_with_model(context_window_size=2)
    anchor_id = await _make_user_anchor(
        agent_id,
        user_id,
        conv=f"cancel-recovery-reaction-{uuid.uuid4().hex}",
    )
    events: list[str] = []

    async def prepare():
        events.append("prepare")

    async def consume():
        events.append("consume")

    async def error(exc):
        assert isinstance(exc, asyncio.CancelledError)
        events.append("error")

    async def fake_reactions(**_kwargs):
        return ChannelReactions(
            on_recover=prepare,
            on_consume=consume,
            on_error=error,
        )

    async def cancelled_llm(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        turn_recovery,
        "load_recovered_channel_reactions",
        fake_reactions,
    )
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", cancelled_llm)

    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
    with pytest.raises(asyncio.CancelledError):
        await turn_recovery.resume_turn(anchor)

    assert events == ["prepare", "consume", "error"]


async def test_resume_turn_delivers_dingtalk_reply_to_origin_runtime(monkeypatch):
    """Recovered IM turns must be delivered through their original channel runtime."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=1)
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk",
            source_channel="dingtalk",
            external_conv_id="dingtalk_p2p_staff-1",
        )
        db.add(session)
        await db.flush()
        conv = str(session.id)
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="interrupted from dingtalk",
        )
        anchor_id = anchor.id
        await db.commit()

    async def fake_call_agent_llm(*_args, **_kwargs):
        return "dingtalk resumed reply"

    delivered = []
    lifecycle_events: list[str] = []

    async def fake_deliver(**kwargs):
        lifecycle_events.append("terminal-delivered")
        delivered.append(kwargs)
        return True

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert lifecycle_events == ["terminal-delivered"]
    assert len(delivered) == 1
    delivered_message_id = delivered[0].pop("message_id")
    assert isinstance(delivered_message_id, uuid.UUID)
    assert delivered[0] == {
        "agent_id": agent_id,
        "conversation_id": conv,
        "reply": "dingtalk resumed reply",
        "expected_source_channel": "dingtalk",
        "expected_external_conv_id": "dingtalk_p2p_staff-1",
        "validate_external_conv_id": True,
    }


async def test_resume_turn_drops_reply_when_session_is_archived_during_recovery(monkeypatch):
    """A concurrent /new must win over an in-flight restart recovery."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=1)
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk",
            source_channel="dingtalk",
            external_conv_id="dingtalk_group_generation-1",
        )
        db.add(session)
        await db.flush()
        session_id = session.id
        conv = str(session_id)
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="must not cross /new",
        )
        anchor_id = anchor.id
        await db.commit()

    async def fake_call_agent_llm(*_args, **_kwargs):
        async with async_session() as db:
            session = await db.get(ChatSession, session_id)
            session.external_conv_id = "dingtalk_group_generation-1__archived_test"
            await db.commit()
        return "stale recovered reply"

    async def fail_if_delivered(**_kwargs):
        raise AssertionError("stale recovery reply must not reach the archived route")

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fail_if_delivered)

    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)

    assert await turn_recovery.resume_turn(anchor) is False
    async with async_session() as db:
        reply = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv,
                    ChatMessage.role == "assistant",
                    ChatMessage.content == "stale recovered reply",
                )
            )
        ).scalar_one_or_none()
    assert reply is None
