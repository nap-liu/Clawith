import uuid

import pytest
from fastapi import HTTPException

from app.api import files as files_api
from app.models.agent import Agent
from app.models.user import User
from app.services import workspace_collaboration
from app.services import workspace_locking
from app.services.storage_runtime import facade as storage_facade
from app.services.storage_runtime.local import LocalStorageBackend


class _FakeRedis:
    """Minimal in-memory redis covering the surface workspace_locking touches.

    ``delete_workspace_file`` acquires a distributed lock via
    ``workspace_locks`` -> ``acquire_workspace_lock`` (``set(..., ex=, nx=True)``)
    / ``release_workspace_lock`` (``eval(...)``). In the full-suite run the
    module-cached real redis client binds to an already-closed function-scoped
    event loop and raises ``RuntimeError: Event loop is closed``. A fresh fake
    per test sidesteps that entirely.
    """

    def __init__(self):
        self._data: dict[str, str] = {}

    async def set(self, key, value, *, ex=None, nx=False, **_kwargs):
        if nx and key in self._data:
            return None
        self._data[key] = value
        return True

    async def get(self, key):
        return self._data.get(key)

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if self._data.pop(key, None) is not None:
                removed += 1
        return removed

    async def eval(self, _script, _numkeys, key, owner):
        if self._data.get(key) == owner:
            return await self.delete(key)
        return 0


@pytest.fixture(autouse=True)
def _isolate_storage_and_lock(monkeypatch):
    # Reset the process-wide storage backend singleton so a backend configured
    # by an earlier test in the full suite cannot leak in (tests here point the
    # backend at their own tmp_path via monkeypatch).
    monkeypatch.setattr(storage_facade, "_storage_backend", None)

    # workspace_locking imports get_redis from app.core.events at module level;
    # patch it to a per-test in-memory fake so the distributed lock never uses a
    # real client cached against a dying event loop.
    fake_redis = _FakeRedis()

    async def _fake_get_redis():
        return fake_redis

    monkeypatch.setattr(workspace_locking, "get_redis", _fake_get_redis)


class _StubDb:
    """Minimal async DB stub for endpoints that only call ``commit``."""

    async def commit(self):
        return None


def make_user(**overrides):
    values = {
        "id": uuid.uuid4(),
        "display_name": "Alice",
        "role": "member",
        "tenant_id": uuid.uuid4(),
        "is_active": True,
    }
    values.update(overrides)
    return User(**values)


def make_agent(creator_id: uuid.UUID, **overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "Ops Bot",
        "role_description": "assistant",
        "creator_id": creator_id,
        "status": "idle",
        "agent_type": "native",
    }
    values.update(overrides)
    return Agent(**values)


@pytest.mark.asyncio
async def test_use_access_cannot_delete_agent_workspace_file(monkeypatch, tmp_path):
    user = make_user()
    agent = make_agent(uuid.uuid4(), tenant_id=user.tenant_id)
    workspace_file = tmp_path / str(agent.id) / "workspace" / "important.md"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_text("do not delete", encoding="utf-8")

    async def fake_check_agent_access(_db, _current_user, _agent_id):
        return agent, "use"

    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)

    with pytest.raises(HTTPException) as exc:
        await files_api.delete_file(
            agent_id=agent.id,
            path="workspace/important.md",
            current_user=user,
            db=object(),
        )

    assert exc.value.status_code == 403
    assert workspace_file.exists()


@pytest.mark.asyncio
async def test_manage_access_can_delete_agent_workspace_file(monkeypatch, tmp_path):
    user = make_user()
    agent = make_agent(user.id, tenant_id=user.tenant_id)
    workspace_file = tmp_path / str(agent.id) / "workspace" / "obsolete.md"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_text("delete me", encoding="utf-8")

    async def fake_check_agent_access(_db, _current_user, _agent_id):
        return agent, "manage"

    async def _noop_revision(*args, **kwargs):
        return None

    # delete_file now routes through workspace_collaboration.delete_workspace_file,
    # which resolves files via the storage backend. Point that backend at tmp_path
    # so the file the test wrote on disk is the one that gets deleted.
    storage = LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(files_api.settings, "AGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(files_api, "check_agent_access", fake_check_agent_access)
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    result = await files_api.delete_file(
        agent_id=agent.id,
        path="workspace/obsolete.md",
        current_user=user,
        db=_StubDb(),
    )

    assert result == {"status": "ok", "path": "workspace/obsolete.md"}
    assert not workspace_file.exists()
