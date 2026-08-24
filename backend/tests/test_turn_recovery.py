"""Tests for startup turn recovery primitives."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.database import async_session, engine
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.identity import IdentityProvider
from app.models.llm import LLMModel
from app.models.org import OrgMember
from app.models.participant import Participant  # noqa: F401
from app.models.tenant import Tenant
from app.models.user import Identity, User

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _dispose_engine_between_tests():
    async with async_session() as db:
        await db.execute(delete(ChatMessage))
        await db.commit()
    yield
    await engine.dispose()


async def _make_agent_with_model(*, context_window_size: int = 2) -> tuple[uuid.UUID, uuid.UUID]:
    suffix = uuid.uuid4().hex[:8]
    async with async_session() as db:
        tenant = Tenant(name=f"t_{suffix}", slug=f"t-{suffix}")
        db.add(tenant)
        await db.flush()

        identity = Identity(username=f"u_{suffix}", email=f"{suffix}@t.local", password_hash="x")
        db.add(identity)
        await db.flush()

        user = User(
            identity_id=identity.id,
            display_name="Tester",
            role="member",
            is_active=True,
            tenant_id=tenant.id,
        )
        db.add(user)
        await db.flush()

        model = LLMModel(
            tenant_id=tenant.id,
            provider="openai",
            model="test-model",
            api_key_encrypted="unused",
            label="Test Model",
            enabled=True,
            context_window=128000,
        )
        db.add(model)
        await db.flush()

        agent = Agent(
            name=f"Agent_{suffix}",
            creator_id=user.id,
            tenant_id=tenant.id,
            primary_model_id=model.id,
            context_window_size=context_window_size,
            status="idle",
        )
        db.add(agent)
        await db.commit()
        return agent.id, user.id


async def test_call_agent_llm_recovery_mode_keeps_supplied_history_and_appends_no_user(monkeypatch):
    """Recovery mode re-enters an interrupted turn instead of creating a new user turn."""
    import app.services.llm as llm_module
    from app.services.channel_llm import _call_agent_llm

    agent_id, user_id = await _make_agent_with_model(context_window_size=2)
    captured: dict = {}

    async def fake_failover(**kwargs):
        captured["messages"] = kwargs["messages"]
        return "done"

    monkeypatch.setattr(llm_module, "call_llm_with_failover", fake_failover)

    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "interrupted question"},
    ]

    async with async_session() as db:
        reply = await _call_agent_llm(
            db,
            agent_id=agent_id,
            user_text="SHOULD_NOT_APPEND",
            session_id="",
            user_id=user_id,
            history=history,
            continue_turn=True,
            recovery_mode=True,
        )

    assert reply == "done"
    assert captured["messages"] == history


async def test_call_agent_llm_releases_database_before_recovery_dispatch(monkeypatch):
    """Detached recovery does not reserve a pool connection during provider I/O."""
    import app.services.llm as llm_module
    from app.services.channel_llm import _call_agent_llm

    agent_id, user_id = await _make_agent_with_model(context_window_size=2)
    observed: dict[str, bool] = {}

    async with async_session() as db:

        async def fake_failover(**_kwargs):
            observed["in_transaction"] = db.in_transaction()
            return "done"

        monkeypatch.setattr(llm_module, "call_llm_with_failover", fake_failover)
        reply = await _call_agent_llm(
            db,
            agent_id=agent_id,
            user_text="",
            session_id="",
            user_id=user_id,
            history=[{"role": "user", "content": "interrupted"}],
            continue_turn=True,
            recovery_mode=True,
            release_db_before_dispatch=True,
        )

    assert reply == "done"
    assert observed == {"in_transaction": False}


async def test_call_agent_llm_recovery_mode_never_compacts_or_reloads(monkeypatch):
    """An interrupted turn is never rewritten or replayed during recovery."""
    import app.services.llm as llm_module
    from app.services.channel_llm import _call_agent_llm

    agent_id, user_id = await _make_agent_with_model(context_window_size=2)
    anchor_id = uuid.uuid4()
    captured: dict = {}

    async def fake_failover(**kwargs):
        captured["messages"] = kwargs["messages"]
        captured["context_recovery"] = kwargs.get("context_recovery")
        return "done"

    async def must_not_compact(**_kwargs):
        raise AssertionError("recovery continuations must never compact")

    monkeypatch.setattr(llm_module, "call_llm_with_failover", fake_failover)
    monkeypatch.setattr("app.services.llm.compactor.maybe_compact", must_not_compact)

    async with async_session() as db:
        reply = await _call_agent_llm(
            db,
            agent_id=agent_id,
            user_text="SHOULD_NOT_APPEND",
            session_id="recoverable-session",
            user_id=user_id,
            history=[{"role": "user", "content": "interrupted question"}],
            continue_turn=True,
            recovery_mode=True,
            turn_anchor_id=anchor_id,
        )

    assert reply == "done"
    assert captured["messages"] == [{"role": "user", "content": "interrupted question"}]
    assert captured["context_recovery"] is None


async def _make_user_anchor(agent_id, user_id, *, conv: str, content: str = "message") -> uuid.UUID:
    from app.services.chat_history import persist_incoming_user_message

    async with async_session() as db:
        row = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content=content,
        )
        await db.commit()
        return row.id


async def test_startup_recovery_scans_recent_incomplete_message_tails(monkeypatch):
    """Recovery scans recent message tails whose current user turn has no final assistant reply."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_pending_confirmation

    agent_id, user_id = await _make_agent_with_model()
    old_anchor = await _make_user_anchor(agent_id, user_id, conv=f"old_{uuid.uuid4().hex}", content="old")
    async with async_session() as db:
        old = (await db.execute(select(ChatMessage).where(ChatMessage.id == old_anchor))).scalar_one()
        old.created_at = datetime.now(timezone.utc) - timedelta(hours=7)
        await db.commit()

    complete_conv = f"complete_{uuid.uuid4().hex}"
    complete_anchor = await _make_user_anchor(agent_id, user_id, conv=complete_conv, content="complete")
    async with async_session() as db:
        complete_user = (await db.execute(select(ChatMessage).where(ChatMessage.id == complete_anchor))).scalar_one()
        complete_user.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=complete_conv,
                role="assistant",
                content="complete reply",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=4),
            )
        )
        await db.commit()

    pending_conv = f"pending_{uuid.uuid4().hex}"
    pending_anchor = await _make_user_anchor(agent_id, user_id, conv=pending_conv, content="needs approval")
    await persist_pending_confirmation(
        async_session,
        agent_id=agent_id,
        user_id=user_id,
        conversation_id=pending_conv,
        name="request_confirmation",
        args={"title": "确认", "summary": "等待"},
        turn_anchor_id=pending_anchor,
    )
    first_anchor = await _make_user_anchor(agent_id, user_id, conv=f"first_{uuid.uuid4().hex}", content="first")
    second_anchor = await _make_user_anchor(agent_id, user_id, conv=f"second_{uuid.uuid4().hex}", content="second")
    resumed: list[uuid.UUID] = []

    async def fake_resume(anchor):
        resumed.append(anchor.id)
        return True

    monkeypatch.setattr(turn_recovery, "resume_turn", fake_resume)

    stats = await turn_recovery.startup_turn_resume_once(limit=1)

    assert resumed == [first_anchor]
    assert stats.scanned == 1
    assert stats.resumed == 1

    resumed.clear()
    stats = await turn_recovery.startup_turn_resume_once(limit=2)

    assert resumed == [first_anchor, second_anchor]
    assert stats.scanned == 2
    assert stats.resumed == 2


