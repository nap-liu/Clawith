"""CLI tools management API.

Spec §5.4. Endpoints use the /tools/cli subpath to avoid colliding with the
existing app/api/tools.py router at /tools.

    GET    /api/tools/cli                      list
    POST   /api/tools/cli                      create metadata
    POST   /api/tools/cli/{id}/binary          upload binary
    GET    /api/tools/cli/{id}                 detail (env masked)
    PATCH  /api/tools/cli/{id}                 update metadata
    DELETE /api/tools/cli/{id}                 delete
    POST   /api/tools/cli/{id}/test-run        test-run
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.audit import AuditLog
from app.models.tool import Tool
from app.models.user import User
from app.services.cli_tools.errors import CliToolError
from app.services.cli_tools.schema import CliToolConfig
from app.services.cli_tools.storage import (
    BinaryStorage,
    MagicNumberError,
    SizeLimitExceededError,
)

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
    name: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    description: str = ""
    parameters_schema: dict = Field(default_factory=dict)
    config: CliToolConfig = Field(default_factory=CliToolConfig)
    tenant_id: Optional[uuid.UUID] = None


class CliToolUpdate(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    parameters_schema: Optional[dict] = None
    config: Optional[CliToolConfig] = None
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
    config: dict  # env_inject masked


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
    cfg = dict(tool.config or {})
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

    tool = Tool(
        id=uuid.uuid4(),
        name=body.name,
        display_name=body.display_name,
        description=body.description,
        type="cli",
        source="admin",
        parameters_schema=body.parameters_schema,
        config=body.config.model_dump(mode="json"),
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
    if body.config is not None:
        existing = dict(tool.config or {})
        incoming = body.config.model_dump(mode="json")
        # Preserve binary metadata unless the incoming body explicitly overrides.
        for preserved in ("binary_sha256", "binary_size", "binary_original_name", "binary_uploaded_at"):
            if incoming.get(preserved) is None:
                incoming[preserved] = existing.get(preserved)
        tool.config = incoming
        diff["config"] = "updated"

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
    _audit(db, user, "cli_tool.delete", tool)
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

    cfg = dict(tool.config or {})
    cfg["binary_sha256"] = sha
    cfg["binary_size"] = size
    cfg["binary_original_name"] = file.filename or "uploaded.bin"
    cfg["binary_uploaded_at"] = datetime.now(timezone.utc).isoformat()
    tool.config = cfg

    _audit(db, user, "cli_tool.upload_binary", tool, detail={"sha256": sha, "size": size})
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

    from app.services.cli_tool_executor import execute_cli_tool
    from app.services.sandbox.local.binary_runner import BinaryRunner

    storage = BinaryStorage(root=_STORAGE_ROOT)
    runner = BinaryRunner(image="clawith-cli-sandbox:stable")

    class _SyntheticAgent:
        id = uuid.uuid4()
        tenant_id = tool.tenant_id if tool.tenant_id is not None else user.tenant_id

    user_context = {
        "id": str(user.id),
        "phone": str(getattr(user, "primary_mobile", "") or ""),
        "email": str(getattr(user, "email", "") or ""),
    }

    # If mock_env supplied, temporarily replace those env keys. Never persist.
    original_config = dict(tool.config or {})
    if body.mock_env:
        patched_env = dict(original_config.get("env_inject", {}))
        patched_env.update(body.mock_env)
        tool.config = {**original_config, "env_inject": patched_env}

    try:
        result = await execute_cli_tool(
            tool=tool,
            agent=_SyntheticAgent(),
            params=body.params,
            user_context=user_context,
            storage=storage,
            runner=runner,
        )
        return TestRunResponse(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
        )
    except CliToolError as exc:
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
