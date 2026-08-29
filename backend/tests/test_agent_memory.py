from __future__ import annotations

import re
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.agent_memory import (
    CORE_MEMORY_TEMPLATE,
    MEMORY_INDEX_TEMPLATE,
    MEMORY_SYSTEM_PROMPT,
    load_agent_memory_snapshot,
)
from app.services.agent_tools import AGENT_TOOLS
from app.services.tool_seeder import BUILTIN_TOOLS
from app.services.storage_runtime.base import StorageEntry


class _MemoryStorage:
    def __init__(self, files: dict[str, str], directories: dict[str, list[StorageEntry]]):
        self.files = files
        self.directories = directories
        self.list_dir_calls = 0

    async def exists(self, key: str) -> bool:
        return key in self.files or key in self.directories

    async def is_file(self, key: str) -> bool:
        return key in self.files

    async def list_dir(self, key: str) -> list[StorageEntry]:
        self.list_dir_calls += 1
        return list(self.directories.get(key, []))

    async def read_text(self, key: str, encoding: str = "utf-8", errors: str = "replace") -> str:
        return self.files[key]


def _dir(agent_id: uuid.UUID, name: str) -> StorageEntry:
    return StorageEntry(name=name, key=f"{agent_id}/memory/{name}", is_dir=True)


@pytest.mark.asyncio
async def test_loads_core_index_and_two_most_recent_existing_daily_records():
    agent_id = uuid.uuid4()
    prefix = f"{agent_id}/memory"
    files = {
        f"{prefix}/memory.md": "CORE-TAIL",
        f"{prefix}/MEMORY_INDEX.md": "STRUCTURE-GUIDE",
        f"{prefix}/2026-07-17/memory.md": "DAY-17",
        f"{prefix}/2026-07-15/memory.md": "DAY-15",
        f"{prefix}/2026-07-12/memory.md": "DAY-12",
        f"{prefix}/2026-07-18/memory.md": "FUTURE",
        f"{prefix}/not-a-date/memory.md": "INVALID",
    }
    storage = _MemoryStorage(
        files,
        {prefix: [
            _dir(agent_id, "2026-07-12"),
            _dir(agent_id, "not-a-date"),
            _dir(agent_id, "2026-07-18"),
            _dir(agent_id, "2026-07-15"),
            _dir(agent_id, "2026-07-17"),
        ]},
    )

    with patch("app.services.agent_memory.get_storage_backend", return_value=storage):
        snapshot = await load_agent_memory_snapshot(agent_id, today=date(2026, 7, 17))

    assert snapshot.core_memory == "CORE-TAIL"
    assert snapshot.structure_guide == "STRUCTURE-GUIDE"
    assert [record.record_date.isoformat() for record in snapshot.daily_records] == [
        "2026-07-17",
        "2026-07-15",
    ]
    rendered = snapshot.render()
    assert "## Core Memory\nCORE-TAIL" in rendered
    assert "## Memory Structure\nSTRUCTURE-GUIDE" in rendered
    assert "DAY-17" in rendered and "DAY-15" in rendered
    assert "DAY-12" not in rendered and "FUTURE" not in rendered and "INVALID" not in rendered


@pytest.mark.asyncio
async def test_empty_or_missing_daily_record_does_not_consume_recent_limit():
    agent_id = uuid.uuid4()
    prefix = f"{agent_id}/memory"
    storage = _MemoryStorage(
        {
            f"{prefix}/2026-07-17/memory.md": "",
            f"{prefix}/2026-07-16/memory.md": "DAY-16",
            f"{prefix}/2026-07-15/memory.md": "DAY-15",
        },
        {prefix: [
            _dir(agent_id, "2026-07-17"),
            _dir(agent_id, "2026-07-16"),
            _dir(agent_id, "2026-07-15"),
        ]},
    )

    with patch("app.services.agent_memory.get_storage_backend", return_value=storage):
        snapshot = await load_agent_memory_snapshot(agent_id, today=date(2026, 7, 17))

    assert [record.record_date.isoformat() for record in snapshot.daily_records] == [
        "2026-07-16",
        "2026-07-15",
    ]


@pytest.mark.asyncio
async def test_zero_daily_limit_loads_no_daily_records():
    agent_id = uuid.uuid4()
    prefix = f"{agent_id}/memory"
    storage = _MemoryStorage(
        {
            f"{prefix}/memory.md": "CORE",
            f"{prefix}/MEMORY_INDEX.md": "STRUCTURE-GUIDE",
            f"{prefix}/2026-07-17/memory.md": "DAY-17",
        },
        {prefix: [_dir(agent_id, "2026-07-17")]},
    )

    with patch("app.services.agent_memory.get_storage_backend", return_value=storage):
        snapshot = await load_agent_memory_snapshot(
            agent_id,
            today=date(2026, 7, 17),
            daily_limit=0,
        )

    assert snapshot.core_memory == "CORE"
    assert snapshot.structure_guide == ""
    assert snapshot.daily_records == ()
    assert storage.list_dir_calls == 0


def test_memory_index_template_matches_seed_file_and_contains_no_dynamic_dates():
    template_path = Path(__file__).parents[1] / "agent_template" / "memory" / "MEMORY_INDEX.md"
    assert template_path.read_text(encoding="utf-8") == MEMORY_INDEX_TEMPLATE
    assert re.search(r"\b20\d{2}-\d{2}-\d{2}\b", MEMORY_INDEX_TEMPLATE) is None
    assert "<YYYY-MM-DD>" in MEMORY_INDEX_TEMPLATE


def test_core_memory_template_matches_seed_file_and_is_always_renderable():
    template_path = Path(__file__).parents[1] / "agent_template" / "memory" / "memory.md"
    assert template_path.read_text(encoding="utf-8") == CORE_MEMORY_TEMPLATE


def test_static_memory_prompt_is_scenario_and_date_independent_but_guides_daily_writes():
    assert re.search(r"\b20\d{2}-\d{2}-\d{2}\b", MEMORY_SYSTEM_PROMPT) is None
    assert "memory/<YYYY-MM-DD>/memory.md" in MEMORY_SYSTEM_PROMPT
    assert "before the final response" in MEMORY_SYSTEM_PROMPT
    assert "Do not write Daily Memory for greetings" in MEMORY_SYSTEM_PROMPT
    assert "throughout the complete tool-calling loop" in MEMORY_SYSTEM_PROMPT


def test_runtime_and_fallback_file_tool_contracts_both_guide_daily_memory():
    seeded = {tool["name"]: tool for tool in BUILTIN_TOOLS}
    fallback = {tool["function"]["name"]: tool["function"] for tool in AGENT_TOOLS}

    for name in ("write_file", "edit_file"):
        assert "memory/<YYYY-MM-DD>/memory.md" in seeded[name]["description"]
        assert "memory/<YYYY-MM-DD>/memory.md" in fallback[name]["description"]