async def test_startup_scan_recovers_recent_unanswered_user_without_turn_marker(monkeypatch):
    """Restart recovery is inferred from recent saved message order, not explicit turn markers."""
    from app.services import turn_recovery

    agent_id, user_id = await _make_agent_with_model()
    conv = f"markerless_{uuid.uuid4().hex}"
    deliveries: list[tuple[uuid.UUID, str, str]] = []

    async def fake_llm(*args, **kwargs):
        assert kwargs["continue_turn"] is True
        assert kwargs["recovery_mode"] is True
        assert kwargs["history"][-1]["role"] == "user"
        return "markerless recovered"

    async def fake_deliver(*, agent_id, conversation_id, reply):
        deliveries.append((agent_id, conversation_id, reply))
        return True

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver)

    async with async_session() as db:
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                conversation_id=conv,
                role="user",
                content="recover this recent markerless turn",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
        )
        await db.commit()

    stats = await turn_recovery.startup_turn_resume_once(limit=10)

    assert stats.scanned == 1
    assert stats.resumed == 1
    assert stats.failed == 0
    assert deliveries == [(agent_id, conv, "markerless recovered")]
    async with async_session() as db:
        rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conv)
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            )
            .scalars()
            .all()
        )
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[-1].content == "markerless recovered"


