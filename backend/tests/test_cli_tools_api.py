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
)


# ─────────────────────────────────────────────────────────────────────────
# Fake DB + Tool stubs
# ─────────────────────────────────────────────────────────────────────────


class FakeDB:
    """Minimal stand-in for an AsyncSession.

    Only the methods the CLI-tools handlers call are implemented: ``add``,
    ``get``, ``flush``, ``commit``, ``refresh``, ``delete``, ``execute``.
    All are awaitables where needed.

    ``execute`` is a no-op stub returning an object exposing
    ``scalars().all()`` / ``scalar_one_or_none()`` / ``scalar()`` — just
    enough for the versioning-service UPDATE/SELECT calls to short-circuit
    to an empty result when wired into these schema-only tests.
    """

    def __init__(self, *, tool=None):
        self._tool = tool
        self.added: list = []
        self.committed = False
        self.deleted: list = []

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

    async def delete(self, obj) -> None:
        # Keep track for tests that want to observe eviction without
        # clobbering self._tool (the versioning service may call delete
        # on CliToolBinaryVersion rows).
        if obj is self._tool:
            self._tool = None
        self.deleted.append(obj)

    async def execute(self, _stmt):
        class _Scalars:
            def all(self):
                return []

            def scalar_one_or_none(self):
                return None

            def scalar(self):
                return None

        class _Result:
            def scalars(self):
                return _Scalars()

            def scalar_one_or_none(self):
                return None

        return _Result()


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
    """Legitimate admin updates still work.

    Legacy sandbox fields (network/readonly_fs/image) are silently
    dropped on read — exercised here to pin the compat behaviour for
    PATCH bodies coming from old UIs / scripts.
    """
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
    assert body.sandbox.memory_limit == "1g"


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

    # v5: runtime.env_inject lifted into config["env"]; no "runtime" subtree stored.
    assert out.config["env"] == {"X": "y"}
    assert "runtime" not in out.config


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
    # v5: runtime.env_inject lifted into config["env"]; no "runtime" subtree stored.
    assert out.config["env"] == {"B": "2"}
    # Stored config is now v5 — flat keys and old subtrees are gone.
    assert "binary_sha256" not in tool.config
    assert "args_template" not in tool.config
    assert "runtime" not in tool.config


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
    # v5: only binary + env in config; runtime/sandbox dropped at write time.
    assert set(out.config.keys()) == {"binary", "env"}
    assert out.config["env"] == {}


@pytest.mark.asyncio
async def test_upload_binary_writes_binary_subtree(monkeypatch, tmp_path):
    """POST /tools/cli/{id}/binary is the *only* place binary.sha256 is
    set. After upload, v5 config has only binary + env; env is untouched."""
    # Pre-existing tool with no binary and some env vars.
    tool = _make_tool(config=CliToolConfig(
        binary=BinaryMetadata(),
        env={"TOOL_ENV": "pre-upload"},
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

    # v5: only binary + env; no runtime/sandbox subtrees.
    assert set(out.config.keys()) == {"binary", "env"}
    # env is unchanged by upload.
    assert out.config["env"] == {"TOOL_ENV": "pre-upload"}

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
    """PATCH body with old post-M2 runtime fields (rate_limit_per_minute,
    home_quota_mb) is accepted by the shim schema and silently dropped in v5.
    Binary metadata stays untouched; env_inject (if provided) lands in env."""
    sha_before = "a" * 64
    tool = _make_tool(config=CliToolConfig(
        binary=BinaryMetadata(sha256=sha_before, size=1024, original_name="svc",
                              uploaded_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
    ).model_dump(mode="json"))
    db = FakeDB(tool=tool)
    user = _platform_admin()

    body = CliToolUpdate.model_validate({
        "runtime": {
            "args_template": [],
            "env_inject": {"RATE_KEY": "v"},
            "timeout_seconds": 30,
            "persistent_home": True,
            "rate_limit_per_minute": 120,
            "home_quota_mb": 2048,
        },
    })
    out = await update_cli_tool(tool_id=tool.id, body=body, db=db, user=user)

    # Binary metadata preserved.
    assert out.config["binary"]["sha256"] == sha_before
    # v5: only binary + env; rate_limit/home_quota/timeout retired.
    assert set(out.config.keys()) == {"binary", "env"}
    assert out.config["env"] == {"RATE_KEY": "v"}
    assert "runtime" not in out.config


@pytest.mark.asyncio
async def test_patch_sandbox_silently_drops_legacy_fields():
    """v5 dropped the entire sandbox subtree. Old UIs / scripts that still
    POST sandbox fields must not break — the shim schema silently swallows
    them and they are not stored. The stored config only has binary + env."""
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
            "egress_allowlist": ["api.example.com", "registry.example.com"],
        },
    })
    out = await update_cli_tool(tool_id=tool.id, body=body, db=db, user=user)

    # v5: sandbox subtree retired entirely; only binary + env survive.
    assert set(out.config.keys()) == {"binary", "env"}
    assert "sandbox" not in out.config
    assert "cpu_limit" not in out.config


