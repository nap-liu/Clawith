"""MCP active-turn and chat behavior tests."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from tests.test_mcp_tools import (
    ChatMessage,
    ChatSession,
    _FAKE_REPLY,
    _ctx,
    _isolate_async_engine_between_tests,
    _issue_pat_for,
    _seed_agent,
    _seed_message,
    _seed_session,
    _seed_tenant,
    _seed_user,
    _unauth_ctx,
    async_session,
)


async def test_active_turn_tools_enforce_owner_write_scope_and_superadmin_all():
    from app.mcp_server.tools_turns import list_active_turns, stop_turn
    from app.models.audit import AuditLog
    from app.services.active_turns import ensure_active_turn, reset_active_turns_for_testing

    await reset_active_turns_for_testing()
    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id, name="TurnOwner")
    other = await _seed_user(tenant_id=tenant.id, name="OtherUser")
    admin = await _seed_user(tenant_id=tenant.id, role="platform_admin", name="SuperAdmin")
    identity_admin = await _seed_user(
        tenant_id=tenant.id,
        name="IdentitySuperAdmin",
        identity_platform_admin=True,
    )
    agent = await _seed_agent(owner.id, tenant_id=tenant.id, name="RunningAgent")
    owner_read = await _issue_pat_for(owner, scope="read")
    other_write = await _issue_pat_for(other, scope="write")
    admin_write = await _issue_pat_for(admin, scope="write")
    identity_admin_write = await _issue_pat_for(identity_admin, scope="write")
    ready = asyncio.Event()

    async def running_turn():
        record = await ensure_active_turn(
            owner_user_id=owner.id,
            agent_id=agent.id,
            session_id="oneshot:test",
            turn_type="oneshot",
            title="正在执行的任务",
        )
        ready.set()
        await asyncio.Event().wait()
        return record.turn_id

    worker = asyncio.create_task(running_turn())
    await ready.wait()
    owner_result = await list_active_turns(_ctx(owner_read))
    turn_id = owner_result.split("turn_id=", 1)[1].split(" ·", 1)[0]

    assert "正在执行的任务" in owner_result
    assert "RunningAgent" in owner_result
    assert await list_active_turns(_ctx(await _issue_pat_for(other))) == "（当前没有激活中的 turn）"
    assert "write 范围" in await stop_turn(_ctx(owner_read), turn_id)
    assert "找不到" in await stop_turn(_ctx(other_write), turn_id)
    assert not worker.done()

    admin_result = await list_active_turns(_ctx(admin_write))
    assert turn_id in admin_result
    assert turn_id in await list_active_turns(_ctx(identity_admin_write))
    assert "已终止" in await stop_turn(_ctx(admin_write), turn_id)
    with pytest.raises(asyncio.CancelledError):
        await worker
    async with async_session() as db:
        audit = await db.scalar(
            select(AuditLog).where(
                AuditLog.action == "mcp_turn_stop_requested",
                AuditLog.user_id == admin.id,
            )
        )
    assert audit is not None
    assert audit.details["turn_id"] == turn_id
    assert audit.details["platform_admin"] is True

    second_ready = asyncio.Event()

    async def second_running_turn():
        await ensure_active_turn(
            owner_user_id=owner.id,
            agent_id=agent.id,
            session_id="heartbeat:identity-admin-stop",
            turn_type="heartbeat",
        )
        second_ready.set()
        await asyncio.Event().wait()

    second_worker = asyncio.create_task(second_running_turn())
    await second_ready.wait()
    second_result = await list_active_turns(_ctx(identity_admin_write))
    second_turn_id = second_result.split("turn_id=", 1)[1].split(" ·", 1)[0]
    assert "已终止" in await stop_turn(
        _ctx(identity_admin_write),
        second_turn_id,
    )
    with pytest.raises(asyncio.CancelledError):
        await second_worker
    await reset_active_turns_for_testing()


async def test_stop_turn_does_not_cancel_when_durable_write_fails():
    from app.mcp_server.tools_turns import stop_turn
    from app.services.active_turns import (
        cancel_active_turn,
        ensure_active_turn,
        list_active_turns,
        reset_active_turns_for_testing,
    )

    await reset_active_turns_for_testing()
    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id, name="DurableStopOwner")
    agent = await _seed_agent(owner.id, tenant_id=tenant.id, name="DurableStopAgent")
    write_token = await _issue_pat_for(owner, scope="write")
    ready = asyncio.Event()

    async def running_turn():
        await ensure_active_turn(
            owner_user_id=owner.id,
            agent_id=agent.id,
            session_id=str(uuid.uuid4()),
            turn_type="web",
            turn_anchor_id=uuid.uuid4(),
        )
        ready.set()
        await asyncio.Event().wait()

    worker = asyncio.create_task(running_turn())
    await ready.wait()
    record = (await list_active_turns(owner_user_id=owner.id))[0]

    with patch(
        "app.mcp_server.tools_turns.mark_turn_cancelled",
        new=AsyncMock(side_effect=RuntimeError("database unavailable")),
    ):
        with pytest.raises(RuntimeError, match="database unavailable"):
            await stop_turn(_ctx(write_token), record.turn_id)

    assert not worker.done()
    await cancel_active_turn(record.turn_id, owner_user_id=owner.id)
    with pytest.raises(asyncio.CancelledError):
        await worker
    await reset_active_turns_for_testing()


async def test_stop_turn_request_cancellation_cannot_strand_reservation():
    from app.mcp_server import tools_turns
    from app.services.active_turns import (
        ensure_active_turn,
        list_active_turns,
        reset_active_turns_for_testing,
    )

    await reset_active_turns_for_testing()
    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id, name="CancelledStopOwner")
    agent = await _seed_agent(owner.id, tenant_id=tenant.id, name="CancelledStopAgent")
    write_token = await _issue_pat_for(owner, scope="write")
    registered = asyncio.Event()

    async def running_turn():
        await ensure_active_turn(
            owner_user_id=owner.id,
            agent_id=agent.id,
            session_id="request-cancel",
            turn_type="task",
        )
        registered.set()
        await asyncio.Event().wait()

    worker = asyncio.create_task(running_turn())
    await registered.wait()
    record = (await list_active_turns(owner_user_id=owner.id))[0]
    session_exit_started = asyncio.Event()
    allow_session_exit = asyncio.Event()
    real_session_factory = tools_turns.async_session
    factory_calls = 0

    class DelayedSessionExit:
        def __init__(self):
            self.inner = real_session_factory()

        async def __aenter__(self):
            return await self.inner.__aenter__()

        async def __aexit__(self, exc_type, exc, traceback):
            session_exit_started.set()
            await allow_session_exit.wait()
            return await self.inner.__aexit__(exc_type, exc, traceback)

    def controlled_session_factory():
        nonlocal factory_calls
        factory_calls += 1
        return real_session_factory() if factory_calls == 1 else DelayedSessionExit()

    with patch.object(tools_turns, "async_session", side_effect=controlled_session_factory):
        request = asyncio.create_task(
            tools_turns.stop_turn(_ctx(write_token), record.turn_id)
        )
        await session_exit_started.wait()
        request.cancel()
        await asyncio.sleep(0)
        assert record.stop_reserved is True
        allow_session_exit.set()
        with pytest.raises(asyncio.CancelledError):
            await request

    assert record.stop_reserved is False
    with pytest.raises(asyncio.CancelledError):
        await worker
    await reset_active_turns_for_testing()


async def test_stop_turn_freezes_terminal_reply_until_cancel_is_durable():
    from app.mcp_server.tools_turns import stop_turn
    from app.services.active_turns import (
        ensure_active_turn,
        list_active_turns,
        reset_active_turns_for_testing,
    )
    from app.services.chat_history import (
        mark_turn_cancelled as real_mark_turn_cancelled,
        persist_assistant_reply_row,
    )

    await reset_active_turns_for_testing()
    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id, name="StopRaceOwner")
    agent = await _seed_agent(owner.id, tenant_id=tenant.id, name="StopRaceAgent")
    write_token = await _issue_pat_for(owner, scope="write")
    session = await _seed_session(agent.id, owner.id, title="终止竞态")
    anchor_id = await _seed_message(
        agent.id,
        owner.id,
        session.id,
        "user",
        "不要让已终止 turn 写入完成回复",
    )
    registered = asyncio.Event()
    finalize_reply = asyncio.Event()

    async def running_turn():
        await ensure_active_turn(
            owner_user_id=owner.id,
            agent_id=agent.id,
            session_id=str(session.id),
            turn_type="web",
            turn_anchor_id=anchor_id,
        )
        registered.set()
        await finalize_reply.wait()
        async with async_session() as db:
            await persist_assistant_reply_row(
                db,
                agent_id=agent.id,
                user_id=owner.id,
                conversation_id=str(session.id),
                content="这个完成回复不应落库",
                turn_anchor_id=anchor_id,
            )
            await db.commit()

    worker = asyncio.create_task(running_turn())
    await registered.wait()
    record = (await list_active_turns(owner_user_id=owner.id))[0]
    durable_write_started = asyncio.Event()
    allow_durable_write = asyncio.Event()

    async def paused_mark(db, **kwargs):
        durable_write_started.set()
        await allow_durable_write.wait()
        return await real_mark_turn_cancelled(db, **kwargs)

    with patch("app.mcp_server.tools_turns.mark_turn_cancelled", side_effect=paused_mark):
        stop_task = asyncio.create_task(stop_turn(_ctx(write_token), record.turn_id))
        await durable_write_started.wait()
        finalize_reply.set()
        await asyncio.sleep(0)
        assert not worker.done()
        allow_durable_write.set()
        assert "已终止" in await stop_task

    with pytest.raises(asyncio.CancelledError):
        await worker
    async with async_session() as db:
        completed = await db.scalar(
            select(ChatMessage.id).where(
                ChatMessage.agent_id == agent.id,
                ChatMessage.conversation_id == str(session.id),
                ChatMessage.role == "assistant",
                ChatMessage.message_meta["turn_anchor_id"].as_string()
                == str(anchor_id),
                ChatMessage.message_meta["turn_status"].as_string()
                == "completed",
            )
        )
        anchor = await db.get(ChatMessage, anchor_id)
    assert completed is None
    assert anchor.message_meta["turn_status"] == "cancelled"
    await reset_active_turns_for_testing()


async def test_terminal_reply_that_holds_anchor_lock_wins_over_stop_turn():
    from app.mcp_server.tools_turns import stop_turn
    from app.services.active_turns import (
        ensure_active_turn,
        list_active_turns,
        reset_active_turns_for_testing,
    )
    from app.services.chat_history import (
        mark_turn_cancelled as real_mark_turn_cancelled,
        persist_assistant_reply_row,
    )

    await reset_active_turns_for_testing()
    tenant = await _seed_tenant()
    owner = await _seed_user(tenant_id=tenant.id, name="CompleteRaceOwner")
    agent = await _seed_agent(owner.id, tenant_id=tenant.id, name="CompleteRaceAgent")
    write_token = await _issue_pat_for(owner, scope="write")
    session = await _seed_session(agent.id, owner.id, title="完成竞态")
    anchor_id = await _seed_message(
        agent.id,
        owner.id,
        session.id,
        "user",
        "已经完成的 turn 不应被倒写为终止",
    )
    reply_flushed = asyncio.Event()
    allow_commit = asyncio.Event()
    release_worker = asyncio.Event()

    async def running_turn():
        await ensure_active_turn(
            owner_user_id=owner.id,
            agent_id=agent.id,
            session_id=str(session.id),
            turn_type="web",
            turn_anchor_id=anchor_id,
        )
        async with async_session() as db:
            await persist_assistant_reply_row(
                db,
                agent_id=agent.id,
                user_id=owner.id,
                conversation_id=str(session.id),
                content="这个完成回复已经获得锚点锁",
                turn_anchor_id=anchor_id,
            )
            reply_flushed.set()
            await allow_commit.wait()
            await db.commit()
        await release_worker.wait()

    worker = asyncio.create_task(running_turn())
    await reply_flushed.wait()
    record = (await list_active_turns(owner_user_id=owner.id))[0]
    mark_entered = asyncio.Event()

    async def observed_mark(db, **kwargs):
        mark_entered.set()
        return await real_mark_turn_cancelled(db, **kwargs)

    with patch("app.mcp_server.tools_turns.mark_turn_cancelled", side_effect=observed_mark):
        stop_task = asyncio.create_task(stop_turn(_ctx(write_token), record.turn_id))
        await mark_entered.wait()
        allow_commit.set()
        assert "已完成，无需终止" in await stop_task

    assert not worker.done()
    async with async_session() as db:
        anchor = await db.get(ChatMessage, anchor_id)
        completed = await db.scalar(
            select(ChatMessage.id).where(
                ChatMessage.agent_id == agent.id,
                ChatMessage.conversation_id == str(session.id),
                ChatMessage.role == "assistant",
                ChatMessage.message_meta["turn_anchor_id"].as_string()
                == str(anchor_id),
                ChatMessage.message_meta["turn_status"].as_string()
                == "completed",
            )
        )
    assert completed is not None
    assert (anchor.message_meta or {}).get("turn_status") != "cancelled"
    release_worker.set()
    await worker
    await reset_active_turns_for_testing()


async def test_chat_new_conversation_creates_new_session():
    """new_conversation=True always creates a fresh session."""
    from app.mcp_server.tools import chat_with_agent

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)
    agent = await _seed_agent(user.id, tenant_id=t.id, name="NewConvAgent")

    with patch("app.services.channel_llm._call_agent_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _FAKE_REPLY
        await chat_with_agent(_ctx(token), message="first", agent="NewConvAgent")
        await chat_with_agent(
            _ctx(token), message="second", agent="NewConvAgent", new_conversation=True
        )

    from sqlalchemy import select

    async with async_session() as db:
        sessions = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent.id,
                    ChatSession.user_id == user.id,
                    ChatSession.source_channel == "mcp",
                )
            )
        ).scalars().all()
    assert len(sessions) == 2, "new_conversation=True must create a new session"


async def test_chat_session_id_path_does_not_rewrite_user_id():
    """Jumping to an existing session (e.g. a feishu session) must NOT change user_id.

    This is the critical safety property: find_or_create_channel_session would
    re-attribute user_id on P2P sessions; the session_id path must use plain SELECT.
    """
    from app.mcp_server.tools import chat_with_agent

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    original_owner = await _seed_user(tenant_id=t.id)
    agent = await _seed_agent(user.id, tenant_id=t.id, name="JumpAgent")

    # Create a feishu session owned by original_owner (the creator of the agent here is user,
    # but the session is "owned" by original_owner as the chat participant).
    # We use the agent's creator as the session user so the MCP user (same tenant admin)
    # can access it via SCOPE_ALL (org_admin access).
    admin_user = await _seed_user(tenant_id=t.id, role="org_admin")
    admin_token = await _issue_pat_for(admin_user)

    feishu_sess = await _seed_session(
        agent.id, original_owner.id, channel="feishu", title="Feishu Convo"
    )
    original_user_id = feishu_sess.user_id

    with patch("app.services.channel_llm._call_agent_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = _FAKE_REPLY
        await chat_with_agent(
            _ctx(admin_token),
            message="hello",
            agent="JumpAgent",
            session_id=str(feishu_sess.id),
        )

    # Check that user_id was NOT changed
    from sqlalchemy import select

    async with async_session() as db:
        refreshed = (
            await db.execute(select(ChatSession).where(ChatSession.id == feishu_sess.id))
        ).scalar_one()
    assert refreshed.user_id == original_user_id, (
        "chat_with_agent session_id path must not rewrite user_id of the existing session"
    )


async def test_chat_persists_user_and_assistant_messages():
    """After one chat round, DB must have both role='user' and role='assistant' rows,
    with assistant.created_at >= user.created_at (ordering invariant)."""
    from app.mcp_server.tools import chat_with_agent

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)
    agent = await _seed_agent(user.id, tenant_id=t.id, name="PersistAgent")

    with patch("app.services.channel_llm._call_agent_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "I am the reply"
        await chat_with_agent(_ctx(token), message="ping", agent="PersistAgent")

    from sqlalchemy import select

    async with async_session() as db:
        sess = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == agent.id,
                    ChatSession.user_id == user.id,
                    ChatSession.source_channel == "mcp",
                )
            )
        ).scalars().first()
        assert sess is not None
        msgs = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id == str(sess.id),
                    ChatMessage.compacted_into.is_(None),
                )
            )
        ).scalars().all()

    roles = {m.role for m in msgs}
    assert "user" in roles, "Must persist a user message row"
    assert "assistant" in roles, "Must persist an assistant message row"

    user_msg = next(m for m in msgs if m.role == "user")
    asst_msg = next(m for m in msgs if m.role == "assistant")
    # assistant must be stamped at or after user (persist_assistant_reply opens its own session)
    assert asst_msg.created_at >= user_msg.created_at, (
        "assistant row created_at must be >= user row created_at"
    )


async def test_chat_error_reply_has_no_recovery_hint():
    """When LLM returns [Error] ..., the MCP reply must NOT contain the IM /new hint.

    This validates recovery_hint=None is wired through correctly.
    """
    from app.mcp_server.tools import chat_with_agent
    from app.services.channel_llm import _IM_LLM_RECOVERY_HINT

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)
    await _seed_agent(user.id, tenant_id=t.id, name="ErrorAgent")

    with patch("app.services.channel_llm._call_agent_llm", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "[Error] something went wrong"
        result = await chat_with_agent(_ctx(token), message="break", agent="ErrorAgent")

    assert "/new" not in result
    assert _IM_LLM_RECOVERY_HINT not in result
    assert result.startswith("[Error]")


async def test_chat_unauth():
    from app.mcp_server.tools import chat_with_agent, _UNAUTH

    result = await chat_with_agent(_unauth_ctx(), message="hello", agent="anyone")
    assert result == _UNAUTH


async def test_chat_no_agent_no_session_returns_error():
    """chat_with_agent with no agent and no session_id must return an error."""
    from app.mcp_server.tools import chat_with_agent

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)

    result = await chat_with_agent(_ctx(token), message="hello")
    assert "❌" in result


async def test_chat_unknown_agent_returns_error():
    """chat_with_agent with an agent name that doesn't exist → error."""
    from app.mcp_server.tools import chat_with_agent

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id)
    token = await _issue_pat_for(user)

    result = await chat_with_agent(_ctx(token), message="hello", agent="GhostAgent")
    assert "❌" in result


