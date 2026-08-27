"""Capacity and database-lifetime boundaries for startup turn recovery."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import turn_recovery
from app.services.workload_capacity import WorkloadKind

pytestmark = pytest.mark.asyncio


class _FakeDatabaseSession:
    def __init__(self, sessions: list[_FakeDatabaseSession], *, agent=None) -> None:
        self.active = False
        self.closed_before_exit = False
        self._agent = agent
        sessions.append(self)

    async def __aenter__(self):
        self.active = True
        return self

    async def __aexit__(self, *_args):
        self.active = False

    async def close(self) -> None:
        self.closed_before_exit = True
        self.active = False

    async def commit(self) -> None:
        return None

    async def execute(self, _query):
        return SimpleNamespace(scalar_one_or_none=lambda: self._agent)


class _FakeRedisLease:
    def __init__(self, resource: str, *, namespace: str) -> None:
        assert resource == turn_recovery.STARTUP_RECOVERY_LEASE_RESOURCE
        assert namespace == "turn-recovery"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


async def test_startup_scan_closes_database_before_resuming_turn(monkeypatch):
    """The cross-replica gate and recovered turns never retain the scan session."""

    sessions: list[_FakeDatabaseSession] = []
    anchor = SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(turn_recovery, "RedisLeaseLock", _FakeRedisLease)
    monkeypatch.setattr(
        turn_recovery,
        "async_session",
        lambda: _FakeDatabaseSession(sessions),
    )

    async def fake_load(db, *, limit):
        assert limit == 1
        assert db.active is True
        return [anchor]

    async def fake_resume(received):
        assert received is anchor
        assert sessions
        assert all(session.active is False for session in sessions)
        return True

    monkeypatch.setattr(turn_recovery, "_load_recoverable_anchors", fake_load)
    monkeypatch.setattr(turn_recovery, "resume_turn", fake_resume)

    stats = await turn_recovery.startup_turn_resume_once(limit=1)

    assert stats.scanned == 1
    assert stats.resumed == 1
    assert len(sessions) == 1


async def test_resume_turn_admits_background_work_without_holding_database(monkeypatch):
    """Recovery waits for capacity before opening any execution database session."""

    execution_agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    anchor = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=str(uuid.uuid4()),
    )
    origin = turn_recovery._RecoveryOrigin(False, None, None)
    agent = SimpleNamespace(
        id=execution_agent_id,
        tenant_id=tenant_id,
        context_window_size=8,
        agent_type="standard",
    )
    sessions: list[_FakeDatabaseSession] = []
    capacity_active = False
    capacity_calls: list[tuple[WorkloadKind, uuid.UUID]] = []

    monkeypatch.setattr(
        turn_recovery,
        "async_session",
        lambda: _FakeDatabaseSession(sessions, agent=agent),
    )

    async def fake_execution_agent(_db, _anchor):
        return execution_agent_id

    async def fake_origin(_anchor):
        return origin

    async def fake_origin_matches(_anchor, _origin):
        return True

    async def fake_pending(_db, _anchor, *, ctx_size):
        assert ctx_size == 8
        return False

    async def fake_complete(db, *_args, **kwargs):
        assert kwargs["release_db_before_execution"] is True
        await db.close()
        assert all(session.active is False for session in sessions)
        assert capacity_active is True
        return 0

    async def fake_history(*_args, **_kwargs):
        return [{"role": "user", "content": "recover"}]

    async def fake_llm(db, *_args, **kwargs):
        assert capacity_active is True
        assert kwargs["release_db_before_dispatch"] is True
        await db.close()
        assert all(session.active is False for session in sessions)
        return "recovered"

    async def fake_persist(*_args, **_kwargs):
        return None

    async def fake_deliver(*_args, **_kwargs):
        return True

    class _Capacity:
        @asynccontextmanager
        async def slot(self, kind, workload_tenant_id):
            nonlocal capacity_active
            assert all(session.active is False for session in sessions)
            capacity_calls.append((kind, workload_tenant_id))
            capacity_active = True
            try:
                yield
            finally:
                capacity_active = False

    monkeypatch.setattr(turn_recovery, "_load_fresh_recovery_origin", fake_origin)
    monkeypatch.setattr(turn_recovery, "_validated_execution_agent_id", fake_execution_agent)
    monkeypatch.setattr(turn_recovery, "_recovery_origin_matches", fake_origin_matches)
    monkeypatch.setattr(turn_recovery, "_tail_has_pending_confirmation", fake_pending)
    monkeypatch.setattr(turn_recovery, "_complete_unfinished_tool_calls", fake_complete)
    monkeypatch.setattr(turn_recovery, "load_recoverable_history_for_turn", fake_history)
    monkeypatch.setattr(turn_recovery, "_call_agent_llm", fake_llm)
    monkeypatch.setattr(turn_recovery, "persist_assistant_reply_row", fake_persist)
    monkeypatch.setattr(turn_recovery, "_deliver_recovered_reply", fake_deliver)
    monkeypatch.setattr(turn_recovery, "get_workload_capacity", _Capacity)

    assert await turn_recovery.resume_turn(anchor) is True
    assert capacity_calls == [(WorkloadKind.BACKGROUND, tenant_id)]
    assert capacity_active is False
    assert all(session.active is False for session in sessions)