async def test_startup_scan_skips_cancelled_turn(monkeypatch):
    """A durable /stop marker must survive restart and suppress recovery."""
    from app.services import turn_recovery
    from app.services.chat_history import mark_latest_incomplete_turn_cancelled

    agent_id, user_id = await _make_agent_with_model()
    conv = f"cancelled_{uuid.uuid4().hex}"
    anchor_id = await _make_user_anchor(agent_id, user_id, conv=conv, content="stop this")

    async with async_session() as db:
        marked_id = await mark_latest_incomplete_turn_cancelled(
            db,
            agent_id=agent_id,
            conversation_id=conv,
            reason="stop",
        )
        await db.commit()

    assert marked_id == anchor_id

    async def fail_if_resumed(_anchor):
        raise AssertionError("cancelled turns must not be resumed after restart")

    monkeypatch.setattr(turn_recovery, "resume_turn", fail_if_resumed)

    stats = await turn_recovery.startup_turn_resume_once(limit=10)

    assert stats.scanned == 0
    assert stats.resumed == 0
    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
    assert anchor.message_meta["turn_status"] == "cancelled"
    assert anchor.message_meta["cancel_reason"] == "stop"


async def test_stopping_one_startup_recovery_turn_keeps_batch_running(monkeypatch):
    """Each startup anchor is a separate cancel unit, not the scanner task."""
    from types import SimpleNamespace

    from app.services import turn_recovery
    from app.services.active_turns import (
        cancel_active_turn,
        ensure_active_turn,
        list_active_turns,
        reset_active_turns_for_testing,
    )

    await reset_active_turns_for_testing()
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    anchors = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]
    first_ready = asyncio.Event()
    resumed_ids: list[uuid.UUID] = []

    async def fake_load(_db, *, limit):
        assert limit == 2
        return anchors

    async def fake_resume(anchor):
        await ensure_active_turn(
            owner_user_id=owner_id,
            agent_id=agent_id,
            session_id=str(anchor.id),
            turn_type="recovery",
        )
        resumed_ids.append(anchor.id)
        if anchor is anchors[0]:
            first_ready.set()
            await asyncio.Event().wait()
        return True

    monkeypatch.setattr(turn_recovery, "_load_recoverable_anchors", fake_load)
    monkeypatch.setattr(turn_recovery, "resume_turn", fake_resume)

    scanner = asyncio.create_task(turn_recovery.startup_turn_resume_once(limit=2))
    await first_ready.wait()
    record = (await list_active_turns(owner_user_id=owner_id))[0]
    await cancel_active_turn(record.turn_id, owner_user_id=owner_id)

    stats = await scanner
    assert resumed_ids == [anchors[0].id, anchors[1].id]
    assert stats.scanned == 2
    assert stats.skipped == 1
    assert stats.resumed == 1
    await reset_active_turns_for_testing()


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


