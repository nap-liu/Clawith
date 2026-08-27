"""Ordinary Web history must not discover project-scoped conversations."""

import uuid
from types import SimpleNamespace

import pytest

from app.api import activity


class _Rows:
    def scalars(self):
        return self

    def all(self):
        return []


class _RecordingDB:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows()


@pytest.mark.asyncio
async def test_ordinary_activity_history_excludes_project_sessions(monkeypatch):
    agent_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), role="org_admin")
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=tenant_id,
        creator_id=uuid.uuid4(),
    )
    db = _RecordingDB()

    async def _check_access(_db, _user, _agent_id):
        return agent, "manage"

    async def _filter_tenant(_db, sessions, _tenant_id):
        return sessions

    monkeypatch.setattr(activity, "check_agent_access", _check_access)
    monkeypatch.setattr(activity, "filter_tenant_safe_chat_sessions", _filter_tenant)
    monkeypatch.setattr(activity, "can_view_all_agent_chat_sessions", lambda *_args: True)

    conversations = await activity.list_conversations(
        agent_id=agent_id,
        current_user=user,
        db=db,
    )

    assert conversations == []
    assert len(db.statements) == 1
    assert "chat_sessions.project_id IS NULL" in str(db.statements[0])
