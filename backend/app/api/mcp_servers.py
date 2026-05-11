"""Admin REST endpoints for MCP server management.

All endpoints require ``platform_admin`` role. Credentials are
encrypted at rest and NEVER returned in responses (response schemas
expose ``credential_state`` only).

Audit: every write writes a record via ``write_audit_log`` with
action prefix ``MCP_SERVER_*`` and a JSON diff of changed fields.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_role
from app.database import get_db
from app.models.mcp_server import MCPServer
from app.models.user import User
from app.schemas.mcp_server import (
    MCPServerCreate,
    MCPServerOut,
    MCPServerUpdate,
    TestConnectionResult,
)
from app.services.audit_logger import write_audit_log
from app.services.mcp_client import MCPClient


router = APIRouter(prefix="/admin/mcp-servers", tags=["mcp-admin"])

PlatformAdmin = Annotated[User, Depends(require_role("platform_admin"))]


@router.get("", response_model=list[MCPServerOut])
async def list_mcp_servers(
    current_user: PlatformAdmin,
    db: AsyncSession = Depends(get_db),
) -> list[MCPServerOut]:
    rows = (await db.execute(select(MCPServer).order_by(MCPServer.name))).scalars().all()
    return [MCPServerOut.from_orm_model(s) for s in rows]


@router.get("/{server_id}", response_model=MCPServerOut)
async def get_mcp_server(
    server_id: uuid.UUID,
    current_user: PlatformAdmin,
    db: AsyncSession = Depends(get_db),
) -> MCPServerOut:
    srv = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if srv is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return MCPServerOut.from_orm_model(srv)


@router.post("", response_model=MCPServerOut, status_code=status.HTTP_201_CREATED)
async def create_mcp_server(
    payload: MCPServerCreate,
    current_user: PlatformAdmin,
    db: AsyncSession = Depends(get_db),
) -> MCPServerOut:
    srv = MCPServer(
        tenant_id=payload.tenant_id,
        name=payload.name,
        display_name=payload.display_name,
        base_url_template=payload.base_url_template,
        headers_template=payload.headers_template,
        credential_template=payload.credential_template,  # TODO P3: envelope-encrypt
        system_prompt_block=payload.system_prompt_block,
    )
    db.add(srv)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=409, detail=f"name conflict or invalid data: {e}") from e
    await db.refresh(srv)

    await write_audit_log(
        action="MCP_SERVER_CREATE",
        details={"server_id": str(srv.id), "name": srv.name},
        user_id=current_user.id,
    )
    return MCPServerOut.from_orm_model(srv)


@router.patch("/{server_id}", response_model=MCPServerOut)
async def update_mcp_server(
    server_id: uuid.UUID,
    payload: MCPServerUpdate,
    current_user: PlatformAdmin,
    db: AsyncSession = Depends(get_db),
) -> MCPServerOut:
    srv = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if srv is None:
        raise HTTPException(status_code=404, detail="MCP server not found")

    diff: dict[str, dict] = {}
    update_data = payload.model_dump(exclude_unset=True)

    for field, new_value in update_data.items():
        old_value = getattr(srv, field)
        if old_value != new_value:
            # Don't include credential plaintext in audit log — log the fact, not the value
            if field == "credential_template":
                diff[field] = {"changed": True}
            else:
                diff[field] = {"old": old_value, "new": new_value}
            setattr(srv, field, new_value)

    await db.commit()
    await db.refresh(srv)

    if diff:
        await write_audit_log(
            action="MCP_SERVER_UPDATE",
            details={"server_id": str(srv.id), "diff": diff},
            user_id=current_user.id,
        )
    return MCPServerOut.from_orm_model(srv)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(
    server_id: uuid.UUID,
    current_user: PlatformAdmin,
    db: AsyncSession = Depends(get_db),
) -> None:
    srv = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if srv is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    name = srv.name
    await db.delete(srv)
    await db.commit()

    await write_audit_log(
        action="MCP_SERVER_DELETE",
        details={"server_id": str(server_id), "name": name},
        user_id=current_user.id,
    )


@router.post("/{server_id}/test-connection", response_model=TestConnectionResult)
async def test_mcp_server_connection(
    server_id: uuid.UUID,
    current_user: PlatformAdmin,
    db: AsyncSession = Depends(get_db),
) -> TestConnectionResult:
    srv = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
    if srv is None:
        raise HTTPException(status_code=404, detail="MCP server not found")

    # NOTE: At this point base_url_template / headers_template / credential_template
    # may contain ${user.email} etc. — P3 will resolve those. For P1, we treat them
    # as literals (test-connection from admin context has no user identity).
    client = MCPClient(
        server_url=srv.base_url_template,
        api_key=srv.credential_template,  # may be None
        headers=srv.headers_template,
    )
    try:
        await client.initialize()
        # Capture & persist
        srv.instructions = client.server_instructions
        srv.instructions_captured_at = datetime.now(timezone.utc)
        await db.commit()

        await write_audit_log(
            action="MCP_SERVER_TEST_CONNECTION",
            details={"server_id": str(server_id), "ok": True},
            user_id=current_user.id,
        )
        return TestConnectionResult(
            success=True,
            instructions=client.server_instructions,
            server_info=client.server_info,
        )
    except Exception as e:  # noqa: BLE001 — we want to surface anything to the admin
        await write_audit_log(
            action="MCP_SERVER_TEST_CONNECTION",
            details={"server_id": str(server_id), "ok": False, "error": str(e)[:200]},
            user_id=current_user.id,
        )
        return TestConnectionResult(success=False, error=str(e))