async def test_startup_scan_uses_latest_message_save_time_without_markers(monkeypatch):
    """Only sessions whose latest saved message is recent and incomplete are resumed."""
    from app.services import turn_recovery

    agent_id, user_id = await _make_agent_with_model()
    suffix = uuid.uuid4().hex[:8]
    old_conv = f"old_markerless_{suffix}"
    complete_conv = f"complete_markerless_{suffix}"
    recent_conv = f"recent_markerless_{suffix}"
    resumed: list[str] = []

    async def fake_llm(*args, **kwargs):
        resumed.append(kwargs["session_id"])
        return f"done {kwargs['session_id']}"

    async def fake_deliver(*, agent_id, conversation_id, reply):
        return True

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver)

    now = datetime.now(timezone.utc)
    async with async_session() as db:
        db.add_all(
            [
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    conversation_id=old_conv,
                    role="user",
                    content="old incomplete",
                    created_at=now - timedelta(hours=7),
                ),
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    conversation_id=complete_conv,
                    role="user",
                    content="recent complete user",
                    created_at=now - timedelta(minutes=4),
                ),
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    conversation_id=complete_conv,
                    role="assistant",
                    content="recent complete assistant",
                    created_at=now - timedelta(minutes=3),
                ),
                ChatMessage(
                    agent_id=agent_id,
                    user_id=user_id,
                    conversation_id=recent_conv,
                    role="user",
                    content="recent incomplete",
                    created_at=now - timedelta(minutes=2),
                ),
            ]
        )
        await db.commit()

    stats = await turn_recovery.startup_turn_resume_once(limit=10)

    assert stats.scanned == 1
    assert stats.resumed == 1
    assert resumed == [recent_conv]


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

    async def fake_call_agent_llm(*args, **kwargs):
        captured.update(kwargs)
        return "resumed reply"

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert captured["continue_turn"] is True
    assert captured["recovery_mode"] is True
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

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return True

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert len(delivered) == 1
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


async def test_new_waits_for_locked_recovery_delivery(monkeypatch):
    """The route generation cannot change between final validation and send."""
    from app.services import channel_commands, turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=1)
    external_conv_id = f"dingtalk_group_locked_{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk locked delivery",
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
            content="lock delivery generation",
        )
        anchor_id = anchor.id
        await db.commit()

    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
    expected_origin = await turn_recovery._load_fresh_recovery_origin(anchor)
    assert expected_origin is not None

    delivery_entered = asyncio.Event()
    allow_delivery = asyncio.Event()
    delivery_finished = asyncio.Event()

    async def fake_deliver(**_kwargs):
        delivery_entered.set()
        await allow_delivery.wait()
        delivery_finished.set()
        return True

    async def no_local_task(_lock_key: str) -> bool:
        return False

    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver)
    monkeypatch.setattr(channel_commands, "cancel_running_turn", no_local_task)

    delivery_task = asyncio.create_task(
        turn_recovery._deliver_recovered_reply(
            anchor,
            expected_origin=expected_origin,
            reply="serialized reply",
            execution_agent_id=agent_id,
        )
    )
    await asyncio.wait_for(delivery_entered.wait(), timeout=2)

    async def archive_session():
        async with async_session() as db:
            result = await channel_commands.handle_channel_command(
                db=db,
                command="/new",
                agent_id=agent_id,
                user_id=user_id,
                external_conv_id=external_conv_id,
                source_channel="dingtalk",
            )
            await db.commit()
            return result

    archive_task = asyncio.create_task(archive_session())
    await asyncio.sleep(0.05)
    assert archive_task.done() is False

    allow_delivery.set()
    assert await asyncio.wait_for(delivery_task, timeout=2) is True
    assert delivery_finished.is_set()
    result = await asyncio.wait_for(archive_task, timeout=2)
    assert result["action"] == "new_session"

    async with async_session() as db:
        session = await db.get(ChatSession, uuid.UUID(conv))
    assert "__archived_" in session.external_conv_id


