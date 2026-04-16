"""API-level tests for /tools/cli.

The security invariant under test: **binary metadata cannot be set
through PATCH**. Admins write binary sha/size only by uploading a file
through POST /tools/cli/{id}/binary, where the server computes the sha
itself. A malicious or compromised admin trying to slip
``{"config": {"binary_sha256": "..."}}`` or ``{"binary": {...}}``
through the PATCH endpoint must get HTTP 422 at the parser layer.

The handlers are exercised directly (not via ASGI) because the FastAPI
route depends on an async DB dependency that would otherwise need a full
async SQLAlchemy fixture. The Pydantic ``CliToolUpdate`` schema is
imported and model_validate'd — this is exactly what FastAPI does before
the handler runs, so a schema-level reject here is a schema-level reject
in production too.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import cli_tools as cli_tools_api
from app.api.cli_tools import (
    CliToolCreate,
    CliToolUpdate,
    create_cli_tool,
    update_cli_tool,
    upload_binary,
)
from app.services.cli_tools.schema import (
    BinaryMetadata,
    CliToolConfig,
    RuntimeConfig,
    SandboxConfig,
)


# ─────────────────────────────────────────────────────────────────────────
# Fake DB + Tool stubs
# ─────────────────────────────────────────────────────────────────────────


class FakeDB:
    """Minimal stand-in for an AsyncSession.

    Only the methods the CLI-tools handlers call are implemented: ``add``,
    ``get``, ``flush``, ``commit``, ``refresh``, ``delete``. All are
    awaitables where needed.
    """

    def __init__(self, *, tool=None):
        self._tool = tool
        self.added: list = []
        self.committed = False

    async def get(self, _model, _id):
        return self._tool

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _obj) -> None:
        return None

    async def delete(self, _obj) -> None:
        self._tool = None


def _make_tool(**overrides):
    """Build a stand-in Tool ORM row.

    SimpleNamespace keeps the handler code thinking it's an ORM instance
    without needing the real model at test time.
    """
    base = {
        "id": uuid.uuid4(),
        "name": "svc",
        "display_name": "Svc",
        "description": "",
        "type": "cli",
        "tenant_id": None,
        "enabled": True,
        "parameters_schema": {},
        "config": CliToolConfig(
            binary=BinaryMetadata(
                sha256="a" * 64,
                size=1024,
                original_name="svc",
                uploaded_at=datetime.now(timezone.utc),
            ),
            runtime=RuntimeConfig(timeout_seconds=30),
            sandbox=SandboxConfig(),
        ).model_dump(mode="json"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _platform_admin():
    return SimpleNamespace(
        id=uuid.uuid4(),
        role="platform_admin",
        tenant_id=uuid.uuid4(),
        is_active=True,
        primary_mobile="",
        email="a@b",
    )


# ─────────────────────────────────────────────────────────────────────────
# Schema-level rejection (the security invariant)
# ─────────────────────────────────────────────────────────────────────────


def test_update_body_rejects_binary_subtree():
    """PATCH body containing ``binary`` fails parsing with extra='forbid'."""
    with pytest.raises(ValidationError) as exc:
        CliToolUpdate.model_validate({"binary": {"sha256": "f" * 64}})
    # The message should clearly indicate extra-fields-forbidden.
    assert "binary" in str(exc.value).lower()


def test_update_body_rejects_full_config_key():
    """PATCH body containing ``config`` fails parsing — the old attack
    vector went ``{"config": {"binary_sha256": "..."}}``."""
    with pytest.raises(ValidationError) as exc:
        CliToolUpdate.model_validate({"config": {"binary_sha256": "f" * 64}})
    assert "config" in str(exc.value).lower()


def test_update_body_rejects_flat_binary_sha256_key():
    """Catch the direct flat variant too."""
    with pytest.raises(ValidationError):
        CliToolUpdate.model_validate({"binary_sha256": "f" * 64})


def test_update_body_accepts_runtime_and_sandbox():
    """Legitimate admin updates still work."""
    body = CliToolUpdate.model_validate({
        "runtime": {
            "args_template": ["--x"],
            "env_inject": {"K": "v"},
            "timeout_seconds": 60,
            "persistent_home": True,
        },
        "sandbox": {
            "cpu_limit": "2",
            "memory_limit": "1g",
            "network": True,
            "readonly_fs": True,
            "image": None,
        },
    })
    assert body.runtime is not None
    assert body.runtime.timeout_seconds == 60
    assert body.sandbox is not None
    assert body.sandbox.cpu_limit == "2"


def test_create_body_rejects_binary_subtree():
    """Create body must also refuse ``binary`` — uploads happen via the
    dedicated upload endpoint after creation."""
    with pytest.raises(ValidationError):
        CliToolCreate.model_validate({
            "name": "x",
            "display_name": "X",
            "binary": {"sha256": "f" * 64},
        })


def test_create_body_rejects_config_key():
    with pytest.raises(ValidationError):
        CliToolCreate.model_validate({
            "name": "x",
            "display_name": "X",
            "config": {"binary_sha256": "f" * 64},
        })


# ─────────────────────────────────────────────────────────────────────────
# Handler-level behaviour (binary preserved on PATCH, rewritten on upload)
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_runtime_preserves_binary_metadata():
    """After updating runtime, binary.sha256 / size / original_name must
    be byte-identical to the pre-PATCH DB values — the handler never
    touches the binary subtree on a PATCH."""
    sha_before = "a" * 64
    tool = _make_tool(config=CliToolConfig(
        binary=BinaryMetadata(sha256=sha_before, size=1024, original_name="svc",
                              uploaded_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        runtime=RuntimeConfig(timeout_seconds=30),
        sandbox=SandboxConfig(),
    ).model_dump(mode="json"))

    db = FakeDB(tool=tool)
    user = _platform_admin()

    body = CliToolUpdate.model_validate({
        "runtime": {
            "args_template": ["--new"],
            "env_inject": {"X": "y"},
            "timeout_seconds": 90,
            "persistent_home": True,
        },
    })

    out = await update_cli_tool(tool_id=tool.id, body=body, db=db, user=user)
    assert db.committed is True

    # Binary metadata unchanged.
    assert out.config["binary"]["sha256"] == sha_before
    assert out.config["binary"]["size"] == 1024
    assert out.config["binary"]["original_name"] == "svc"

    # Runtime updated.
    assert out.config["runtime"]["args_template"] == ["--new"]
    assert out.config["runtime"]["env_inject"] == {"X": "y"}
    assert out.config["runtime"]["timeout_seconds"] == 90
    assert out.config["runtime"]["persistent_home"] is True


@pytest.mark.asyncio
async def test_patch_runtime_on_legacy_flat_config_normalises_to_nested():
    """A row still holding the M2 flat shape gets normalised on first
    PATCH, and the binary sha survives (critical — we must not lose
    binary metadata during the migration)."""
    sha_before = "b" * 64
    legacy_flat = {
        "binary_sha256": sha_before,
        "binary_size": 2048,
        "binary_original_name": "legacy",
        "binary_uploaded_at": "2026-01-01T00:00:00+00:00",
        "args_template": ["--old"],
        "env_inject": {"A": "1"},
        "timeout_seconds": 15,
        "persistent_home": False,
        "sandbox": {"cpu_limit": "1.0", "memory_limit": "512m",
                    "network": False, "readonly_fs": True, "image": None},
    }
    tool = _make_tool(config=legacy_flat)
    db = FakeDB(tool=tool)
    user = _platform_admin()

    body = CliToolUpdate.model_validate({
        "runtime": {
            "args_template": ["--new"],
            "env_inject": {"B": "2"},
            "timeout_seconds": 60,
            "persistent_home": False,
        },
    })
    out = await update_cli_tool(tool_id=tool.id, body=body, db=db, user=user)

    assert out.config["binary"]["sha256"] == sha_before
    assert out.config["binary"]["size"] == 2048
    assert out.config["runtime"]["args_template"] == ["--new"]
    # Stored config is now nested — flat keys are gone.
    assert "binary_sha256" not in tool.config
    assert "args_template" not in tool.config


@pytest.mark.asyncio
async def test_create_does_not_accept_binary_and_starts_with_empty_binary():
    """Create path never writes binary metadata — the stored row starts
    with an all-None BinaryMetadata."""
    db = FakeDB()
    user = _platform_admin()
    body = CliToolCreate.model_validate({
        "name": "mytool",
        "display_name": "My Tool",
        "runtime": {
            "args_template": ["--go"],
            "env_inject": {},
            "timeout_seconds": 30,
            "persistent_home": False,
        },
        "sandbox": {
            "cpu_limit": "1.0", "memory_limit": "512m",
            "network": False, "readonly_fs": True, "image": None,
        },
    })

    out = await create_cli_tool(body=body, db=db, user=user)
    assert out.config["binary"]["sha256"] is None
    assert out.config["binary"]["size"] is None
    assert out.config["runtime"]["args_template"] == ["--go"]


@pytest.mark.asyncio
async def test_upload_binary_writes_binary_subtree(monkeypatch, tmp_path):
    """POST /tools/cli/{id}/binary is the *only* place binary.sha256 is
    set. After upload, runtime/sandbox stay untouched."""
    # Pre-existing tool with no binary and a non-default runtime.
    tool = _make_tool(config=CliToolConfig(
        binary=BinaryMetadata(),
        runtime=RuntimeConfig(args_template=["--keep"], timeout_seconds=42),
        sandbox=SandboxConfig(cpu_limit="0.5"),
    ).model_dump(mode="json"))
    db = FakeDB(tool=tool)
    user = _platform_admin()

    # Swap the module-level storage root so the real filesystem write
    # lands in tmp_path (and the shebang magic check passes).
    monkeypatch.setattr(cli_tools_api, "_STORAGE_ROOT", tmp_path)

    payload = b"#!/bin/sh\necho ok\n"

    class _FakeUpload:
        filename = "my-binary.sh"
        file = io.BytesIO(payload)

    out = await upload_binary(
        tool_id=tool.id,
        file=_FakeUpload(),  # type: ignore[arg-type]
        db=db,
        user=user,
    )

    # Binary metadata got written.
    assert out.config["binary"]["sha256"] is not None
    assert len(out.config["binary"]["sha256"]) == 64
    assert out.config["binary"]["size"] == len(payload)
    assert out.config["binary"]["original_name"] == "my-binary.sh"
    assert out.config["binary"]["uploaded_at"] is not None

    # Runtime/sandbox preserved.
    assert out.config["runtime"]["args_template"] == ["--keep"]
    assert out.config["runtime"]["timeout_seconds"] == 42
    assert out.config["sandbox"]["cpu_limit"] == "0.5"

    # The on-disk blob exists at the expected content-addressed path.
    expected_blob = Path(tmp_path) / "_global" / str(tool.id) / f"{out.config['binary']['sha256']}.bin"
    assert expected_blob.is_file()


@pytest.mark.asyncio
async def test_patch_404_when_tool_missing():
    db = FakeDB(tool=None)
    user = _platform_admin()
    body = CliToolUpdate.model_validate({"display_name": "x"})
    with pytest.raises(HTTPException) as exc_info:
        await update_cli_tool(tool_id=uuid.uuid4(), body=body, db=db, user=user)
    assert exc_info.value.status_code == 404


# ─────────────────────────────────────────────────────────────────────────
# Post-M2 (dda8c9e) integration: rate_limit / home_quota / backend /
# egress_allowlist round-trip through PATCH.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_runtime_accepts_post_m2_fields():
    """The rate_limit_per_minute + home_quota_mb fields introduced in the
    dda8c9e baseline live under `runtime` in the nested schema. PATCH
    must accept them there, the handler must persist them, and binary
    metadata must stay untouched."""
    sha_before = "a" * 64
    tool = _make_tool(config=CliToolConfig(
        binary=BinaryMetadata(sha256=sha_before, size=1024, original_name="svc",
                              uploaded_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        runtime=RuntimeConfig(timeout_seconds=30, rate_limit_per_minute=60, home_quota_mb=500),
        sandbox=SandboxConfig(),
    ).model_dump(mode="json"))
    db = FakeDB(tool=tool)
    user = _platform_admin()

    body = CliToolUpdate.model_validate({
        "runtime": {
            "args_template": [],
            "env_inject": {},
            "timeout_seconds": 30,
            "persistent_home": True,
            "rate_limit_per_minute": 120,
            "home_quota_mb": 2048,
        },
    })
    out = await update_cli_tool(tool_id=tool.id, body=body, db=db, user=user)

    assert out.config["binary"]["sha256"] == sha_before
    assert out.config["runtime"]["rate_limit_per_minute"] == 120
    assert out.config["runtime"]["home_quota_mb"] == 2048


@pytest.mark.asyncio
async def test_patch_sandbox_accepts_backend_and_egress_allowlist():
    """Post-M2 sandbox additions (backend, egress_allowlist) also flow
    through PATCH untouched."""
    tool = _make_tool()
    db = FakeDB(tool=tool)
    user = _platform_admin()

    body = CliToolUpdate.model_validate({
        "sandbox": {
            "cpu_limit": "1.0",
            "memory_limit": "512m",
            "network": True,
            "readonly_fs": True,
            "image": None,
            "backend": "bwrap",
            "egress_allowlist": ["api.yeyecha.com", "registry.example.com"],
        },
    })
    out = await update_cli_tool(tool_id=tool.id, body=body, db=db, user=user)

    assert out.config["sandbox"]["backend"] == "bwrap"
    assert out.config["sandbox"]["egress_allowlist"] == [
        "api.yeyecha.com",
        "registry.example.com",
    ]


@pytest.mark.asyncio
async def test_read_of_post_m2_flat_config_normalises_and_preserves_values():
    """A row still carrying the dda8c9e flat shape (rate_limit_per_minute
    and home_quota_mb at the top level) gets normalised on every read
    through `_to_out`, and the values survive the migration into the
    runtime subtree."""
    sha_before = "c" * 64
    legacy_post_m2_flat = {
        "binary_sha256": sha_before,
        "binary_size": 4096,
        "binary_original_name": "legacy",
        "binary_uploaded_at": "2026-01-01T00:00:00+00:00",
        "args_template": ["--old"],
        "env_inject": {},
        "timeout_seconds": 30,
        "persistent_home": True,
        "rate_limit_per_minute": 42,
        "home_quota_mb": 777,
        "sandbox": {"cpu_limit": "1.0", "memory_limit": "512m",
                    "network": True, "readonly_fs": True, "image": None,
                    "backend": "docker", "egress_allowlist": ["api.example.com"]},
    }
    tool = _make_tool(config=legacy_post_m2_flat)
    db = FakeDB(tool=tool)
    user = _platform_admin()

    # PATCH only display_name — runtime/sandbox are not sent, but the flat
    # row still normalises on serialisation through `_to_out`, so the
    # output must be nested and the values preserved.
    body = CliToolUpdate.model_validate({"display_name": "Renamed"})
    out = await update_cli_tool(tool_id=tool.id, body=body, db=db, user=user)

    assert out.config["binary"]["sha256"] == sha_before
    assert out.config["runtime"]["rate_limit_per_minute"] == 42
    assert out.config["runtime"]["home_quota_mb"] == 777
    assert out.config["runtime"]["persistent_home"] is True
    assert out.config["sandbox"]["egress_allowlist"] == ["api.example.com"]
