"""CLI tools management API.

Spec §5.4. Endpoints use the /tools/cli subpath to avoid colliding with the
existing app/api/tools.py router at /tools.

    GET    /api/tools/cli                           list
    POST   /api/tools/cli                           create metadata
    POST   /api/tools/cli/{id}/binary               upload binary
    GET    /api/tools/cli/{id}                      detail (env masked)
    PATCH  /api/tools/cli/{id}                      update metadata
    DELETE /api/tools/cli/{id}                      delete
    POST   /api/tools/cli/{id}/test-run             test-run
    GET    /api/tools/cli/{id}/home-usage?user_id=  per-user HOME usage
    DELETE /api/tools/cli/{id}/home-cache?user_id=  clear per-user HOME

Security invariant: binary metadata (sha256, size, original_name,
uploaded_at) is written **only** by the upload endpoint. The update
endpoint's request schema forbids any `binary` or `config` key so admins
cannot repoint the sandbox at a different on-disk blob via PATCH. That
invariant is enforced at the Pydantic layer with ``extra="forbid"``, not
inside the handler body — rejecting at parse time gives attackers zero
surface.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.audit import AuditLog
from app.models.tool import Tool
from app.models.user import User
from app.services.cli_tools.errors import CliToolError
from app.services.cli_tools.schema import (
    BinaryMetadata,
    CliToolConfig,
    RuntimeConfig,
    SandboxConfig,
)
from app.services.cli_tools.state_storage import StateStorage
from app.services.cli_tools.storage import (
    BinaryStorage,
    MagicNumberError,
    SizeLimitExceededError,
)
from app.services.cli_tools import versioning as versioning_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools/cli", tags=["cli-tools"])

_BINARY_MAX_BYTES = 100 * 1024 * 1024
_STORAGE_ROOT = Path("/data/cli_binaries")


def _require_manage(user: User, tool: Optional[Tool] = None) -> None:
    """org_admin of the tool's tenant, or platform_admin anywhere."""
    if user.role == "platform_admin":
        return
    if user.role != "org_admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "org_admin required")
    if tool is not None:
        if tool.tenant_id is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "only platform_admin may manage global tools")
        if tool.tenant_id != user.tenant_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "tool belongs to another tenant")


def _visible(user: User, tool: Tool) -> bool:
    """Scope check: user's own tenant + global."""
    if user.role == "platform_admin":
        return True
    return tool.tenant_id is None or tool.tenant_id == user.tenant_id


def _audit(db: AsyncSession, user: User, action: str, tool: Tool, detail: dict | None = None) -> None:
    db.add(AuditLog(
        user_id=user.id,
        action=action,
        details={
            "resource_type": "tool",
            "resource_id": str(tool.id),
            "name": tool.name,
            "tenant_id": str(tool.tenant_id) if tool.tenant_id else None,
            **(detail or {}),
        },
    ))


class CliToolCreate(BaseModel):
    """Create body. Binary metadata is intentionally absent — uploads go
    through POST /tools/cli/{id}/binary after creation."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    description: str = ""
    parameters_schema: dict = Field(default_factory=dict)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    tenant_id: Optional[uuid.UUID] = None


class CliToolUpdate(BaseModel):
    """Patch body. Deliberately does not accept ``binary`` or ``config``.

    ``extra="forbid"`` means any attempt to slip a ``binary`` (or a full
    ``config``) dict through this endpoint fails at the parser level with
    HTTP 422 — the sandbox's on-disk binary can only be changed by the
    upload endpoint, which writes binary metadata itself and audits it.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = None
    description: Optional[str] = None
    parameters_schema: Optional[dict] = None
    runtime: Optional[RuntimeConfig] = None
    sandbox: Optional[SandboxConfig] = None
    is_active: Optional[bool] = None


class CliToolOut(BaseModel):
    id: uuid.UUID
    name: str
    display_name: str
    description: str
    type: str
    tenant_id: Optional[uuid.UUID]
    is_active: bool
    parameters_schema: dict
    config: dict  # nested shape: {"binary": ..., "runtime": ..., "sandbox": ...}


