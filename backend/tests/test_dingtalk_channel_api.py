import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import dingtalk as dingtalk_api
from app.models.channel_config import ChannelConfig


class DummyResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class RecordingDB:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.added = []
        self.deleted = []
        self.execute_count = 0
        self.flushed = False
        self.committed = False

    async def execute(self, _statement):
        self.execute_count += 1
        if self.responses:
            return self.responses.pop(0)
        return DummyResult()

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True
        for obj in self.added:
            if obj.id is None:
                obj.id = uuid.uuid4()
            if obj.created_at is None:
                obj.created_at = datetime.now(UTC)
            if obj.is_connected is None:
                obj.is_connected = False

    async def delete(self, obj):
        self.deleted.append(obj)


class FakeStreamManager:
    async def start_client(self, *_args, **_kwargs):
        return None

    async def stop_client(self, *_args, **_kwargs):
        return None


def make_user():
    return SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=uuid.uuid4())


def make_channel(agent_id: uuid.UUID) -> ChannelConfig:
    return ChannelConfig(
        id=uuid.uuid4(),
        agent_id=agent_id,
        channel_type="dingtalk",
        app_id="old-app-key",
        app_secret="old-secret",
        is_configured=True,
        is_connected=False,
        extra_config={"connection_mode": "websocket", "agent_id": "old-agent-id"},
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def capture_background_tasks(monkeypatch):
    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(
        "app.services.dingtalk_stream.dingtalk_stream_manager",
        FakeStreamManager(),
    )
    return scheduled


@pytest.mark.asyncio
async def test_manage_access_can_update_dingtalk_channel(monkeypatch, capture_background_tasks):
    agent_id = uuid.uuid4()
    config = make_channel(agent_id)
    db = RecordingDB([DummyResult(config)])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id, creator_id=uuid.uuid4()), "manage"

    monkeypatch.setattr(dingtalk_api, "check_agent_access", fake_check_agent_access)

    result = await dingtalk_api.configure_dingtalk_channel(
        agent_id=agent_id,
        data={
            "app_key": "new-app-key",
            "app_secret": "new-secret",
            "extra_config": {"connection_mode": "websocket", "agent_id": "new-agent-id"},
        },
        current_user=make_user(),
        db=db,
    )

    assert result.app_id == "new-app-key"
    assert result.extra_config == {"connection_mode": "websocket", "agent_id": "new-agent-id"}
    assert config.app_secret == "new-secret"
    assert db.flushed is True
    assert len(capture_background_tasks) == 1


@pytest.mark.asyncio
async def test_use_access_cannot_configure_dingtalk_channel(monkeypatch):
    db = RecordingDB()

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=_agent_id), "use"

    monkeypatch.setattr(dingtalk_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc:
        await dingtalk_api.configure_dingtalk_channel(
            agent_id=uuid.uuid4(),
            data={"app_key": "app-key", "app_secret": "secret"},
            current_user=make_user(),
            db=db,
        )

    assert exc.value.status_code == 403
    assert "manage" in exc.value.detail.lower()
    assert db.execute_count == 0


@pytest.mark.asyncio
async def test_new_dingtalk_channel_persists_optional_agent_id(monkeypatch, capture_background_tasks):
    agent_id = uuid.uuid4()
    db = RecordingDB([DummyResult()])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id), "manage"

    monkeypatch.setattr(dingtalk_api, "check_agent_access", fake_check_agent_access)

    result = await dingtalk_api.configure_dingtalk_channel(
        agent_id=agent_id,
        data={
            "app_key": "app-key",
            "app_secret": "secret",
            "extra_config": {"connection_mode": "websocket", "agent_id": "optional-agent-id"},
        },
        current_user=make_user(),
        db=db,
    )

    assert db.committed is True
    assert len(db.added) == 1
    assert db.added[0].extra_config == {
        "connection_mode": "websocket",
        "agent_id": "optional-agent-id",
    }
    assert result.extra_config == db.added[0].extra_config
    assert len(capture_background_tasks) == 1


@pytest.mark.asyncio
async def test_manage_access_can_delete_dingtalk_channel(monkeypatch, capture_background_tasks):
    agent_id = uuid.uuid4()
    config = make_channel(agent_id)
    db = RecordingDB([DummyResult(config)])

    async def fake_check_agent_access(_db, _user, _agent_id):
        return SimpleNamespace(id=agent_id), "manage"

    monkeypatch.setattr(dingtalk_api, "check_agent_access", fake_check_agent_access)

    await dingtalk_api.delete_dingtalk_channel(
        agent_id=agent_id,
        current_user=make_user(),
        db=db,
    )

    assert db.deleted == [config]
    assert len(capture_background_tasks) == 1
