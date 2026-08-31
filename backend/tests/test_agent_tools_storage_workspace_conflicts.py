"""Conflict and protected-workspace cases for agent storage tools."""

import uuid

import pytest

from app.services import agent_tools, workspace_collaboration
from test_agent_tools_storage_workspace import (
    MemoryStorageBackend,
    _fake_workspace_lock_redis,
)


@pytest.mark.asyncio
async def test_write_workspace_file_fails_on_expected_version_conflict(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/test.md": b"old",
    })
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    version = await storage.get_version(f"{agent_id}/workspace/test.md")
    await storage.write_bytes(f"{agent_id}/workspace/test.md", b"remote-new")
    result = await workspace_collaboration.write_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/test.md",
        content="local-new",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
        expected_version_token=version.token,
    )

    assert result.ok is False
    assert "Conflict detected" in result.message
    assert storage.files[f"{agent_id}/workspace/test.md"] == b"remote-new"


@pytest.mark.asyncio
async def test_move_workspace_path_fails_when_source_changes(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/source.md": b"old",
    })
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    version = await storage.get_version(f"{agent_id}/workspace/source.md")
    await storage.write_bytes(f"{agent_id}/workspace/source.md", b"remote-new")
    result = await workspace_collaboration.move_workspace_path(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        source_path="workspace/source.md",
        destination_path="workspace/dest.md",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
        expected_source_version_token=version.token,
    )

    assert result.ok is False
    assert "Conflict detected" in result.message
    assert f"{agent_id}/workspace/dest.md" not in storage.files


@pytest.mark.asyncio
async def test_delete_workspace_directory_uses_prefix_existence(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/dir/a.txt": b"a",
        f"{agent_id}/workspace/dir/nested/b.txt": b"b",
    })
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    result = await workspace_collaboration.delete_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/dir",
        actor_type="user",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert result.ok is True
    assert f"{agent_id}/workspace/dir/a.txt" not in storage.files
    assert f"{agent_id}/workspace/dir/nested/b.txt" not in storage.files


@pytest.mark.asyncio
async def test_memory_index_is_an_ordinary_deletable_workspace_file(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    key = f"{agent_id}/memory/MEMORY_INDEX.md"
    storage = MemoryStorageBackend({key: b"agent-owned ordinary file"})
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    result = await workspace_collaboration.delete_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="memory/MEMORY_INDEX.md",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert result.ok is True
    assert key not in storage.files


@pytest.mark.asyncio
async def test_agent_workspace_mutations_reject_system_webhook_inbox(tmp_path):
    agent_id = uuid.uuid4()
    base_dir = tmp_path / str(agent_id)
    cases = (
        ("write_file", {"path": "webhook/t/e/payload.json", "content": "changed"}),
        ("edit_file", {"path": "webhook/t/e/payload.json", "old_string": "a", "new_string": "b"}),
        ("delete_file", {"path": "webhook/t/e/payload.json"}),
        (
            "move_file",
            {
                "source_path": "webhook/t/e/payload.json",
                "destination_path": "workspace/payload.json",
            },
        ),
    )

    for tool_name, arguments in cases:
        result = await agent_tools._execute_workspace_mutation(
            tool_name,
            arguments,
            agent_id=agent_id,
            base_dir=base_dir,
            session_id=None,
        )
        assert "read-only" in result


@pytest.mark.asyncio
async def test_agent_can_read_complete_webhook_inbox_payload(monkeypatch):
    agent_id = uuid.uuid4()
    rel_path = "webhook/1d5930c4-1d59-40c4-9bb4-b9532f2f6432/20260825/1787625600123_00000000000000000042/payload.json"
    payload = b'{"submission":"complete","score":100}\n'
    storage = MemoryStorageBackend({f"{agent_id}/{rel_path}": payload})
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    result = await agent_tools._storage_read_file(agent_id, rel_path)

    assert rel_path in result
    assert payload.decode().strip() in result