async def test_chat_session_id_deny_wrong_user():
    """chat_with_agent with session_id belonging to a different user → _DENY."""
    from app.mcp_server.tools import chat_with_agent, _DENY

    t = await _seed_tenant()
    user = await _seed_user(tenant_id=t.id, role="member")
    token = await _issue_pat_for(user)

    other_user = await _seed_user(tenant_id=t.id, role="member")
    agent = await _seed_agent(other_user.id, tenant_id=t.id, access_mode="company")

    # Session belonging to other_user
    other_sess = await _seed_session(agent.id, other_user.id, channel="mcp")

    result = await chat_with_agent(
        _ctx(token), message="hi", agent=agent.name, session_id=str(other_sess.id)
    )
    # user is a plain member on a company agent → SCOPE_OWN → can't access other user's session
    assert result == _DENY


# ── A2A two-sided permission (the subtlest get_session logic) ──────────────────


async def test_get_session_a2a_peer_side_manage_grants_access():
    """A2A session anchored on agent X (viewer has NO access) but peer is agent Y
    the viewer manages → _session_max_scope picks the peer side → read allowed."""
    from app.mcp_server.tools import get_session, _DENY

    tenant = await _seed_tenant()
    viewer = await _seed_user(tenant_id=tenant.id, name="Viewer")
    other = await _seed_user(tenant_id=tenant.id, name="Other")
    token = await _issue_pat_for(viewer)

    # Anchor X: created by other, private → viewer access = None.
    agent_x = await _seed_agent(other.id, tenant_id=tenant.id, access_mode="private", name="AnchorX")
    # Peer Y: created by viewer → viewer manages it.
    agent_y = await _seed_agent(viewer.id, tenant_id=tenant.id, access_mode="private", name="PeerY")

    sess = await _seed_session(agent_x.id, other.id, channel="agent", peer=agent_y.id)
    await _seed_message(agent_x.id, other.id, sess.id, "user", "a2a hello world")

    result = await get_session(_ctx(token), str(sess.id))
    assert result != _DENY, "manage on the peer-side agent must grant read access"
    assert "a2a hello world" in result


