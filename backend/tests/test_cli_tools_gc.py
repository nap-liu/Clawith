"""GC rule tests — hard-reference check + age gate."""

from __future__ import annotations

import io
import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.cli_tools.gc import gc_cli_binaries
from app.services.cli_tools.storage import BinaryStorage


_SHEBANG = b"#!/bin/sh\necho hi\n"


def _age_file(path, days: int) -> None:
    past = time.time() - days * 86400
    os.utime(path, (past, past))


def _mock_db_with_tools(tools: list[MagicMock]) -> MagicMock:
    """Build a MagicMock db whose execute().scalars().all() returns `tools`."""
    scalars_result = MagicMock()
    scalars_result.all = MagicMock(return_value=tools)

    exec_result = MagicMock()
    exec_result.scalars = MagicMock(return_value=scalars_result)

    db = MagicMock()
    db.execute = AsyncMock(return_value=exec_result)
    return db


@pytest.mark.asyncio
async def test_gc_deletes_orphan_older_than_threshold(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    sha_a, _ = await storage.write(
        tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG), max_bytes=1_000_000,
    )
    sha_b, _ = await storage.write(
        tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG + b"\n"), max_bytes=1_000_000,
    )

    orphan_path = storage.resolve("t1", "tool1", sha_b)
    _age_file(orphan_path, days=31)

    tool_a = MagicMock()
    tool_a.config = {"binary_sha256": sha_a}
    db = _mock_db_with_tools([tool_a])

    deleted = await gc_cli_binaries(db=db, storage=storage, age_threshold_days=30)
    assert deleted == 1
    assert storage.resolve("t1", "tool1", sha_a).exists()
    assert not orphan_path.exists()


@pytest.mark.asyncio
async def test_gc_keeps_referenced_even_when_old(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    sha_a, _ = await storage.write(
        tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG), max_bytes=1_000_000,
    )
    _age_file(storage.resolve("t1", "tool1", sha_a), days=365)

    tool_a = MagicMock()
    tool_a.config = {"binary_sha256": sha_a}
    db = _mock_db_with_tools([tool_a])

    deleted = await gc_cli_binaries(db=db, storage=storage, age_threshold_days=30)
    assert deleted == 0
    assert storage.resolve("t1", "tool1", sha_a).exists()


@pytest.mark.asyncio
async def test_gc_keeps_young_orphan(tmp_path):
    storage = BinaryStorage(root=tmp_path)
    sha, _ = await storage.write(
        tenant_key="t1", tool_id="tool1", stream=io.BytesIO(_SHEBANG), max_bytes=1_000_000,
    )
    # File is fresh. No referencing Tool row.
    db = _mock_db_with_tools([])

    deleted = await gc_cli_binaries(db=db, storage=storage, age_threshold_days=30)
    assert deleted == 0
    assert storage.resolve("t1", "tool1", sha).exists()