async def test_deliver_recovered_reply_routes_dingtalk_from_chat_session(monkeypatch):
    """Restart delivery should derive DingTalk runtime from ChatSession, not turn metadata."""
    from app.services.turn_runtime import deliver_recovered_reply_to_origin

    agent_id, user_id = await _make_agent_with_model(context_window_size=1)
    app_id = f"fake-app-id-{uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk",
            source_channel="dingtalk",
            external_conv_id="dingtalk_p2p_staff-42",
        )
        cfg = ChannelConfig(
            agent_id=agent_id,
            channel_type="dingtalk",
            app_id=app_id,
            app_secret="fake-secret",
            is_configured=True,
        )
        db.add_all([session, cfg])
        await db.commit()
        conv = str(session.id)

    captured: dict = {}

    async def fake_send(app_id, app_secret, user_ids, message, msg_type="text", robot_code=None):
        captured.update(
            {
                "app_id": app_id,
                "app_secret": app_secret,
                "user_ids": user_ids,
                "message": message,
                "msg_type": msg_type,
                "robot_code": robot_code,
            }
        )
        return {"errcode": 0}

    monkeypatch.setattr(
        "app.services.dingtalk_service.send_dingtalk_v1_robot_oto_message",
        fake_send,
    )

    ok = await deliver_recovered_reply_to_origin(
        agent_id=agent_id,
        conversation_id=conv,
        reply="恢复完成",
    )

    assert ok is True
    assert captured == {
        "app_id": app_id,
        "app_secret": "fake-secret",
        "user_ids": ["staff-42"],
        "message": "恢复完成",
        "msg_type": "markdown",
        "robot_code": app_id,
    }


async def test_deliver_reply_to_origin_contains_transport_exceptions(monkeypatch):
    """A channel outage must not unwind a turn whose assistant reply is already durable."""
    from app.services import turn_runtime

    agent_id = uuid.uuid4()
    conversation_id = str(uuid.uuid4())

    async def fake_load_turn_runtime(**_kwargs):
        return turn_runtime.TurnRuntime(
            session_found=True,
            source_channel="dingtalk",
            conversation_id=conversation_id,
            external_conv_id="dingtalk_group_test",
            is_group=True,
        )

    async def failing_delivery(**_kwargs):
        raise RuntimeError("temporary channel outage")

    monkeypatch.setattr(turn_runtime, "load_turn_runtime", fake_load_turn_runtime)
    monkeypatch.setattr(turn_runtime, "deliver_message_to_runtime", failing_delivery)

    delivered = await turn_runtime.deliver_reply_to_origin(
        agent_id=agent_id,
        conversation_id=conversation_id,
        reply="already persisted",
        require_transport=True,
    )

    assert delivered is False


