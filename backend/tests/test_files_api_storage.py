import uuid
from types import SimpleNamespace

import pytest

from app.api import files
from app.services.agent_manager import AgentManager
from app.services.storage_runtime.base import StorageBackend, StorageEntry, StorageVersion


class PrefixOnlyStorage(StorageBackend):
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def is_file(self, key: str) -> bool:
        return key in self.objects

    async def is_dir(self, key: str) -> bool:
        prefix = key.rstrip("/") + "/"
        return any(existing.startswith(prefix) for existing in self.objects)

    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = key.rstrip("/") + "/"
        entries_by_name: dict[str, StorageEntry] = {}
        for existing, data in self.objects.items():
            if not existing.startswith(prefix):
                continue
            rest = existing.removeprefix(prefix)
            name, _, tail = rest.partition("/")
            entries_by_name[name] = StorageEntry(
                name=name,
                key=f"{prefix}{name}",
                is_dir=bool(tail),
                size=0 if tail else len(data),
            )
        return sorted(entries_by_name.values(), key=lambda entry: (not entry.is_dir, entry.name))

    async def read_bytes(self, key: str) -> bytes:
        return self.objects[key]

    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = data

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def delete_tree(self, key: str) -> None:
        prefix = key.rstrip("/") + "/"
        for existing in list(self.objects):
            if existing.startswith(prefix):
                self.objects.pop(existing, None)

    async def stat(self, key: str) -> StorageEntry:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return StorageEntry(name=key.rsplit("/", 1)[-1], key=key, is_dir=False, size=len(self.objects[key]))

    async def get_version(self, key: str) -> StorageVersion:
        if key not in self.objects:
            return StorageVersion(key=key, exists=False, is_dir=False)
        token = f"v:{len(self.objects[key])}"
        return StorageVersion(
            key=key,
            exists=True,
            is_dir=False,
            size=len(self.objects[key]),
            version_id=token,
            etag=token,
            content_hash=token,
        )


def _make_access_user_and_check(agent_id):
    """Build a (user, check_agent_access) pair matching the (Agent, access) contract.

    list_files / read_file unpack ``agent, _access = await check_agent_access(...)``
    and then compare ``agent.creator_id == current_user.id`` for the CREATOR_ONLY
    filter, so the mock must return a 2-tuple and the user must carry an id/role.
    """
    user = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=None)
    agent = SimpleNamespace(id=agent_id, creator_id=uuid.uuid4())

    async def allow_access(*args, **kwargs):
        return agent, "manage"

    return user, allow_access


@pytest.mark.asyncio
async def test_list_files_accepts_s3_prefix_directory(monkeypatch):
    agent_id = uuid.uuid4()
    # Use a regular file: focus.md / agenda.md are intentionally hidden from the
    # root file tree (focus is DB-backed) in both upstream and our code, so they
    # would never surface here.
    storage = PrefixOnlyStorage({f"{agent_id}/notes.md": b"# Notes\n"})
    monkeypatch.setattr(files, "get_storage_backend", lambda: storage)

    user, allow_access = _make_access_user_and_check(agent_id)
    monkeypatch.setattr(files, "check_agent_access", allow_access)

    result = await files.list_files(agent_id, path="", current_user=user, db=None)

    assert [item.name for item in result] == ["notes.md"]
    assert result[0].path == "notes.md"
    # list_dir entries carry no version_id/etag/content_hash and an empty
    # modified_at, so _entry_version_token falls back to f"{modified_at}:{size}"
    # — i.e. ":8" (8 = len(b"# Notes\n")). No "v" prefix exists in that path.
    assert result[0].version_token == ":8"


@pytest.mark.asyncio
async def test_list_files_allows_empty_agent_root(monkeypatch):
    agent_id = uuid.uuid4()
    monkeypatch.setattr(files, "get_storage_backend", lambda: PrefixOnlyStorage())

    user, allow_access = _make_access_user_and_check(agent_id)
    monkeypatch.setattr(files, "check_agent_access", allow_access)

    assert await files.list_files(agent_id, path="", current_user=user, db=None) == []


@pytest.mark.asyncio
async def test_read_file_returns_version_token(monkeypatch):
    agent_id = uuid.uuid4()
    # focus.md is special-cased (410 GONE, DB-backed) on read_file; use a regular file.
    storage = PrefixOnlyStorage({f"{agent_id}/notes.md": b"# Notes\n"})
    monkeypatch.setattr(files, "get_storage_backend", lambda: storage)

    user, allow_access = _make_access_user_and_check(agent_id)
    monkeypatch.setattr(files, "check_agent_access", allow_access)

    result = await files.read_file(agent_id, path="notes.md", current_user=user, db=None)

    assert result.version_token == "v:8"


@pytest.mark.asyncio
async def test_agent_manager_does_not_reinitialize_s3_prefix_directory(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = PrefixOnlyStorage({f"{agent_id}/soul.md": b"existing"})
    monkeypatch.setattr("app.services.agent_manager.get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.agent_manager.settings.STORAGE_LOCAL_ROOT", str(tmp_path))

    manager = AgentManager()
    agent = SimpleNamespace(id=agent_id)

    await manager.initialize_agent_files(db=None, agent=agent)

    assert storage.objects[f"{agent_id}/soul.md"] == b"existing"


@pytest.mark.asyncio
async def test_agent_manager_materializes_s3_prefix_directory(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = PrefixOnlyStorage({
        f"{agent_id}/soul.md": b"# Soul\n",
        f"{agent_id}/memory/memory.md": b"# Memory\n",
    })
    monkeypatch.setattr("app.services.agent_manager.get_storage_backend", lambda: storage)
    monkeypatch.setattr("app.services.agent_manager.settings.STORAGE_LOCAL_ROOT", str(tmp_path))

    manager = AgentManager()

    agent_dir = await manager._materialize_agent_dir(agent_id)

    assert (agent_dir / "soul.md").read_text(encoding="utf-8") == "# Soul\n"
    assert (agent_dir / "memory" / "memory.md").read_text(encoding="utf-8") == "# Memory\n"