@pytest.mark.asyncio
async def test_read_of_post_m2_flat_config_normalises_and_preserves_values():
    """A row still carrying the dda8c9e flat shape (env_inject at the top
    level) gets normalised on every read through `_to_out`, and env_inject
    lifts into env.  All other flat runtime/sandbox keys are silently dropped
    in v5 (rate_limit, home_quota, persistent_home, cpu_limit, etc.)."""
    sha_before = "c" * 64
    legacy_post_m2_flat = {
        "binary_sha256": sha_before,
        "binary_size": 4096,
        "binary_original_name": "legacy",
        "binary_uploaded_at": "2026-01-01T00:00:00+00:00",
        "args_template": ["--old"],
        "env_inject": {"LEGACY_KEY": "abc"},
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
    # output must be v5 shape.
    body = CliToolUpdate.model_validate({"display_name": "Renamed"})
    out = await update_cli_tool(tool_id=tool.id, body=body, db=db, user=user)

    assert out.config["binary"]["sha256"] == sha_before
    assert out.config["binary"]["size"] == 4096
    # v5: flat env_inject lifts into env; all other runtime/sandbox keys dropped.
    assert out.config["env"] == {"LEGACY_KEY": "abc"}
    assert set(out.config.keys()) == {"binary", "env"}
    assert "runtime" not in out.config
    assert "sandbox" not in out.config


# ─────────────────────────────────────────────────────────────────────────
# Binary version history + rollback endpoints
#
# These tests exercise the API handlers with FakeDB/stub service so we
# cover the glue (404s, auth, audit shape) without standing up a full
# SQLAlchemy engine. The service-layer behaviour is tested separately in
# test_cli_tools_versioning.py.
# ─────────────────────────────────────────────────────────────────────────


from app.api.cli_tools import (  # noqa: E402
    RollbackRequest,
    list_binary_versions,
    rollback_binary_version,
)


def _member_user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        role="member",
        tenant_id=uuid.uuid4(),
        is_active=True,
    )


@pytest.mark.asyncio
async def test_get_versions_returns_history(monkeypatch):
    """GET /versions returns the service-layer list, mapped to the
    BinaryVersionOut wire shape, newest first."""
    tool = _make_tool()
    db = FakeDB(tool=tool)
    user = _platform_admin()

    version_rows = [
        SimpleNamespace(
            id=uuid.uuid4(),
            tool_id=tool.id,
            sha256="a" * 64,
            size=10,
            original_name="v1",
            uploaded_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            uploaded_by_user_id=None,
            is_current=False,
            notes=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            tool_id=tool.id,
            sha256="b" * 64,
            size=20,
            original_name="v2",
            uploaded_at=datetime(2026, 4, 2, tzinfo=timezone.utc),
            uploaded_by_user_id=user.id,
            is_current=True,
            notes="ship",
        ),
    ]

    async def _fake_list(_db, _tool):
        return version_rows

    from app.services.cli_tools import versioning as versioning_service

    monkeypatch.setattr(versioning_service, "list_versions", _fake_list)

    out = await list_binary_versions(tool_id=tool.id, db=db, user=user)

    assert len(out) == 2
    assert out[0].sha256 == "a" * 64
    assert out[1].is_current is True
    assert out[1].notes == "ship"


