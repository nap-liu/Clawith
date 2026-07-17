from __future__ import annotations

import asyncio
import uuid
from unittest.mock import patch

import pytest

from app.services.agent_memory import MEMORY_INDEX_TEMPLATE
from app.services.storage_runtime.base import ConditionalWriteResult, StorageVersion, WriteCondition
from app.services.storage_runtime.local import LocalStorageBackend
from scripts.backfill_agent_memory_v2 import backfill_agent_memory


class _Storage:
    def __init__(
        self,
        files: dict[str, bytes] | None = None,
        *,
        race_on_create: dict[str, bytes] | None = None,
    ):
        self.files = dict(files or {})
        self.race_on_create = dict(race_on_create or {})

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def read_bytes(self, key: str) -> bytes:
        return self.files[key]

    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.files[key] = data

    async def write_text(self, key: str, content: str, encoding: str = "utf-8") -> None:
        self.files[key] = content.encode(encoding)

    async def write_bytes_if_match(
        self,
        key: str,
        data: bytes,
        *,
        condition: WriteCondition | None = None,
        content_type: str | None = None,
    ) -> ConditionalWriteResult:
        if key in self.race_on_create:
            self.files[key] = self.race_on_create.pop(key)
        current = StorageVersion(key=key, exists=key in self.files, is_dir=False)
        if condition and condition.require_absent and current.exists:
            return ConditionalWriteResult(ok=False, conflict=True, current_version=current)
        self.files[key] = data
        return ConditionalWriteResult(
            ok=True,
            current_version=StorageVersion(key=key, exists=True, is_dir=False),
        )

    async def delete(self, key: str) -> None:
        self.files.pop(key, None)


@pytest.mark.asyncio
async def test_backfill_copies_legacy_core_seeds_index_and_removes_only_verified_duplicate():
    agent_id = uuid.uuid4()
    legacy_key = f"{agent_id}/memory.md"
    core_key = f"{agent_id}/memory/memory.md"
    index_key = f"{agent_id}/memory/MEMORY_INDEX.md"
    storage = _Storage({legacy_key: b"legacy durable memory"})

    with patch("scripts.backfill_agent_memory_v2.get_storage_backend", return_value=storage):
        result = await backfill_agent_memory(
            agent_id,
            apply=True,
            remove_legacy_root=True,
        )

    assert result.core_action == "copy_legacy_root"
    assert result.index_action == "create_template"
    assert result.legacy_action == "removed_verified_duplicate"
    assert storage.files[core_key] == b"legacy durable memory"
    assert storage.files[index_key] == MEMORY_INDEX_TEMPLATE.encode()
    assert legacy_key not in storage.files


@pytest.mark.asyncio
async def test_backfill_never_overwrites_existing_ordinary_files():
    agent_id = uuid.uuid4()
    core_key = f"{agent_id}/memory/memory.md"
    index_key = f"{agent_id}/memory/MEMORY_INDEX.md"
    legacy_key = f"{agent_id}/memory.md"
    storage = _Storage({
        core_key: b"agent-maintained core",
        index_key: b"agent-maintained ordinary index",
        legacy_key: b"different legacy content",
    })

    with patch("scripts.backfill_agent_memory_v2.get_storage_backend", return_value=storage):
        result = await backfill_agent_memory(
            agent_id,
            apply=True,
            remove_legacy_root=True,
        )

    assert result.core_action == "unchanged"
    assert result.index_action == "unchanged"
    assert result.legacy_action == "retain_not_verified"
    assert storage.files[core_key] == b"agent-maintained core"
    assert storage.files[index_key] == b"agent-maintained ordinary index"
    assert storage.files[legacy_key] == b"different legacy content"


@pytest.mark.asyncio
async def test_backfill_dry_run_has_no_side_effects():
    agent_id = uuid.uuid4()
    storage = _Storage()

    with patch("scripts.backfill_agent_memory_v2.get_storage_backend", return_value=storage):
        result = await backfill_agent_memory(agent_id, apply=False)

    assert result.core_action == "create_template"
    assert result.index_action == "create_template"
    assert storage.files == {}


@pytest.mark.asyncio
async def test_backfill_does_not_overwrite_a_concurrent_first_memory_write():
    agent_id = uuid.uuid4()
    core_key = f"{agent_id}/memory/memory.md"
    index_key = f"{agent_id}/memory/MEMORY_INDEX.md"
    legacy_key = f"{agent_id}/memory.md"
    storage = _Storage(
        {legacy_key: b"legacy memory"},
        race_on_create={
            core_key: b"agent concurrent core",
            index_key: b"agent concurrent index",
        },
    )

    with patch("scripts.backfill_agent_memory_v2.get_storage_backend", return_value=storage):
        result = await backfill_agent_memory(
            agent_id,
            apply=True,
            remove_legacy_root=True,
        )

    assert result.core_action == "unchanged_concurrent"
    assert result.index_action == "unchanged_concurrent"
    assert result.legacy_action == "retain_not_verified"
    assert storage.files[core_key] == b"agent concurrent core"
    assert storage.files[index_key] == b"agent concurrent index"
    assert storage.files[legacy_key] == b"legacy memory"


@pytest.mark.asyncio
async def test_local_require_absent_allows_exactly_one_concurrent_creator(tmp_path):
    storage = LocalStorageBackend(str(tmp_path))
    key = "agent/memory/memory.md"
    first, second = await asyncio.gather(
        storage.write_bytes_if_match(
            key,
            b"first",
            condition=WriteCondition(require_absent=True),
        ),
        storage.write_bytes_if_match(
            key,
            b"second",
            condition=WriteCondition(require_absent=True),
        ),
    )

    assert sorted([first.ok, second.ok]) == [False, True]
    assert sorted([first.conflict, second.conflict]) == [False, True]
    assert await storage.read_bytes(key) in {b"first", b"second"}