class TestRunRequest(BaseModel):
    params: dict = Field(default_factory=dict)
    mock_env: Optional[dict[str, str]] = None


class TestRunResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    error_class: Optional[str] = None
    error_message: Optional[str] = None


def _to_out(tool: Tool) -> CliToolOut:
    """Normalise stored config to the new nested shape for the response.

    Reading through ``CliToolConfig.model_validate`` lifts any legacy
    flat keys into their subtree so the API contract is stable even for
    rows that haven't been written yet since the upgrade.
    """
    cfg = CliToolConfig.model_validate(tool.config or {}).model_dump(mode="json")
    return CliToolOut(
        id=tool.id,
        name=tool.name,
        display_name=tool.display_name,
        description=tool.description,
        type=tool.type,
        tenant_id=tool.tenant_id,
        is_active=tool.enabled,
        parameters_schema=tool.parameters_schema,
        config=cfg,
    )


@router.get("", response_model=list[CliToolOut])
async def list_cli_tools(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rows = (await db.execute(select(Tool).where(Tool.type == "cli"))).scalars().all()
    return [_to_out(t) for t in rows if _visible(user, t)]


@router.post("", response_model=CliToolOut, status_code=status.HTTP_201_CREATED)
async def create_cli_tool(
    body: CliToolCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Scope resolution: platform_admin may set global/any tenant;
    # org_admin forces own tenant; member forbidden.
    effective_tenant: Optional[uuid.UUID]
    if user.role == "platform_admin":
        effective_tenant = body.tenant_id
    elif user.role == "org_admin":
        if body.tenant_id not in (None, user.tenant_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "may only create tools in your own tenant")
        effective_tenant = user.tenant_id
    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "org_admin required")

    # Binary metadata starts empty — upload endpoint fills it in.
    initial_config = CliToolConfig(
        binary=BinaryMetadata(),
        runtime=body.runtime,
        sandbox=body.sandbox,
    ).model_dump(mode="json")

    tool = Tool(
        id=uuid.uuid4(),
        name=body.name,
        display_name=body.display_name,
        description=body.description,
        type="cli",
        source="admin",
        parameters_schema=body.parameters_schema,
        config=initial_config,
        tenant_id=effective_tenant,
        enabled=True,
    )
    db.add(tool)
    await db.flush()
    _audit(db, user, "cli_tool.create", tool)
    await db.commit()
    await db.refresh(tool)
    return _to_out(tool)


@router.get("/{tool_id}", response_model=CliToolOut)
async def get_cli_tool(
    tool_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    if not _visible(user, tool):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not visible")
    return _to_out(tool)


@router.patch("/{tool_id}", response_model=CliToolOut)
async def update_cli_tool(
    tool_id: uuid.UUID,
    body: CliToolUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    _require_manage(user, tool)

    diff: dict = {}

    if body.display_name is not None:
        diff["display_name"] = [tool.display_name, body.display_name]
        tool.display_name = body.display_name
    if body.description is not None:
        diff["description"] = ["...", "..."]
        tool.description = body.description
    if body.parameters_schema is not None:
        diff["parameters_schema"] = "updated"
        tool.parameters_schema = body.parameters_schema
    if body.is_active is not None:
        diff["enabled"] = [tool.enabled, body.is_active]
        tool.enabled = body.is_active

    # Load existing config through the nested schema so legacy rows get
    # normalised on first touch. Binary metadata stays whatever the DB
    # had — neither the request body nor this handler touch it.
    if body.runtime is not None or body.sandbox is not None:
        existing = CliToolConfig.model_validate(tool.config or {})
        new_runtime = body.runtime if body.runtime is not None else existing.runtime
        new_sandbox = body.sandbox if body.sandbox is not None else existing.sandbox
        tool.config = CliToolConfig(
            binary=existing.binary,  # preserved; not exposed on PATCH
            runtime=new_runtime,
            sandbox=new_sandbox,
        ).model_dump(mode="json")
        if body.runtime is not None:
            diff["runtime"] = "updated"
        if body.sandbox is not None:
            diff["sandbox"] = "updated"

    _audit(db, user, "cli_tool.update", tool, detail={"changes": list(diff.keys())})
    await db.commit()
    await db.refresh(tool)
    return _to_out(tool)


@router.delete("/{tool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cli_tool(
    tool_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    _require_manage(user, tool)

    # Cascading filesystem GC: remove binary subtree + per-user state subtree
    # BEFORE dropping the DB row, so that if the rmtree raises we bail out
    # rather than leaving the row pointing at nothing. But each individual
    # call swallows its own IO errors (shutil.rmtree(ignore_errors=True))
    # because leaving a row pointing at missing files is still a worse
    # outcome than leaving disk files with no owning row — the latter gets
    # caught by the periodic gc_cli_binaries sweep.
    tenant_key = str(tool.tenant_id) if tool.tenant_id is not None else "_global"
    binary_storage = BinaryStorage(root=_STORAGE_ROOT)
    state_storage = StateStorage()

    t0 = time.monotonic()
    bin_path = str(binary_storage.root / tenant_key / str(tool.id))
    try:
        bin_freed = binary_storage.delete_tool(tenant_key, str(tool.id))
    except Exception as exc:  # defensive: must never block DB delete
        logger.warning("cli-tools.gc: binary cleanup failed for %s: %s", bin_path, exc)
        bin_freed = 0
    bin_ms = int((time.monotonic() - t0) * 1000)
    logger.info(
        "cli-tools.gc",
        extra={
            "operation": "tool",
            "scope": "binary",
            "path": bin_path,
            "freed_bytes": bin_freed,
            "duration_ms": bin_ms,
        },
    )

    t1 = time.monotonic()
    state_path = str(state_storage._root / tenant_key / str(tool.id))  # noqa: SLF001
    try:
        state_freed = state_storage.delete_tool(tool.tenant_id, tool.id)
    except Exception as exc:
        logger.warning("cli-tools.gc: state cleanup failed for %s: %s", state_path, exc)
        state_freed = 0
    state_ms = int((time.monotonic() - t1) * 1000)
    logger.info(
        "cli-tools.gc",
        extra={
            "operation": "tool",
            "scope": "state",
            "path": state_path,
            "freed_bytes": state_freed,
            "duration_ms": state_ms,
        },
    )

    _audit(
        db,
        user,
        "cli_tool.delete",
        tool,
        detail={
            "gc": {
                "binary_path": bin_path,
                "binary_freed_bytes": bin_freed,
                "binary_duration_ms": bin_ms,
                "state_path": state_path,
                "state_freed_bytes": state_freed,
                "state_duration_ms": state_ms,
            }
        },
    )
    # Separate audit row for the cascading GC itself, keyed to resource_type=
    # "cli_tool" per task spec (distinct from the cli_tool.delete row whose
    # `_audit` helper hardcodes resource_type="tool").
    db.add(AuditLog(
        user_id=user.id,
        action="gc.tool",
        details={
            "resource_type": "cli_tool",
            "resource_id": str(tool.id),
            "tenant_id": str(tool.tenant_id) if tool.tenant_id else None,
            "binary_freed_bytes": bin_freed,
            "state_freed_bytes": state_freed,
            "duration_ms": bin_ms + state_ms,
        },
    ))
    await db.delete(tool)
    await db.commit()


@router.post("/{tool_id}/binary", response_model=CliToolOut)
async def upload_binary(
    tool_id: uuid.UUID,
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    _require_manage(user, tool)

    tenant_key = str(tool.tenant_id) if tool.tenant_id is not None else "_global"
    storage = BinaryStorage(root=_STORAGE_ROOT)

    try:
        sha, size = await storage.write(
            tenant_key=tenant_key,
            tool_id=str(tool.id),
            stream=file.file,
            max_bytes=_BINARY_MAX_BYTES,
        )
    except MagicNumberError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unrecognised binary format: {exc}") from exc
    except SizeLimitExceededError as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc

    # Delegate to the versioning service: it inserts a row, flips the
    # previous current flag, rewrites tool.config.binary, and evicts
    # anything past the retention cap (including the old ``.bin`` file).
    await versioning_service.record_new_version(
        db,
        tool,
        sha256=sha,
        size=size,
        original_name=file.filename or "uploaded.bin",
        user_id=user.id,
        binary_storage=storage,
    )

    _audit(db, user, "cli_tool.upload_binary", tool, detail={"sha256": sha, "size": size})
    await db.commit()
    await db.refresh(tool)
    return _to_out(tool)


# ─────────────────────────────────────────────────────────────────────────
# Binary version history + rollback
# ─────────────────────────────────────────────────────────────────────────


class BinaryVersionOut(BaseModel):
    id: uuid.UUID
    tool_id: uuid.UUID
    sha256: str
    size: int
    original_name: str
    uploaded_at: datetime
    uploaded_by_user_id: Optional[uuid.UUID]
    is_current: bool
    notes: Optional[str] = None


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: uuid.UUID
    notes: Optional[str] = Field(default=None, max_length=500)


def _version_to_out(v) -> BinaryVersionOut:
    return BinaryVersionOut(
        id=v.id,
        tool_id=v.tool_id,
        sha256=v.sha256,
        size=v.size,
        original_name=v.original_name,
        uploaded_at=v.uploaded_at,
        uploaded_by_user_id=v.uploaded_by_user_id,
        is_current=v.is_current,
        notes=v.notes,
    )


@router.get("/{tool_id}/versions", response_model=list[BinaryVersionOut])
async def list_binary_versions(
    tool_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List binary versions (newest first) for a CLI tool.

    Tenant-scoped: org_admins only see their own tenant's tools;
    platform_admins see everything. Members are blocked by ``_require_manage``.
    """
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    _require_manage(user, tool)

    rows = await versioning_service.list_versions(db, tool)
    return [_version_to_out(v) for v in rows]


@router.post("/{tool_id}/rollback", response_model=CliToolOut)
async def rollback_binary_version(
    tool_id: uuid.UUID,
    body: RollbackRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Swap the current binary version to a previous upload.

    Only flips flags; does not delete anything. The previously-current
    version stays in history so the admin can roll forward again. The
    same permission check as upload gates access (``_require_manage``).
    """
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    _require_manage(user, tool)

    # Capture the outgoing sha for the audit trail before we flip.
    previous = CliToolConfig.model_validate(tool.config or {})
    from_sha = previous.binary.sha256

    try:
        new_current = await versioning_service.rollback_to(db, tool, body.version_id)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    _audit(
        db,
        user,
        "cli_tool.rollback",
        tool,
        detail={
            "from_sha": from_sha,
            "to_sha": new_current.sha256,
            "version_id": str(new_current.id),
            "notes": body.notes,
        },
    )
    await db.commit()
    await db.refresh(tool)
    return _to_out(tool)


@router.post("/{tool_id}/test-run", response_model=TestRunResponse)
async def test_run_cli_tool(
    tool_id: uuid.UUID,
    body: TestRunRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    _require_manage(user, tool)

    from dataclasses import asdict

    from app.services.cli_tool_executor import CliExecutionAudit, execute_cli_tool

    storage = BinaryStorage(root=_STORAGE_ROOT)
    # Don't pre-pick a runner — executor's factory returns the cached
    # subprocess singleton.

    synthetic_agent_id = uuid.uuid4()

    class _SyntheticAgent:
        id = synthetic_agent_id
        tenant_id = tool.tenant_id if tool.tenant_id is not None else user.tenant_id

    user_context = {
        "id": str(user.id),
        "phone": str(getattr(user, "primary_mobile", "") or ""),
        "email": str(getattr(user, "email", "") or ""),
    }

    # If mock_env supplied, temporarily replace those env keys. Never persist.
    original_config = dict(tool.config or {})
    if body.mock_env:
        normalised = CliToolConfig.model_validate(original_config)
        patched_runtime = normalised.runtime.model_copy(update={
            "env_inject": {**normalised.runtime.env_inject, **body.mock_env},
        })
        tool.config = CliToolConfig(
            binary=normalised.binary,
            runtime=patched_runtime,
            sandbox=normalised.sandbox,
        ).model_dump(mode="json")

    async def _write_audit(audit: CliExecutionAudit) -> None:
        # test-run is an admin-triggered exec. The real user's UUID is
        # safe to attach to AuditLog.user_id (FK); the synthetic agent id
        # is random-per-call, only useful via details['agent_id'].
        db.add(AuditLog(
            user_id=user.id,
            agent_id=None,  # synthetic agent is not a real row
            action="cli_tool.execute",
            details={
                "resource_type": "cli_tool_exec",
                "resource_id": audit.tool_id,
                "source": "test_run",
                **asdict(audit),
            },
        ))

    try:
        result = await execute_cli_tool(
            tool=tool,
            agent=_SyntheticAgent(),
            params=body.params,
            user_context=user_context,
            storage=storage,
            audit_sink=_write_audit,
        )
        await db.commit()  # persist the audit row alongside any other tx work
        return TestRunResponse(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
        )
    except CliToolError as exc:
        # Audit sink already fired in the executor's finally; commit the
        # row even on classified failure so compliance has a record.
        await db.commit()
        return TestRunResponse(
            exit_code=-1,
            stdout="",
            stderr="",
            duration_ms=0,
            error_class=exc.error_class.value,
            error_message=exc.message,
        )
    finally:
        # Never commit the mock-env patch.
        tool.config = original_config


class HomeUsageOut(BaseModel):
    user_id: uuid.UUID
    bytes: int
    mb: int
    within_limit: bool
    limit_mb: int


@router.get("/{tool_id}/home-usage", response_model=HomeUsageOut)
async def get_home_usage(
    tool_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Report HOME usage for one (tool, user). Admin-only.

    Scoped to the tool's tenant: platform_admin everywhere, org_admin
    only within their own tenant. No 'read-your-own' shortcut — this is
    a cache diagnostic surface, not a user-facing feature.
    """
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    _require_manage(user, tool)

    config = CliToolConfig.model_validate(tool.config or {})
    state_storage = StateStorage()
    within, current = state_storage.check_quota(
        tenant_id=tool.tenant_id,
        tool_id=tool.id,
        user_id=user_id,
        limit_mb=config.runtime.home_quota_mb,
    )
    return HomeUsageOut(
        user_id=user_id,
        bytes=current,
        mb=current // (1024 * 1024),
        within_limit=within,
        limit_mb=config.runtime.home_quota_mb,
    )


@router.delete("/{tool_id}/home-cache", status_code=status.HTTP_204_NO_CONTENT)
async def clear_home_cache(
    tool_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Hard-delete the per-(tool, user) HOME subtree. Admin-only.

    Synchronous `rmtree` — typical HOMEs are small enough that blocking
    the request is fine. Returns 204 even when the directory was already
    missing (idempotent: caller's goal of 'gone' is met either way).
    """
    tool = await db.get(Tool, tool_id)
    if tool is None or tool.type != "cli":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "CLI tool not found")
    _require_manage(user, tool)

    state_storage = StateStorage()
    t0 = time.monotonic()
    freed = state_storage.clear_home(
        tenant_id=tool.tenant_id,
        tool_id=tool.id,
        user_id=user_id,
    )
    duration_ms = int((time.monotonic() - t0) * 1000)

    db.add(AuditLog(
        user_id=user.id,
        action="cli_tool.clear_home",
        details={
            "resource_type": "cli_tool",
            "resource_id": str(tool.id),
            "tenant_id": str(tool.tenant_id) if tool.tenant_id else None,
            "target_user_id": str(user_id),
            "freed_bytes": freed,
            "duration_ms": duration_ms,
        },
    ))
    await db.commit()
    logger.info(
        "cli-tools.quota",
        extra={
            "operation": "clear_home",
            "tool_id": str(tool.id),
            "target_user_id": str(user_id),
            "freed_bytes": freed,
            "duration_ms": duration_ms,
        },
    )