async def test_resume_turn_continues_after_completed_tool_call_tail(monkeypatch):
    """Crash after a completed tool row should continue the same tool loop."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    conv = f"tool_tail_{uuid.uuid4().hex}"
    async with async_session() as db:
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="status please",
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
                        "name": "list_sessions",
                        "args": {"query": "刘喜", "limit": 5},
                        "status": "done",
                        "result": "session-1",
                    },
                    ensure_ascii=False,
                ),
                conversation_id=conv,
                created_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        await db.commit()

    captured = {}

    async def fake_call_agent_llm(*_args, **kwargs):
        captured.update(kwargs)
        return "final reply after tool"

    delivered = []

    async def fake_deliver(*, agent_id, conversation_id, reply):
        delivered.append((agent_id, conversation_id, reply))
        return True

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert captured["continue_turn"] is True
    assert captured["recovery_mode"] is True
    assert captured["turn_anchor_id"] == anchor_id
    assert [msg["role"] for msg in captured["history"]] == ["user", "assistant", "tool"]
    assert captured["history"][1]["tool_calls"][0]["function"]["name"] == "list_sessions"
    assert captured["history"][2]["content"] == "session-1"
    assert len(delivered) == 1
    async with async_session() as db:
        replies = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == conv,
                        ChatMessage.role == "assistant",
                        ChatMessage.content == "final reply after tool",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(replies) == 1


async def test_resume_turn_executes_unfinished_code_without_new_tool_snapshot(monkeypatch):
    """Recovery runs code but cannot widen its original, unavailable tool scope."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    conv = f"running_tool_{uuid.uuid4().hex}"
    call_id = "call_resume_running"
    async with async_session() as db:
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="run the code",
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
                        "name": "execute_code_aio",
                        "call_id": call_id,
                        "args": {
                            "language": "bash",
                            "code": "echo recovered",
                            "execution_mode": "foreground",
                        },
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

    executed = []

    async def fake_execute_tool(name, args, **kwargs):
        executed.append((name, args, kwargs))
        return "recovered\n"

    captured = {}

    async def fake_call_agent_llm(*_args, **kwargs):
        captured.update(kwargs)
        return "reply after recovered tool"

    async def fake_deliver(*_args, **_kwargs):
        return True

    monkeypatch.setattr(turn_recovery, "execute_tool", fake_execute_tool, raising=False)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert executed == [
        (
            "execute_code_aio",
            {
                "language": "bash",
                "code": "echo recovered",
                "execution_mode": "foreground",
            },
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "session_id": conv,
                "tool_call_id": call_id,
                "turn_anchor_id": anchor_id,
                "on_output": None,
            },
        )
    ]
    assert [msg["role"] for msg in captured["history"]] == ["user", "assistant", "tool"]
    assert captured["history"][1]["tool_calls"][0]["function"]["name"] == "execute_code_aio"
    assert captured["history"][1]["tool_calls"][0]["id"] == call_id
    assert captured["history"][2]["content"] == "recovered\n"

    async with async_session() as db:
        payloads = [
            json.loads(row.content)
            for row in (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conv, ChatMessage.role == "tool_call")
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            )
            .scalars()
            .all()
        ]
    assert [payload["status"] for payload in payloads] == ["running", "done"]
    assert payloads[1]["call_id"] == call_id
    assert payloads[1]["result"] == "recovered\n"
    async with async_session() as db:
        replies = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == conv,
                        ChatMessage.role == "assistant",
                        ChatMessage.content == "reply after recovered tool",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(replies) == 1


