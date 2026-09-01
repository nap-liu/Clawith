import asyncio
import io
import uuid

import pytest
from starlette.datastructures import UploadFile

import app.services.redis_lease_lock as redis_lease_lock_module
from app.api import files as files_api
from app.services import agent_tools, workspace_collaboration, workspace_locking
from app.services.storage_runtime import agent_files
from app.services.storage_runtime.base import (
    ConditionalWriteResult,
    StorageBackend,
    StorageEntry,
    StorageVersion,
    WriteCondition,
)
from app.services.storage_runtime.local import LocalStorageBackend


class _FakeRedis:
    """Minimal in-memory Redis for the shared renewable workspace lease."""

    def __init__(self):
        self._data: dict[str, str] = {}

    async def set(self, key, value, *, ex=None, px=None, nx=False, **_kwargs):
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

    async def eval(self, script, _numkeys, key, owner, *_args):
        if "pexpire" in script:
            return int(self._data.get(key) == owner)
        # Mirror the release-if-owner Lua: delete only when the caller owns it.
        if self._data.get(key) == owner:
            return await self.delete(key)
        return 0


@pytest.fixture(autouse=True)
def _fake_workspace_lock_redis(monkeypatch):
    fake_redis = _FakeRedis()

    async def _fake_get_redis():
        return fake_redis

    monkeypatch.setattr(redis_lease_lock_module, "get_redis", _fake_get_redis)


@pytest.mark.asyncio
async def test_workspace_write_queue_serializes_different_paths():
    agent_id = uuid.uuid4()
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()
    order: list[str] = []

    async def owner():
        async with workspace_locking.workspace_locks(agent_id, ["workspace"]):
            order.append("owner")
            owner_entered.set()
            await release_owner.wait()

    async def waiter():
        await owner_entered.wait()
        async with workspace_locking.workspace_locks(
            agent_id,
            ["HEARTBEAT.md"],
            acquire_timeout_seconds=0.2,
        ):
            order.append("waiter")

    owner_task = asyncio.create_task(owner())
    waiter_task = asyncio.create_task(waiter())
    await owner_entered.wait()
    await asyncio.sleep(0.02)
    assert order == ["owner"]

    release_owner.set()
    await asyncio.gather(owner_task, waiter_task)
    assert order == ["owner", "waiter"]


@pytest.mark.asyncio
async def test_workspace_write_queue_is_reentrant_for_one_operation():
    agent_id = uuid.uuid4()
    async with (
        workspace_locking.workspace_locks(agent_id, ["workspace"]),
        workspace_locking.workspace_locks(agent_id, ["workspace/report.md"]),
    ):
        pass


@pytest.mark.asyncio
async def test_workspace_write_queue_timeout_is_actionable():
    agent_id = uuid.uuid4()
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner():
        async with workspace_locking.workspace_locks(agent_id, ["workspace"]):
            owner_entered.set()
            await release_owner.wait()

    owner_task = asyncio.create_task(owner())
    await owner_entered.wait()
    try:
        with pytest.raises(
            workspace_locking.WorkspaceLockTimeoutError,
            match="retry later or continue with a task that does not modify the workspace",
        ):
            async with workspace_locking.workspace_locks(
                agent_id,
                ["memory/memory.md"],
                acquire_timeout_seconds=0.02,
            ):
                pass
    finally:
        release_owner.set()
        await owner_task