async def test_get_session_a2a_no_access_either_side_denied():
    """A2A session where the viewer manages neither side → uniform _DENY."""
    from app.mcp_server.tools import get_session, _DENY

    tenant = await _seed_tenant()
    viewer = await _seed_user(tenant_id=tenant.id, name="Viewer")
    other = await _seed_user(tenant_id=tenant.id, name="Other")
    token = await _issue_pat_for(viewer)

    agent_x = await _seed_agent(other.id, tenant_id=tenant.id, access_mode="private", name="X2")
    agent_y = await _seed_agent(other.id, tenant_id=tenant.id, access_mode="private", name="Y2")
    sess = await _seed_session(agent_x.id, other.id, channel="agent", peer=agent_y.id)
    await _seed_message(agent_x.id, other.id, sess.id, "user", "secret a2a")

    result = await get_session(_ctx(token), str(sess.id))
    assert result == _DENY


# ── chat_with_agent cross-agent session_id mismatch ───────────────────────────


async def test_chat_with_agent_cross_agent_session_id_mismatch(monkeypatch):
    """Explicit agent that doesn't match the session's agent → mismatch error,
    and the LLM is never invoked."""
    from app.mcp_server import tools

    tenant = await _seed_tenant()
    viewer = await _seed_user(tenant_id=tenant.id, name="V")
    creator = await _seed_user(tenant_id=tenant.id, name="C")
    token = await _issue_pat_for(viewer)

    # AgentX must exist & be visible so it resolves by name (then mismatches the session's agent).
    await _seed_agent(creator.id, tenant_id=tenant.id, access_mode="company", name="AgentX")
    agent_y = await _seed_agent(creator.id, tenant_id=tenant.id, access_mode="company", name="AgentY")
    # Session owned by Y that the viewer participated in (so scope passes before mismatch).
    sess_y = await _seed_session(agent_y.id, viewer.id, channel="web")

    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        return "must not be called"

    monkeypatch.setattr("app.services.channel_llm._call_agent_llm", _boom)

    result = await tools.chat_with_agent(
        _ctx(token), message="hi", agent="AgentX", session_id=str(sess_y.id)
    )
    assert "不一致" in result
    assert called["n"] == 0, "LLM must not run when session_id's agent != requested agent"