@pytest.mark.asyncio
async def test_get_versions_404_when_tool_missing(monkeypatch):
    db = FakeDB(tool=None)
    user = _platform_admin()
    with pytest.raises(HTTPException) as exc_info:
        await list_binary_versions(tool_id=uuid.uuid4(), db=db, user=user)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_rollback_endpoint_requires_manage_permission():
    """A member user (not org_admin / not platform_admin) gets 403."""
    tool = _make_tool()
    db = FakeDB(tool=tool)
    user = _member_user()

    body = RollbackRequest(version_id=uuid.uuid4())
    with pytest.raises(HTTPException) as exc_info:
        await rollback_binary_version(tool_id=tool.id, body=body, db=db, user=user)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_rollback_unknown_version_returns_404(monkeypatch):
    """LookupError from the service layer becomes HTTP 404."""
    tool = _make_tool()
    db = FakeDB(tool=tool)
    user = _platform_admin()

    async def _raise_lookup(_db, _tool, _vid):
        raise LookupError("nope")

    from app.services.cli_tools import versioning as versioning_service

    monkeypatch.setattr(versioning_service, "rollback_to", _raise_lookup)

    body = RollbackRequest(version_id=uuid.uuid4())
    with pytest.raises(HTTPException) as exc_info:
        await rollback_binary_version(tool_id=tool.id, body=body, db=db, user=user)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_rollback_body_rejects_extra_keys():
    """The rollback body is strict — no admin can smuggle extra fields."""
    with pytest.raises(ValidationError):
        RollbackRequest.model_validate({
            "version_id": str(uuid.uuid4()),
            "sha256": "a" * 64,  # not allowed
        })


@pytest.mark.asyncio
async def test_upload_binary_413_uses_configured_cap(monkeypatch, tmp_path):
    """The upload endpoint enforces whatever ``_BINARY_MAX_BYTES`` is set to,
    returning 413 with the byte count in the detail.

    This is the production failure path: a binary above the cap yields
    ``413 binary exceeds <N> bytes``. We shrink the module-level cap (the
    same knob ``CLI_BINARY_MAX_BYTES`` drives) so a tiny shebang script is
    already oversize, proving the limit is honoured end-to-end.
    """
    tool = _make_tool(config=CliToolConfig(binary=BinaryMetadata()).model_dump(mode="json"))
    db = FakeDB(tool=tool)
    user = _platform_admin()
    monkeypatch.setattr(cli_tools_api, "_STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(cli_tools_api, "_BINARY_MAX_BYTES", 8)

    payload = b"#!/bin/sh\necho hello\n"  # > 8 bytes

    class _FakeUpload:
        filename = "big.sh"
        file = io.BytesIO(payload)

    with pytest.raises(HTTPException) as exc_info:
        await upload_binary(
            tool_id=tool.id,
            file=_FakeUpload(),  # type: ignore[arg-type]
            db=db,
            user=user,
        )
    assert exc_info.value.status_code == 413
    assert "exceeds 8 bytes" in str(exc_info.value.detail)


def test_binary_max_bytes_configurable_via_env(monkeypatch):
    """``_BINARY_MAX_BYTES`` reads ``CLI_BINARY_MAX_BYTES`` at import,
    defaulting to 100 MiB when unset (mirrors ``MAX_SKILL_SIZE``).

    Reloads the module under different env so the override path is exercised,
    then restores the default so sibling tests see the unpatched constant.
    """
    import importlib

    try:
        monkeypatch.delenv("CLI_BINARY_MAX_BYTES", raising=False)
        importlib.reload(cli_tools_api)
        assert cli_tools_api._BINARY_MAX_BYTES == 100 * 1024 * 1024

        monkeypatch.setenv("CLI_BINARY_MAX_BYTES", "314572800")  # 300 MiB
        importlib.reload(cli_tools_api)
        assert cli_tools_api._BINARY_MAX_BYTES == 314572800
    finally:
        # Restore the module to its default-env state regardless of outcome,
        # so later tests in this process don't inherit a patched constant.
        monkeypatch.delenv("CLI_BINARY_MAX_BYTES", raising=False)
        importlib.reload(cli_tools_api)
