"""Tests for startup turn recovery primitives."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, text

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


async def test_call_agent_llm_releases_database_before_provider_dispatch(monkeypatch):
    """Provider I/O runs without the ingress transaction or its locks."""
    import app.services.llm as llm_module
    from app.services.channel_llm import _call_agent_llm

    agent_id, user_id = await _make_agent_with_model(context_window_size=2)
    observed: dict[str, object] = {}

    # Pin the observer to a different pool connection before resolving the
    # channel runtime. It can then inspect the exact ingress backend while the
    # fake provider represents a long remote model call.
    async with async_session() as observer_db:
        await observer_db.execute(text("SELECT 1"))
        async with async_session() as db:
            ingress_pid = await db.scalar(text("SELECT pg_backend_pid()"))

            async def fake_failover(**_kwargs):
                observed["session_in_transaction"] = db.in_transaction()
                activity = (
                    await observer_db.execute(
                        text(
                            "SELECT state, xact_start FROM pg_stat_activity "
                            "WHERE pid = :pid"
                        ),
                        {"pid": ingress_pid},
                    )
                ).one()
                observed["state"] = activity.state
                observed["xact_start"] = activity.xact_start
                observed["dangerous_locks"] = await observer_db.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE pid = :pid "
                        "AND locktype IN ('transactionid', 'tuple', 'advisory')"
                    ),
                    {"pid": ingress_pid},
                )
                return "done"

            monkeypatch.setattr(llm_module, "call_llm_with_failover", fake_failover)
            reply = await _call_agent_llm(
                db,
                agent_id=agent_id,
                user_text="ordinary inbound message",
                session_id="",
                user_id=user_id,
                history=[{"role": "user", "content": "interrupted"}],
            )

    assert reply == "done"
    assert observed == {
        "session_in_transaction": False,
        "state": "idle",
        "xact_start": None,
        "dangerous_locks": 0,
    }


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


@pytest.mark.parametrize("injection_kind", ["subagent", "im"])
async def test_recovery_anchor_allows_different_sender_inside_same_execution_scope(
    monkeypatch,
    injection_kind,
):
    from app.services import conversation_turn_lifecycle
    from app.services.turn_recovery import _find_turn_anchor_for_latest

    root_id = uuid.uuid4()
    session_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    root = SimpleNamespace(
        id=root_id,
        role="user",
        agent_id=agent_id,
        conversation_id=str(session_id),
        user_id=uuid.uuid4(),
    )
    meta = (
        {"subagent_turn_anchor_id": str(root_id)}
        if injection_kind == "subagent"
        else {
            "turn_inbox_state": "delivered",
            "turn_inbox_anchor_id": str(root_id),
            "turn_inbox_generation": 7,
        }
    )
    projection = SimpleNamespace(
        id=uuid.uuid4(),
        role="user",
        agent_id=agent_id,
        conversation_id=str(session_id),
        user_id=uuid.uuid4(),
        message_meta=meta,
    )
    session = SimpleNamespace(id=session_id, agent_id=agent_id)

    class _DB:
        async def get(self, model, key):
            if model is ChatSession:
                return session
            if model is ChatMessage and key == root_id:
                return root
            return None

    monkeypatch.setattr(
        conversation_turn_lifecycle,
        "conversation_turn_snapshot_for_session",
        lambda _session: SimpleNamespace(anchor_id=root_id, generation=7),
    )

    selected = await _find_turn_anchor_for_latest(_DB(), projection)

    assert selected is root


@pytest.mark.parametrize("changed_attr", ["agent_id", "conversation_id"])
async def test_recovery_anchor_rejects_cross_execution_scope(changed_attr):
    from app.services.turn_recovery import _find_turn_anchor_for_latest

    root_id = uuid.uuid4()
    root = SimpleNamespace(
        id=root_id,
        role="user",
        agent_id=uuid.uuid4(),
        conversation_id=str(uuid.uuid4()),
        user_id=uuid.uuid4(),
    )
    projection = SimpleNamespace(
        id=uuid.uuid4(),
        role="user",
        agent_id=root.agent_id,
        conversation_id=root.conversation_id,
        user_id=uuid.uuid4(),
        message_meta={"subagent_turn_anchor_id": str(root_id)},
    )
    setattr(
        projection,
        changed_attr,
        uuid.uuid4() if changed_attr == "agent_id" else str(uuid.uuid4()),
    )

    class _DB:
        async def get(self, model, key):
            if model is ChatMessage and key == root_id:
                return root
            return None

    assert await _find_turn_anchor_for_latest(_DB(), projection) is None


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

    monkeypatch.setattr(turn_recovery, "resume_startup_anchor", fake_resume)

    stats = await turn_recovery.startup_turn_resume_once(limit=1)

    assert resumed == [first_anchor, second_anchor]
    assert stats.scanned == 2
    assert stats.resumed == 2

    resumed.clear()
    stats = await turn_recovery.startup_turn_resume_once(limit=2)

    assert resumed == [first_anchor, second_anchor]
    assert stats.scanned == 2
    assert stats.resumed == 2


async def test_startup_scan_does_not_drop_recoverable_tail_after_two_hundred_complete_conversations(
    monkeypatch,
):
    """Eligibility is decided before any count cap, including a busy two-hour window."""
    from app.services import turn_recovery

    agent_id, user_id = await _make_agent_with_model()
    now = datetime.now(UTC)
    async with async_session() as db:
        rows: list[ChatMessage] = []
        for index in range(205):
            conversation_id = f"complete-busy-window-{index}-{uuid.uuid4().hex}"
            rows.extend(
                [
                    ChatMessage(
                        agent_id=agent_id,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        role="user",
                        content="already handled",
                        created_at=now - timedelta(minutes=110) + timedelta(seconds=index * 2),
                    ),
                    ChatMessage(
                        agent_id=agent_id,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        role="assistant",
                        content="done",
                        created_at=now - timedelta(minutes=110) + timedelta(seconds=index * 2 + 1),
                    ),
                ]
            )
        target = ChatMessage(
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=f"recover-after-200-{uuid.uuid4().hex}",
            role="user",
            content="must still recover",
            created_at=now - timedelta(seconds=1),
        )
        rows.append(target)
        db.add_all(rows)
        await db.commit()
        target_id = target.id

    resumed: list[uuid.UUID] = []

    async def fake_resume(anchor):
        resumed.append(anchor.id)
        return True

    monkeypatch.setattr(turn_recovery, "resume_startup_anchor", fake_resume)
    stats = await turn_recovery.startup_turn_resume_once(limit=1)

    assert resumed == [target_id]
    assert stats.scanned == 1
    assert stats.resumed == 1


async def test_startup_scan_prefers_durable_owner_over_newer_queued_user(monkeypatch):
    """A later inbox row cannot hide the turn that owned the session at restart."""
    from app.services import turn_recovery
    from app.services.chat_history import persist_incoming_user_message
    from app.services.conversation_turn_lifecycle import transition_conversation_turn

    agent_id, user_id = await _make_agent_with_model()
    async with async_session() as db:
        session = ChatSession(
            agent_id=agent_id,
            user_id=user_id,
            title="durable recovery owner",
            source_channel="dingtalk",
            external_conv_id=f"dingtalk_group_{uuid.uuid4().hex}",
        )
        db.add(session)
        await db.flush()
        owner = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=str(session.id),
            content="running before restart",
        )
        await transition_conversation_turn(
            db,
            agent_id=agent_id,
            conversation_id=str(session.id),
            turn_anchor_id=owner.id,
            status="running",
        )
        queued = await persist_incoming_user_message(
            db,
            agent_id=agent_id,
            user_id=user_id,
            conversation_id=str(session.id),
            content="arrived while owner was running",
            message_meta={
                "turn_inbox_state": "pending",
                "turn_inbox_anchor_id": str(owner.id),
                "turn_inbox_generation": 1,
                "turn_inbox_mode": "current_turn",
            },
        )
        owner_id = owner.id
        queued_id = queued.id
        await db.commit()

    resumed: list[uuid.UUID] = []

    async def fake_resume(anchor):
        resumed.append(anchor.id)
        return True

    monkeypatch.setattr(turn_recovery, "resume_startup_anchor", fake_resume)
    stats = await turn_recovery.startup_turn_resume_once(limit=1)

    assert resumed == [owner_id]
    assert queued_id not in resumed
    assert stats.scanned == 1
    assert stats.resumed == 1


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

    async def fake_deliver(*, agent_id, conversation_id, reply, message_id):
        assert message_id is not None
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

    monkeypatch.setattr(turn_recovery, "resume_startup_anchor", fail_if_resumed)

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

    async def fake_load(_db):
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
    monkeypatch.setattr(turn_recovery, "resume_startup_anchor", fake_resume)

    scanner = asyncio.create_task(turn_recovery.startup_turn_resume_once(limit=2))
    await first_ready.wait()
    records = await list_active_turns(owner_user_id=owner_id)
    record = next(item for item in records if item.session_id == str(anchors[0].id))
    await cancel_active_turn(record.turn_id, owner_user_id=owner_id)

    stats = await scanner
    assert set(resumed_ids) == {anchors[0].id, anchors[1].id}
    assert stats.scanned == 2
    assert stats.skipped == 1
    assert stats.resumed == 1
    await reset_active_turns_for_testing()


async def test_startup_recovery_starts_every_eligible_anchor_in_parallel(monkeypatch):
    """Recovery adds no batching or concurrency gate beyond normal admission."""
    from types import SimpleNamespace

    from app.services import turn_recovery

    anchors = [
        SimpleNamespace(id=uuid.uuid4())
        for _ in range(4)
    ]
    first_wave_ready = asyncio.Event()
    release = asyncio.Event()
    in_flight = 0
    peak_in_flight = 0

    async def fake_load(_db):
        return anchors

    async def fake_resume(_anchor):
        nonlocal in_flight, peak_in_flight
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        if in_flight == len(anchors):
            first_wave_ready.set()
        try:
            await release.wait()
            return True
        finally:
            in_flight -= 1

    monkeypatch.setattr(turn_recovery, "_load_recoverable_anchors", fake_load)
    monkeypatch.setattr(turn_recovery, "resume_startup_anchor", fake_resume)

    scanner = asyncio.create_task(
        turn_recovery.startup_turn_resume_once(limit=len(anchors))
    )
    await asyncio.wait_for(first_wave_ready.wait(), timeout=1)
    assert peak_in_flight == len(anchors)
    release.set()

    stats = await asyncio.wait_for(scanner, timeout=1)
    assert stats.scanned == 4
    assert stats.resumed == 4
    assert stats.skipped == 0
    assert stats.failed == 0
    assert peak_in_flight == len(anchors)


async def test_startup_recovery_failure_does_not_cancel_siblings(monkeypatch):
    """One recovery exception is isolated and the rest of the batch completes."""
    from types import SimpleNamespace

    from app.services import turn_recovery

    anchors = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]
    successful_anchor = asyncio.Event()

    async def fake_load(_db):
        return anchors

    async def fake_resume(anchor):
        if anchor is anchors[0]:
            raise RuntimeError("isolated recovery failure")
        successful_anchor.set()
        return True

    monkeypatch.setattr(turn_recovery, "_load_recoverable_anchors", fake_load)
    monkeypatch.setattr(turn_recovery, "resume_startup_anchor", fake_resume)

    stats = await turn_recovery.startup_turn_resume_once(limit=2)
    assert successful_anchor.is_set()
    assert stats.resumed == 1
    assert stats.failed == 1
    assert stats.skipped == 0
