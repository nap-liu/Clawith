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
from app.models.channel_config import ChannelConfig
from app.models.chat_session import ChatSession
from app.models.identity import IdentityProvider
from app.models.llm import LLMModel
from app.models.org import OrgMember
from app.models.tenant import Tenant
from app.models.user import Identity, User
from app.models.audit import ChatMessage
from app.models.participant import Participant  # noqa: F401


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


async def test_call_agent_llm_recovery_mode_precompacts_with_recoverable_reload(monkeypatch):
    """Recovery preflight compaction must reload through the recoverable turn projection."""
    import app.services.llm as llm_module
    from app.services.channel_llm import _call_agent_llm

    agent_id, user_id = await _make_agent_with_model(context_window_size=2)
    anchor_id = uuid.uuid4()
    captured: dict = {}

    async def fake_failover(**kwargs):
        captured["messages"] = kwargs["messages"]
        return "done"

    async def fake_precompact(**_kwargs):
        return True

    async def fake_recoverable_reload(*_args, **kwargs):
        captured["reload_kwargs"] = kwargs
        return [{"role": "user", "content": "reloaded interrupted question"}]

    monkeypatch.setattr(llm_module, "call_llm_with_failover", fake_failover)
    monkeypatch.setattr("app.services.llm.compactor.maybe_precompact_prompt", fake_precompact)
    monkeypatch.setattr("app.services.chat_history.load_recoverable_history_for_turn", fake_recoverable_reload)

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
    assert captured["messages"] == [{"role": "user", "content": "reloaded interrupted question"}]
    assert captured["reload_kwargs"]["turn_anchor_id"] == anchor_id


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
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conv)
                .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            )
        ).scalars().all()
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[-1].content == "markerless recovered"


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


async def test_startup_scan_redelivers_recent_dingtalk_assistant_tail(monkeypatch):
    """A recent DingTalk assistant tail may represent crash after DB write before IM delivery."""
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
                    content="persisted before delivery",
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                ),
            ]
        )
        await db.commit()

    async def fail_if_llm_called(*_args, **_kwargs):
        raise AssertionError("assistant-tail recovery must redeliver without rerunning LLM")

    delivered = []

    async def fake_deliver(*, agent_id, conversation_id, reply):
        delivered.append((agent_id, conversation_id, reply))
        return True

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fail_if_llm_called)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver)

    stats = await turn_recovery.startup_turn_resume_once(limit=10)

    assert stats.scanned == 1
    assert stats.resumed == 1
    assert delivered == [(agent_id, conv, "persisted before delivery")]


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
    assert captured["history"][-1] == {"role": "user", "content": "interrupted"}
    async with async_session() as db:
        replies = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv,
                    ChatMessage.role == "assistant",
                    ChatMessage.content == "resumed reply",
                )
            )
        ).scalars().all()
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

    async def fake_deliver(*, agent_id, conversation_id, reply):
        delivered.append((agent_id, conversation_id, reply))
        return True

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_call_agent_llm)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert len(delivered) == 1
    delivered_agent_id, delivered_conv, delivered_reply = delivered[0]
    assert delivered_agent_id == agent_id
    assert delivered_conv == conv
    assert delivered_reply == "dingtalk resumed reply"


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


async def test_resume_turn_delivers_existing_assistant_before_completing(monkeypatch):
    """If a crash left reply persisted but anchor processing, recovery must send that reply."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_assistant_reply_row, persist_incoming_user_message

    agent_id, user_id = await _make_agent_with_model(context_window_size=4)
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="DingTalk",
            source_channel="dingtalk",
            external_conv_id="dingtalk_p2p_staff-2",
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

    async with async_session() as db:
        await persist_assistant_reply_row(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=conv,
            content="already persisted reply",
        )
        await db.commit()

    async def fail_if_llm_called(*_args, **_kwargs):
        raise AssertionError("existing assistant reply should be delivered without rerunning LLM")

    delivered = []

    async def fake_deliver(*, agent_id, conversation_id, reply):
        delivered.append((agent_id, conversation_id, reply))
        return True

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fail_if_llm_called)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", fake_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    result = await turn_recovery.resume_turn(anchor)

    assert result is True
    assert len(delivered) == 1
    delivered_agent_id, delivered_conv, delivered_reply = delivered[0]
    assert delivered_agent_id == agent_id
    assert delivered_conv == conv
    assert delivered_reply == "already persisted reply"


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
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv,
                    ChatMessage.role == "assistant",
                    ChatMessage.content == "final reply after tool",
                )
            )
        ).scalars().all()
    assert len(replies) == 1


async def test_resume_turn_executes_unfinished_running_tool_call_before_continuing(monkeypatch):
    """Crash after a running tool marker should finish that tool before LLM continuation."""
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
                        "call_id": call_id,
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

    executed = []

    async def fake_execute_tool(name, args, **kwargs):
        executed.append((name, args, kwargs))
        return "file body"

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
            "read_file",
            {"path": "a.txt"},
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
    assert captured["history"][1]["tool_calls"][0]["function"]["name"] == "read_file"
    assert captured["history"][1]["tool_calls"][0]["id"] == call_id
    assert captured["history"][2]["content"] == "file body"

    async with async_session() as db:
        payloads = [
            json.loads(row.content)
            for row in (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == conv, ChatMessage.role == "tool_call")
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
            ).scalars().all()
        ]
    assert [payload["status"] for payload in payloads] == ["running", "done"]
    assert payloads[1]["call_id"] == call_id
    assert payloads[1]["result"] == "file body"
    async with async_session() as db:
        replies = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv,
                    ChatMessage.role == "assistant",
                    ChatMessage.content == "reply after recovered tool",
                )
            )
        ).scalars().all()
    assert len(replies) == 1


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
            ).scalars().all()
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
            await db.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv, ChatMessage.role == "tool_call"))
        ).scalars().all()

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
    assert captured["history"] == [{"role": "user", "content": "old interrupted"}]


async def test_resume_turn_retries_existing_assistant_delivery_without_rerunning_llm(monkeypatch):
    """Crash/failure after final reply persistence should retry delivery only."""
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

    delivery_results = [False, True]
    deliveries = []

    async def flaky_deliver(*, agent_id, conversation_id, reply):
        deliveries.append((agent_id, conversation_id, reply))
        return delivery_results.pop(0)

    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fail_if_llm_called)
    monkeypatch.setattr(turn_recovery, "deliver_recovered_reply_to_origin", flaky_deliver, raising=False)

    async with async_session() as db:
        anchor = (await db.execute(select(ChatMessage).where(ChatMessage.id == anchor_id))).scalar_one()

    first = await turn_recovery.resume_turn(anchor)

    assert first is False

    second = await turn_recovery.resume_turn(anchor)

    assert second is True
    assert len(deliveries) == 2
    assert all(delivery[2] == "reply persisted before crash" for delivery in deliveries)
    async with async_session() as db:
        assistant_rows = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == conv,
                    ChatMessage.role == "assistant",
                    ChatMessage.content == "reply persisted before crash",
                )
            )
        ).scalars().all()
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
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == str(session.id))
                .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
            )
        ).scalars().all()

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
            await db.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv, ChatMessage.role == "assistant"))
        ).scalars().all()
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
            await db.execute(select(ChatMessage).where(ChatMessage.conversation_id == conv, ChatMessage.role == "tool_call"))
        ).scalars().all()
    assert len(pending_rows) == 1
    assert json.loads(pending_rows[0].content)["status"] == "pending"
