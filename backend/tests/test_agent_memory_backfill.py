from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from app.services.agent_memory import MEMORY_INDEX_TEMPLATE
from scripts.backfill_agent_memory_v2 import backfill_agent_memory


class _Storage:
    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def read_bytes(self, key: str) -> bytes:
        return self.files[key]

    async def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.files[key] = data

    async def write_text(self, key: str, content: str, encoding: str = "utf-8") -> None:
        self.files[key] = content.encode(encoding)

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
