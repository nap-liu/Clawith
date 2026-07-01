"""Regression guard: long-term memory (memory.md) must be injected in FULL.

Bug: build_agent_context read memory via `_read_file_safe(..., 2000)`, which
silently truncated memory.md to the first 2000 chars and appended
"...(truncated)". Curated notes living past char 2000 (e.g. a "hidden rbac
module" section) never reached the LLM — the agent behaved as if its own
memory did not exist.

Fix: `_read_file_safe` accepts `max_chars=None` (no truncation) and the memory
call site passes None. These tests pin both the helper behavior and the call
site so nobody silently reintroduces a hard cap on memory.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

# Import the full model graph so FK references resolve at table-mapping time.
from app.models.user import User, Identity  # noqa: F401
from app.models.tenant import Tenant  # noqa: F401
from app.models.identity import IdentityProvider, SSOScanSession  # noqa: F401
from app.models.participant import Participant  # noqa: F401
from app.database import engine
from app.services.agent_context import _read_file_safe, build_agent_context
from app.services.storage import normalize_storage_key

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_async_engine_between_tests():
    await engine.dispose()
    yield
    await engine.dispose()


class _FakeStorage:
    """Minimal StorageBackend stand-in backed by an in-memory {key: text} map."""

    def __init__(self, files: dict[str, str]):
        self._files = files

    async def exists(self, key: str) -> bool:
        return key in self._files

    async def is_file(self, key: str) -> bool:
        return key in self._files

    async def read_text(self, key: str, encoding: str = "utf-8", errors: str = "replace") -> str:
        return self._files[key]


# A memory whose meaningful tail sits well past the old 2000-char cap.
_TAIL = "RBAC_TAIL_SENTINEL_隐藏rbac模块需显式调用"
_LONG_MEMORY = "记忆开头：常规说明。\n" + ("正文填充行，用于把内容推过 2000 字符上限。\n" * 200) + "\n## 末尾段\n" + _TAIL + "\n"


async def test_read_file_safe_none_disables_truncation():
    """max_chars=None returns the full file; a finite cap truncates + marks it."""
    assert len(_LONG_MEMORY) > 2000  # sanity: tail is genuinely past the old cap
    key = normalize_storage_key(f"{uuid.uuid4()}/memory/memory.md")
    fake = _FakeStorage({key: _LONG_MEMORY})

    with patch("app.services.agent_context.get_storage_backend", return_value=fake):
        full = await _read_file_safe(key, None)
        capped = await _read_file_safe(key, 2000)

    # None → whole file, tail present, no truncation marker.
    assert _TAIL in full
    assert "...(truncated)" not in full
    assert full == _LONG_MEMORY.strip()

    # Finite cap → tail dropped, marker appended (old behavior, still available).
    assert _TAIL not in capped
    assert capped.endswith("...(truncated)")


async def test_build_agent_context_injects_full_memory():
    """End-to-end: the memory tail (> 2000 chars in) must survive into context.

    Runs the REAL truncation logic against a faked storage backend, so a
    regression that re-caps the memory call site (or _read_file_safe) makes the
    tail disappear and fails this test.
    """
    agent_id = uuid.uuid4()
    mem_key = normalize_storage_key(f"{agent_id}/memory/memory.md")
    fake = _FakeStorage({mem_key: _LONG_MEMORY})

    with (
        patch("app.services.agent_context.get_storage_backend", return_value=fake),
        patch("app.services.agent_context._load_skills_index", new_callable=AsyncMock, return_value=""),
        patch("app.services.timezone_utils.get_agent_timezone", new_callable=AsyncMock, return_value="UTC"),
    ):
        _static, dynamic = await build_agent_context(agent_id, "TestAgent")

    assert "## Memory" in dynamic
    assert _TAIL in dynamic  # tail past the old 2000-char cap reached the context
    assert "...(truncated)" not in dynamic
