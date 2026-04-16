"""Unit tests for the CLI-tool binary versioning service.

These exercise ``record_new_version`` / ``rollback_to`` / ``list_versions``
against an in-memory aiosqlite DB that mirrors the production schema
(including the partial unique index on is_current). The BinaryStorage
side is a real tmp_path directory so file eviction is observable.

Why aiosqlite: the production code uses async SQLAlchemy only — any
sync test fixture would hide real bugs (e.g. forgetting to await a
flush). aiosqlite gives us the async contract without a live postgres.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.cli_tool_binary import CliToolBinaryVersion
from app.models.tool import Tool
from app.models.user import User  # noqa: F401 — registers users table for FK resolution
from app.services.cli_tools import versioning as versioning_service
from app.services.cli_tools.schema import (
    BinaryMetadata,
    CliToolConfig,
    RuntimeConfig,
    SandboxConfig,
)
from app.services.cli_tools.storage import BinaryStorage


_SHEBANG = b"#!/bin/sh\necho hi\n"


# ─────────────────────────────────────────────────────────────────────────
# Fixtures — async SQLite engine + per-test session, fresh schema each time
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with eng.begin() as conn:
        # Create a minimal users table so the FK on
        # cli_tool_binary_versions.uploaded_by_user_id can resolve. We
        # don't actually need the User model loaded — just the target
        # table name.
        await conn.execute(text(
            "CREATE TABLE users (id TEXT PRIMARY KEY)"
        ))
        # Create the tool + version tables through the ORM metadata so
        # column types (especially UUID <-> TEXT) match what the service
        # code expects.
        await conn.run_sync(lambda sync_conn: Tool.__table__.create(sync_conn))
        await conn.run_sync(
            lambda sync_conn: CliToolBinaryVersion.__table__.create(sync_conn)
        )
        # Partial unique index: sqlite supports WHERE-partial indexes.
        await conn.execute(text(
            "CREATE UNIQUE INDEX uq_cli_tool_binary_versions_current "
            "ON cli_tool_binary_versions (tool_id) WHERE is_current = 1"
        ))
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        yield s


async def _insert_tool(session: AsyncSession, *, tenant_id=None) -> Tool:
    config = CliToolConfig(
        binary=BinaryMetadata(),
        runtime=RuntimeConfig(),
        sandbox=SandboxConfig(),
    ).model_dump(mode="json")
    tool = Tool(
        id=uuid.uuid4(),
        name=f"tool-{uuid.uuid4().hex[:6]}",
        display_name="Test Tool",
        description="",
        type="cli",
        category="general",
        icon="🔧",
        parameters_schema={},
        config=config,
        config_schema={},
        enabled=True,
        is_default=False,
        source="admin",
        tenant_id=tenant_id,
    )
    session.add(tool)
    await session.flush()
    return tool


# ─────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_upload_creates_single_current_version(session, tmp_path):
    """A brand-new tool uploading its first binary ends up with exactly
    one version row, ``is_current=True``, and ``tool.config.binary``
    mirrors that row."""
    tool = await _insert_tool(session)
    storage = BinaryStorage(root=tmp_path)

    v = await versioning_service.record_new_version(
        session,
        tool,
        sha256="a" * 64,
        size=1024,
        original_name="svc-1.0",
        user_id=None,
        binary_storage=storage,
    )
    await session.flush()

    rows = (
        await session.execute(
            select(CliToolBinaryVersion).where(CliToolBinaryVersion.tool_id == tool.id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == v.id
    assert rows[0].is_current is True
    assert tool.config["binary"]["sha256"] == "a" * 64
    assert tool.config["binary"]["original_name"] == "svc-1.0"


@pytest.mark.asyncio
async def test_subsequent_upload_demotes_previous_current(session, tmp_path):
    """Uploading a second binary flips the first row's is_current off
    and makes the new row the sole current version."""
    tool = await _insert_tool(session)
    storage = BinaryStorage(root=tmp_path)

    v1 = await versioning_service.record_new_version(
        session, tool, sha256="a" * 64, size=10, original_name="svc-1.0",
        user_id=None, binary_storage=storage,
    )
    v2 = await versioning_service.record_new_version(
        session, tool, sha256="b" * 64, size=20, original_name="svc-1.1",
        user_id=None, binary_storage=storage,
    )
    await session.flush()

    rows = sorted(
        (await session.execute(
            select(CliToolBinaryVersion).where(CliToolBinaryVersion.tool_id == tool.id)
        )).scalars().all(),
        key=lambda r: r.uploaded_at,
    )
    assert len(rows) == 2
    # Old row present, not current
    v1_row = next(r for r in rows if r.id == v1.id)
    v2_row = next(r for r in rows if r.id == v2.id)
    assert v1_row.is_current is False
    assert v2_row.is_current is True
    assert tool.config["binary"]["sha256"] == "b" * 64


@pytest.mark.asyncio
async def test_rollback_swaps_current_flag(session, tmp_path):
    """Rollback flips is_current on the target; the previously-current
    row retains its sha and stays in history for future roll-forward."""
    tool = await _insert_tool(session)
    storage = BinaryStorage(root=tmp_path)

    v1 = await versioning_service.record_new_version(
        session, tool, sha256="a" * 64, size=10, original_name="v1",
        user_id=None, binary_storage=storage,
    )
    v2 = await versioning_service.record_new_version(
        session, tool, sha256="b" * 64, size=20, original_name="v2",
        user_id=None, binary_storage=storage,
    )

    result = await versioning_service.rollback_to(session, tool, v1.id)
    await session.flush()

    assert result.id == v1.id
    # Reload both; exactly one is current and it's v1.
    rows = (await session.execute(
        select(CliToolBinaryVersion).where(CliToolBinaryVersion.tool_id == tool.id)
    )).scalars().all()
    currents = [r for r in rows if r.is_current]
    assert len(currents) == 1
    assert currents[0].id == v1.id
    # v2 still there, just demoted.
    assert any(r.id == v2.id and r.is_current is False for r in rows)


@pytest.mark.asyncio
async def test_rollback_updates_tool_config_binary_subtree(session, tmp_path):
    """After rollback, ``tool.config.binary`` reflects the rolled-back
    version (not the one that was current when rollback started)."""
    tool = await _insert_tool(session)
    storage = BinaryStorage(root=tmp_path)

    v1 = await versioning_service.record_new_version(
        session, tool, sha256="a" * 64, size=10, original_name="v1",
        user_id=None, binary_storage=storage,
    )
    await versioning_service.record_new_version(
        session, tool, sha256="b" * 64, size=20, original_name="v2",
        user_id=None, binary_storage=storage,
    )
    assert tool.config["binary"]["sha256"] == "b" * 64

    await versioning_service.rollback_to(session, tool, v1.id)

    assert tool.config["binary"]["sha256"] == "a" * 64
    assert tool.config["binary"]["size"] == 10
    assert tool.config["binary"]["original_name"] == "v1"


@pytest.mark.asyncio
async def test_rollback_writes_audit_log(session, tmp_path, monkeypatch):
    """The API handler wraps rollback with an AuditLog row; this test
    exercises the API-layer code path directly using the real service so
    the from_sha / to_sha fields are populated correctly."""
    from app.api import cli_tools as cli_tools_api

    tool = await _insert_tool(session)
    storage = BinaryStorage(root=tmp_path)

    v1 = await versioning_service.record_new_version(
        session, tool, sha256="a" * 64, size=10, original_name="v1",
        user_id=None, binary_storage=storage,
    )
    await versioning_service.record_new_version(
        session, tool, sha256="b" * 64, size=20, original_name="v2",
        user_id=None, binary_storage=storage,
    )
    await session.commit()

    # Capture audit rows by intercepting db.add — the FakeDB pattern from
    # test_cli_tools_api.py would work too, but the real session gives us
    # flush semantics the partial index depends on.
    captured_audit: list = []
    original_add = session.add

    def _spy_add(obj):
        # Filter to AuditLog-like objects (duck-typed to avoid importing
        # the full model schema into this sqlite session).
        if type(obj).__name__ == "AuditLog":
            captured_audit.append(obj)
        else:
            original_add(obj)

    monkeypatch.setattr(session, "add", _spy_add)

    user = SimpleNamespace(
        id=uuid.uuid4(),
        role="platform_admin",
        tenant_id=None,
    )

    body = cli_tools_api.RollbackRequest(version_id=v1.id, notes="regression fix")
    await cli_tools_api.rollback_binary_version(
        tool_id=tool.id, body=body, db=session, user=user,
    )

    assert len(captured_audit) == 1
    audit = captured_audit[0]
    assert audit.action == "cli_tool.rollback"
    assert audit.details["from_sha"] == "b" * 64
    assert audit.details["to_sha"] == "a" * 64
    assert audit.details["version_id"] == str(v1.id)
    assert audit.details["notes"] == "regression fix"


@pytest.mark.asyncio
async def test_max_retained_hard_deletes_oldest_binary_on_disk(session, tmp_path, monkeypatch):
    """When version count exceeds the retention cap, the oldest ``.bin``
    file gets hard-deleted via ``BinaryStorage.delete_version``."""
    # Cap the retention to 3 so the test runs with tiny fixtures.
    monkeypatch.setattr(versioning_service, "MAX_RETAINED_VERSIONS", 3)

    tool = await _insert_tool(session)
    storage = BinaryStorage(root=tmp_path)
    tenant_key = "_global"

    # Seed four real ``.bin`` files so eviction has something to delete.
    shas: list[str] = []
    for i in range(4):
        payload = _SHEBANG + bytes([i])
        sha, _ = await storage.write(
            tenant_key=tenant_key,
            tool_id=str(tool.id),
            stream=io.BytesIO(payload),
            max_bytes=1_000_000,
        )
        shas.append(sha)

    # Register them through the service in upload order so uploaded_at
    # is strictly monotonic (each call is a separate datetime.now()).
    # We must space them so sqlite's TIMESTAMP column orders them.
    base_ts = datetime(2026, 4, 1, tzinfo=timezone.utc)
    for i, sha in enumerate(shas):
        await versioning_service.record_new_version(
            session,
            tool,
            sha256=sha,
            size=10 + i,
            original_name=f"v{i}",
            user_id=None,
            binary_storage=storage,
            uploaded_at=base_ts + timedelta(minutes=i),
        )
    await session.flush()

    # Only 3 rows left; the oldest (shas[0]) evicted.
    rows = (await session.execute(
        select(CliToolBinaryVersion).where(CliToolBinaryVersion.tool_id == tool.id)
    )).scalars().all()
    assert len(rows) == 3
    assert shas[0] not in {r.sha256 for r in rows}

    # And the on-disk file for shas[0] is gone; retained files remain.
    assert not storage.resolve(tenant_key, str(tool.id), shas[0]).exists()
    for kept in shas[1:]:
        assert storage.resolve(tenant_key, str(tool.id), kept).exists()


@pytest.mark.asyncio
async def test_list_versions_ordered_desc(session, tmp_path):
    """list_versions returns newest-first regardless of insertion order."""
    tool = await _insert_tool(session)
    storage = BinaryStorage(root=tmp_path)

    base_ts = datetime(2026, 4, 10, tzinfo=timezone.utc)
    shas = ["a" * 64, "b" * 64, "c" * 64]
    # Record them out of temporal order by passing explicit uploaded_at.
    timestamps = [base_ts + timedelta(hours=2), base_ts, base_ts + timedelta(hours=1)]
    for sha, ts in zip(shas, timestamps):
        await versioning_service.record_new_version(
            session,
            tool,
            sha256=sha,
            size=10,
            original_name=sha[:4],
            user_id=None,
            binary_storage=storage,
            uploaded_at=ts,
        )
    await session.flush()

    versions = await versioning_service.list_versions(session, tool)
    # shas[0] has latest ts (base+2h), then shas[2] (base+1h), then shas[1] (base).
    assert [v.sha256 for v in versions] == [shas[0], shas[2], shas[1]]


@pytest.mark.asyncio
async def test_rollback_unknown_version_raises_lookup_error(session, tmp_path):
    """Service layer raises LookupError for a version that doesn't exist
    or belongs to another tool; the API turns it into a 404."""
    tool = await _insert_tool(session)
    storage = BinaryStorage(root=tmp_path)

    await versioning_service.record_new_version(
        session, tool, sha256="a" * 64, size=1, original_name="v",
        user_id=None, binary_storage=storage,
    )

    with pytest.raises(LookupError):
        await versioning_service.rollback_to(session, tool, uuid.uuid4())