@pytest.mark.asyncio
async def test_agent_storage_helper_uses_shared_workspace_write_queue(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    monkeypatch.setattr(agent_files, "get_storage_backend", lambda: storage)
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner():
        async with workspace_locking.workspace_locks(agent_id, ["workspace"]):
            owner_entered.set()
            await release_owner.wait()

    owner_task = asyncio.create_task(owner())
    await owner_entered.wait()
    write_task = asyncio.create_task(agent_files.store_agent_bytes(agent_id, "workspace/report.txt", b"queued"))
    await asyncio.sleep(0.02)
    assert not await storage.exists(f"{agent_id}/workspace/report.txt")

    release_owner.set()
    await asyncio.gather(owner_task, write_task)
    assert await storage.read_bytes(f"{agent_id}/workspace/report.txt") == b"queued"


@pytest.mark.asyncio
async def test_binary_upload_uses_shared_workspace_write_queue(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    monkeypatch.setattr(files_api, "get_storage_backend", lambda: storage)

    async def allow_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(files_api, "check_agent_access", allow_access)
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner():
        async with workspace_locking.workspace_locks(agent_id, ["workspace"]):
            owner_entered.set()
            await release_owner.wait()

    owner_task = asyncio.create_task(owner())
    await owner_entered.wait()
    upload_task = asyncio.create_task(
        files_api.upload_file_to_workspace(
            agent_id=agent_id,
            file=UploadFile(io.BytesIO(b"queued"), filename="report.txt"),
            path="workspace/uploads",
            current_user=object(),
            db=object(),
            _workspace_agent=object(),
        )
    )
    await asyncio.sleep(0.02)
    assert not await storage.exists(f"{agent_id}/workspace/uploads/report.txt")

    release_owner.set()
    await asyncio.gather(owner_task, upload_task)
    assert await storage.read_bytes(f"{agent_id}/workspace/uploads/report.txt") == b"queued"


class MemoryStorageBackend(StorageBackend):
    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})
        self.versions = {key: 1 for key in self.files}

    async def exists(self, key: str) -> bool:
        return key in self.files

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def is_dir(self, key: str) -> bool:
        prefix = key.rstrip("/") + "/"
        return any(existing.startswith(prefix) for existing in self.files)

    async def list_dir(self, key: str) -> list[StorageEntry]:
        prefix = key.rstrip("/") + "/"
        entries: dict[str, StorageEntry] = {}
        for existing, data in self.files.items():
            if not existing.startswith(prefix):
                continue
            rest = existing.removeprefix(prefix)
            name, _, tail = rest.partition("/")
            entries[name] = StorageEntry(
                name=name,
                key=f"{prefix}{name}",
                is_dir=bool(tail),
                size=0 if tail else len(data),
            )
        return sorted(entries.values(), key=lambda entry: (not entry.is_dir, entry.name))

    async def read_bytes(self, key: str) -> bytes:
        return self.files[key]

    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.files[key] = data
        self.versions[key] = self.versions.get(key, 0) + 1

    async def delete(self, key: str) -> None:
        self.files.pop(key, None)
        self.versions.pop(key, None)

    async def delete_tree(self, key: str) -> None:
        prefix = key.rstrip("/") + "/"
        for existing in list(self.files):
            if existing.startswith(prefix):
                self.files.pop(existing)
                self.versions.pop(existing, None)

    async def stat(self, key: str) -> StorageEntry:
        return StorageEntry(name=key.rsplit("/", 1)[-1], key=key, is_dir=False, size=len(self.files[key]))

    async def get_version(self, key: str) -> StorageVersion:
        if key not in self.files:
            return StorageVersion(key=key, exists=False, is_dir=False)
        version = str(self.versions.get(key, 0))
        return StorageVersion(
            key=key,
            exists=True,
            is_dir=False,
            size=len(self.files[key]),
            version_id=version,
            etag=version,
            content_hash=version,
        )

    async def write_bytes_if_match(
        self,
        key: str,
        data: bytes,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        current = await self.get_version(key)
        if condition:
            if condition.require_absent and current.exists:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
            if condition.version_token is not None and current.token != condition.version_token:
                return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        await self.write_bytes(key, data, content_type=content_type)
        return ConditionalWriteResult(ok=True, current_version=await self.get_version(key))


class FailingStorageBackend(MemoryStorageBackend):
    def __init__(self, *, fail_operation: str, files: dict[str, bytes] | None = None):
        super().__init__(files)
        self.fail_operation = fail_operation

    async def exists(self, key: str) -> bool:
        if self.fail_operation == "exists":
            raise ConnectionError("storage unavailable")
        return await super().exists(key)

    async def is_file(self, key: str) -> bool:
        if self.fail_operation == "is_file":
            raise ConnectionError("storage unavailable")
        return await super().is_file(key)

    async def read_bytes(self, key: str) -> bytes:
        if self.fail_operation == "read_bytes":
            raise OSError("object read interrupted")
        return await super().read_bytes(key)

    async def read_text(
        self,
        key: str,
        encoding: str = "utf-8",
        errors: str = "strict",
    ) -> str:
        if self.fail_operation == "read_text":
            raise OSError("object read interrupted")
        return (await self.read_bytes(key)).decode(encoding, errors=errors)


@pytest.mark.asyncio
async def test_agent_file_tools_use_storage_paths(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/notes.md": b"# Notes\nneedle\n",
        f"{agent_id}/memory/memory.md": b"# Memory\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    listing = await agent_tools._storage_list_dir(agent_id, "workspace")
    read = await agent_tools._storage_read_file(agent_id, "workspace/notes.md")
    search = await agent_tools._storage_search_files(agent_id, "needle", path="workspace", file_pattern="*.md")
    found = await agent_tools._storage_find_files(agent_id, "*.md", path="workspace")

    assert "notes.md" in listing
    assert "needle" in read
    assert "workspace/notes.md:2" in search
    assert "workspace/notes.md" in found


@pytest.mark.asyncio
async def test_storage_read_file_accepts_binary_content_without_format_guard(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/uploads/sample.xlsx": b"PK\x03\x04\xffbinary",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    read = await agent_tools._storage_read_file(
        agent_id,
        "workspace/uploads/sample.xlsx",
    )

    assert "📄 workspace/uploads/sample.xlsx" in read
    assert "PK" in read
    assert "Unsupported" not in read


@pytest.mark.asyncio
async def test_storage_read_file_streams_requested_local_range(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = LocalStorageBackend(str(tmp_path))
    key = f"{agent_id}/workspace/large.txt"
    path = tmp_path / key
    path.parent.mkdir(parents=True)
    path.write_text("".join(f"line-{i}\n" for i in range(10_000)), encoding="utf-8")

    async def _full_read_must_not_run(_key):
        raise AssertionError("read_file range path must not load the entire local file")

    monkeypatch.setattr(storage, "read_bytes", _full_read_must_not_run)
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    read = await agent_tools._storage_read_file(
        agent_id,
        "workspace/large.txt",
        offset=9_998,
        limit=2,
    )

    assert "lines 9999-10000 of 10000" in read
    assert "line-9998" in read
    assert "line-9999" in read
    assert "line-0" not in read


@pytest.mark.asyncio
async def test_storage_read_file_reports_lookup_failure_instead_of_not_found(monkeypatch):
    agent_id = uuid.uuid4()
    storage = FailingStorageBackend(fail_operation="is_file")
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    read = await agent_tools._storage_read_file(agent_id, "workspace/report.txt")

    assert "File read failed." in read
    assert "Stage: storage_lookup" in read
    assert "Requested path: workspace/report.txt" in read
    assert "ConnectionError: storage unavailable" in read
    assert "File not found" not in read


@pytest.mark.asyncio
async def test_storage_read_file_reports_read_failure_with_exact_path(monkeypatch):
    agent_id = uuid.uuid4()
    storage = FailingStorageBackend(
        fail_operation="read_text",
        files={f"{agent_id}/workspace/report.txt": b"content"},
    )
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    read = await agent_tools._storage_read_file(agent_id, "workspace/report.txt")

    assert "File read failed." in read
    assert "Stage: storage_read" in read
    assert "Requested path: workspace/report.txt" in read
    assert "OSError: object read interrupted" in read


@pytest.mark.asyncio
async def test_storage_list_dir_reports_lookup_failure_instead_of_empty(monkeypatch):
    agent_id = uuid.uuid4()
    storage = FailingStorageBackend(fail_operation="exists")
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    listing = await agent_tools._storage_list_dir(agent_id, "workspace/uploads")

    assert "Directory listing failed." in listing
    assert "Stage: storage_lookup" in listing
    assert "Requested path: workspace/uploads" in listing
    assert "ConnectionError: storage unavailable" in listing
    assert "Empty directory" not in listing


@pytest.mark.asyncio
async def test_storage_read_file_does_not_guess_normalized_filename(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/uploads/6月稽核月报.xlsx": b"canonical content\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    read = await agent_tools._storage_read_file(
        agent_id,
        "workspace/uploads/６ 月稽核月报.xlsx",
    )

    assert "File not found." in read
    assert "Requested path: workspace/uploads/６ 月稽核月报.xlsx" in read
    assert "The requested path was not modified." in read
    assert "canonical content" not in read


@pytest.mark.asyncio
async def test_storage_read_file_does_not_offer_similar_candidates(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/uploads/6月报告.xlsx": b"compact",
        f"{agent_id}/workspace/uploads/6 月报告.xlsx": b"spaced",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    read = await agent_tools._storage_read_file(
        agent_id,
        "workspace/uploads/６　月报告.xlsx",
    )

    assert "File not found." in read
    assert "Requested path: workspace/uploads/６　月报告.xlsx" in read
    assert "6月报告.xlsx" not in read
    assert "6 月报告.xlsx" not in read
    assert "compact" not in read
    assert "spaced" not in read


@pytest.mark.asyncio
async def test_read_document_requires_exact_path_before_materialization(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/uploads/山东7月门店等级.xlsx": b"xlsx bytes",
        f"{agent_id}/workspace/uploads/unrelated.xlsx": b"other",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    result = await agent_tools._read_document_from_storage(
        agent_id,
        "workspace/uploads/山东 7 月门店等级.xlsx",
    )

    assert "File not found." in result
    assert "Requested path: workspace/uploads/山东 7 月门店等级.xlsx" in result
    assert "The requested path was not modified." in result


@pytest.mark.asyncio
async def test_read_document_reports_storage_lookup_failure_instead_of_not_found(monkeypatch):
    agent_id = uuid.uuid4()
    storage = FailingStorageBackend(fail_operation="is_file")
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    result = await agent_tools._read_document_from_storage(
        agent_id,
        "workspace/uploads/report.xlsx",
    )

    assert "Document read was not started." in result
    assert "Stage: storage_lookup" in result
    assert "Requested path: workspace/uploads/report.xlsx" in result
    assert "ConnectionError: storage unavailable" in result
    assert "File not found" not in result


@pytest.mark.asyncio
async def test_read_document_materializes_exact_source_only(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/uploads/山东7月门店等级.xlsx": b"xlsx bytes",
        f"{agent_id}/workspace/uploads/unrelated.xlsx": b"other",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    observed: dict[str, object] = {}

    async def _fake_read_document(ws, rel_path, max_chars, tenant_id):
        observed["rel_path"] = rel_path
        observed["content"] = (ws / rel_path).read_bytes()
        observed["unrelated_exists"] = (ws / "workspace/uploads/unrelated.xlsx").exists()
        return "document ok"

    monkeypatch.setattr(agent_tools, "_read_document", _fake_read_document)

    result = await agent_tools._read_document_from_storage(
        agent_id,
        "workspace/uploads/山东7月门店等级.xlsx",
    )

    assert result == "document ok"
    assert observed == {
        "rel_path": "workspace/uploads/山东7月门店等级.xlsx",
        "content": b"xlsx bytes",
        "unrelated_exists": False,
    }


@pytest.mark.asyncio
async def test_read_document_materializes_file_larger_than_generic_tool_limit(monkeypatch):
    agent_id = uuid.uuid4()
    content = b"x" * (agent_tools.TOOL_MATERIALIZE_MAX_FILE_BYTES + 1)
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/uploads/large.xlsx": content,
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    async def _fake_read_document(ws, rel_path, max_chars, tenant_id):
        return f"materialized={int((ws / rel_path).is_file())};size={(ws / rel_path).stat().st_size}"

    monkeypatch.setattr(agent_tools, "_read_document", _fake_read_document)

    result = await agent_tools._read_document_from_storage(
        agent_id,
        "workspace/uploads/large.xlsx",
    )

    assert result == f"materialized=1;size={len(content)}"


@pytest.mark.asyncio
async def test_read_document_reports_existing_file_over_document_limit(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/uploads/large.xlsx": b"123456",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_tools, "_READ_DOCUMENT_MAX_FILE_BYTES", 5)

    result = await agent_tools._read_document_from_storage(
        agent_id,
        "workspace/uploads/large.xlsx",
    )

    assert "Document read was not started." in result
    assert "Stage: materialization" in result
    assert "File exists: true" in result
    assert "File size: 6 bytes" in result
    assert "Limit: 5 bytes" in result
    assert "file exceeds the document-processing limit" in result


@pytest.mark.asyncio
async def test_storage_list_dir_returns_full_canonical_virtual_paths(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/uploads/report.xlsx": b"xlsx",
        f"{agent_id}/workspace/uploads/archive/old.xlsx": b"old",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    result = await agent_tools._storage_list_dir(agent_id, "workspace/uploads")

    assert "📄 workspace/uploads/report.xlsx" in result
    assert "📁 workspace/uploads/archive/" in result


@pytest.mark.asyncio
async def test_temp_workspace_runner_canonicalizes_sources_but_not_targets(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/uploads/6月源数据.csv": b"a,b\n1,2\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    source_path = "workspace/uploads/6 月源数据.csv"
    target_path = "workspace/reports/6 月结果.xlsx"

    async def _runner(ws):
        resolved_source = agent_tools._resolve_tool_source_path(ws, source_path)
        assert resolved_source.name == "6月源数据.csv"
        assert resolved_source.read_bytes() == b"a,b\n1,2\n"
        # A new target must retain the exact name requested by the caller.
        assert not (ws / target_path).exists()
        return "runner ok"

    result = await agent_tools._run_with_temp_workspace(
        agent_id,
        None,
        _runner,
        paths=[source_path, target_path],
        source_paths=[source_path],
    )

    assert result == "runner ok"


def test_execute_code_canonicalizes_unique_existing_upload_literals(tmp_path):
    upload_dir = tmp_path / "workspace/uploads"
    upload_dir.mkdir(parents=True)
    (upload_dir / "6月稽核月报.xlsx").write_bytes(b"xlsx")
    (upload_dir / "山东7月门店等级.xlsx").write_bytes(b"xlsx")

    code = """\
from openpyxl import load_workbook
file1 = 'workspace/uploads/6 月稽核月报.xlsx'
file2 = os.path.join(os.getcwd(), \"workspace/uploads/山东 7 月门店等级.xlsx\")
target = 'workspace/reports/6 月结果.xlsx'
"""
    rewritten, replacements = agent_tools._canonicalize_execute_code_upload_paths(tmp_path, code)

    assert "workspace/uploads/6月稽核月报.xlsx" in rewritten
    assert "workspace/uploads/山东7月门店等级.xlsx" in rewritten
    assert "workspace/reports/6 月结果.xlsx" in rewritten
    assert len(replacements) == 2


def test_execute_code_does_not_guess_ambiguous_upload_literal(tmp_path):
    upload_dir = tmp_path / "workspace/uploads"
    upload_dir.mkdir(parents=True)
    (upload_dir / "6月报告.xlsx").write_bytes(b"compact")
    (upload_dir / "6 月报告.xlsx").write_bytes(b"spaced")

    original = "load_workbook('workspace/uploads/６　月报告.xlsx')"
    rewritten, replacements = agent_tools._canonicalize_execute_code_upload_paths(tmp_path, original)

    assert rewritten == original
    assert replacements == []


@pytest.mark.asyncio
async def test_temp_workspace_materializes_only_requested_paths(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/input.md": b"# Input\n",
        f"{agent_id}/workspace/other.md": b"# Other\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace/input.md"])
    try:
        assert (temp_ws.root / "workspace" / "input.md").read_text(encoding="utf-8") == "# Input\n"
        assert not (temp_ws.root / "workspace" / "other.md").exists()
    finally:
        temp_ws.cleanup()


@pytest.mark.asyncio
async def test_execute_tool_list_files_does_not_create_persistent_workspace(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/input.md": b"# Input\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", tmp_path)

    async def _tenant(_agent_id):
        return None

    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", _tenant)

    result = await agent_tools.execute_tool("list_files", {"path": "workspace"}, agent_id, agent_id)

    assert "input.md" in result
    assert not (tmp_path / str(agent_id)).exists()


@pytest.mark.asyncio
async def test_write_workspace_file_does_not_mirror_to_local_for_non_local_storage(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _noop_revision(*args, **kwargs):
        return None

    monkeypatch.setattr(workspace_collaboration, "record_revision", _noop_revision)

    result = await workspace_collaboration.write_workspace_file(
        db=None,
        agent_id=agent_id,
        base_dir=tmp_path / str(agent_id),
        path="workspace/test.md",
        content="hello",
        actor_type="agent",
        actor_id=agent_id,
        enforce_human_lock=False,
    )

    assert result.ok is True
    assert storage.files[f"{agent_id}/workspace/test.md"] == b"hello"
    assert not (tmp_path / str(agent_id) / "workspace" / "test.md").exists()


@pytest.mark.asyncio
async def test_workspace_write_queue_covers_revision_before_next_mutation(monkeypatch, tmp_path):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend()
    revision_entered = asyncio.Event()
    release_revision = asyncio.Event()
    monkeypatch.setattr(workspace_collaboration, "get_storage_backend", lambda: storage)

    async def _record_revision(*args, **kwargs):
        if kwargs.get("after_content") == "first":
            revision_entered.set()
            await release_revision.wait()

    monkeypatch.setattr(workspace_collaboration, "record_revision", _record_revision)

    async def write(content):
        return await workspace_collaboration.write_workspace_file(
            db=None,
            agent_id=agent_id,
            base_dir=tmp_path / str(agent_id),
            path="workspace/test.md",
            content=content,
            actor_type="agent",
            actor_id=agent_id,
            enforce_human_lock=False,
        )

    first_task = asyncio.create_task(write("first"))
    await revision_entered.wait()
    second_task = asyncio.create_task(write("second"))
    await asyncio.sleep(0.02)
    assert storage.files[f"{agent_id}/workspace/test.md"] == b"first"

    release_revision.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)
    assert first_result.ok is True
    assert second_result.ok is True
    assert storage.files[f"{agent_id}/workspace/test.md"] == b"second"


@pytest.mark.asyncio
async def test_flush_temp_workspace_only_writes_changed_files(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/input.md": b"# Input\n",
        f"{agent_id}/workspace/other.md": b"# Other\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace"])
    try:
        (temp_ws.root / "workspace" / "input.md").write_text("# Updated\n", encoding="utf-8")
        result = await agent_tools.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert result["updated"] == ["workspace/input.md"]
    assert "workspace/other.md" in result["skipped"]
    assert storage.files[f"{agent_id}/workspace/input.md"] == b"# Updated\n"
    assert storage.files[f"{agent_id}/workspace/other.md"] == b"# Other\n"


@pytest.mark.asyncio
async def test_flush_temp_workspace_fails_on_conflict(monkeypatch):
    agent_id = uuid.uuid4()
    storage = MemoryStorageBackend({
        f"{agent_id}/workspace/input.md": b"# Input\n",
    })
    monkeypatch.setattr(agent_tools, "get_storage_backend", lambda: storage)

    temp_ws = await agent_tools._prepare_temp_workspace(agent_id, paths=["workspace/input.md"])
    try:
        (temp_ws.root / "workspace" / "input.md").write_text("# Local change\n", encoding="utf-8")
        await storage.write_bytes(f"{agent_id}/workspace/input.md", b"# Remote change\n")
        result = await agent_tools.flush_temp_workspace(temp_ws)
    finally:
        temp_ws.cleanup()

    assert result["conflicted"] == ["workspace/input.md"]
    assert storage.files[f"{agent_id}/workspace/input.md"] == b"# Remote change\n"