async def test_resume_turn_replays_only_tools_after_current_anchor(monkeypatch):
    """Restart must not replay unfinished tools belonging to an older turn."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    conv = f"current_anchor_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    async with async_session() as db:
        old_anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="old interrupted turn",
        )
        old_anchor.created_at = now - timedelta(seconds=4)
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "read_file",
                        "call_id": "old_call",
                        "args": {"path": "old.txt"},
                        "status": "running",
                        "result": "",
                    }
                ),
                conversation_id=conv,
                message_meta={"turn_anchor_id": str(old_anchor.id)},
                created_at=now - timedelta(seconds=3),
            )
        )
        current_anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="current interrupted turn",
        )
        current_anchor_id = current_anchor.id
        current_anchor.created_at = now - timedelta(seconds=2)
        db.add(
            ChatMessage(
                agent_id=agent_id,
                user_id=user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "read_file",
                        "call_id": "current_call",
                        "args": {"path": "current.txt"},
                        "status": "running",
                        "result": "",
                    }
                ),
                conversation_id=conv,
                message_meta={"turn_anchor_id": str(current_anchor_id)},
                created_at=now - timedelta(seconds=1),
            )
        )
        await db.commit()

    executed: list[str] = []

    async def fake_execute_tool(_name, args, **_kwargs):
        executed.append(args["path"])
        return f"read {args['path']}"

    async def fake_call_agent_llm(*_args, **_kwargs):
        return "current turn recovered"

    async def fake_deliver(**_kwargs):
        return True

    monkeypatch.setattr(turn_recovery, "execute_tool", fake_execute_tool, raising=False)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver)

    async with async_session() as db:
        anchor = await db.get(ChatMessage, current_anchor_id)

    assert await turn_recovery.resume_turn(anchor) is True
    assert executed == ["current.txt"]

    async with async_session() as db:
        tool_rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conv, ChatMessage.role == "tool_call")
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            )
            .scalars()
            .all()
        )
    done_rows = [row for row in tool_rows if json.loads(row.content)["status"] == "done"]
    assert len(done_rows) == 1
    assert json.loads(done_rows[0].content)["call_id"] == "current_call"
    assert done_rows[0].message_meta["turn_anchor_id"] == str(current_anchor_id)


async def test_resume_turn_reexecutes_running_tool_without_synthetic_recovery_message(monkeypatch):
    """Crash recovery should resume the original tool call and replay its real result."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    conv = f"unsafe_running_tool_{uuid.uuid4().hex}"
    call_id = "call_unsafe_running"
    async with async_session() as db:
        anchor = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="send this externally",
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
                        "name": "send_feishu_message",
                        "call_id": call_id,
                        "args": {"open_id": "ou_x", "text": "hello"},
                        "status": "running",
                        "result": "",
                    },
                    ensure_ascii=False,
                ),
                conversation_id=conv,
                created_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        await db.commit()

    executed = []

    async def fake_execute_tool(name, args, **kwargs):
        executed.append((name, args, kwargs))
        return "sent ok"

    captured = {}

    async def fake_call_agent_llm(*_args, **kwargs):
        captured.update(kwargs)
        return "continued after recovered send"

    async def fake_deliver(*_args, **_kwargs):
        return True

    monkeypatch.setattr(turn_recovery, "execute_tool", fake_execute_tool, raising=False)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert executed == [
        (
            "send_feishu_message",
            {"open_id": "ou_x", "text": "hello"},
            {
                "agent_id": agent_id,
                "user_id": user_id,
                "session_id": conv,
                "tool_call_id": call_id,
                "turn_anchor_id": anchor_id,
                "on_output": None,
            },
        )
    ]
    assert [msg["role"] for msg in captured["history"]] == ["user", "assistant", "tool"]
    assert captured["history"][1]["tool_calls"][0]["function"]["name"] == "send_feishu_message"
    assert captured["history"][1]["tool_calls"][0]["id"] == call_id
    assert captured["history"][2]["content"] == "sent ok"

    async with async_session() as db:
        payloads = [
            json.loads(row.content)
            for row in (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conv, ChatMessage.role == "tool_call")
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            )
            .scalars()
            .all()
        ]
    assert [payload["status"] for payload in payloads] == ["running", "done"]
    assert payloads[1]["call_id"] == call_id
    assert payloads[1]["result"] == "sent ok"


async def test_resume_turn_does_not_continue_when_recovered_tool_result_persist_fails(monkeypatch):
    """After re-executing a running tool, durable done persistence is mandatory."""
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

    posts = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {"ok": True}

    class FakeAsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            posts.append((url, kwargs))
            return FakeResponse()

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr("httpx.AsyncClient", FakeAsyncClient)

    await process_dingtalk_message(
        agent_id=agent_id,
        sender_staff_id=sender_staff_id,
        user_text="natural dingtalk user message",
        conversation_id="open-conv-1",
        conversation_type="1",
        session_webhook="https://example.invalid/dingtalk-webhook",
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
    assert any(payload["json"]["msgtype"] == "markdown" for _url, payload in posts)


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
            session_webhook="https://example.invalid/dingtalk-webhook",
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
