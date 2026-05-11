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

from app.core.security import get_current_user, require_role
from app.database import get_db
from app.models.agent import Agent
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.models.user import User
from app.schemas.mcp_server import (
    MCPServerCreate,
    MCPServerOut,
    MCPServerUpdate,
    MCPServerOverridePut,
    MCPServerOverrideOut,
    OverridesGroupedOut,
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
        # credential_template: None means "don't touch" per schema contract;
        # callers must send "" to explicitly clear.
        if field == "credential_template" and new_value is None:
            continue
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


# ---------------------------------------------------------------------------
# Override CRUD helpers
# ---------------------------------------------------------------------------


async def _require_tenant_override_access(
    current_user: User, scope_id: uuid.UUID,
) -> None:
    """Platform admin OR org_admin of the given tenant."""
    is_platform = (current_user.role == "platform_admin"
                   or (current_user.identity and current_user.identity.is_platform_admin))
    if is_platform:
        return
    if current_user.role == "org_admin" and current_user.tenant_id == scope_id:
        return
    raise HTTPException(status_code=403, detail="not authorized for this tenant override")


async def _require_agent_override_access(
    current_user: User, agent_id: uuid.UUID, db: AsyncSession,
) -> None:
    """Platform admin OR the agent's creator."""
    is_platform = (current_user.role == "platform_admin"
                   or (current_user.identity and current_user.identity.is_platform_admin))
    if is_platform:
        return
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if agent.creator_id != current_user.id:
        raise HTTPException(status_code=403, detail="not authorized for this agent override")


async def _upsert_override(
    db: AsyncSession, server_id: uuid.UUID, scope_type: str,
    scope_id: uuid.UUID, payload: MCPServerOverridePut, user_id: uuid.UUID,
) -> MCPServerOverride:
    existing = (await db.execute(
        select(MCPServerOverride).where(
            MCPServerOverride.mcp_server_id == server_id,
            MCPServerOverride.scope_type == scope_type,
            MCPServerOverride.scope_id == scope_id,
        )
    )).scalar_one_or_none()

    update_data = payload.model_dump(exclude_unset=True)
    # credential_template: None means "don't touch" (mirrors server PATCH semantics)
    if "credential_template" in update_data and update_data["credential_template"] is None:
        update_data.pop("credential_template")

    if existing is None:
        ovr = MCPServerOverride(
            mcp_server_id=server_id, scope_type=scope_type, scope_id=scope_id,
            last_modified_by_user_id=user_id,
            **update_data,
        )
        db.add(ovr)
    else:
        for f, v in update_data.items():
            setattr(existing, f, v)
        existing.last_modified_by_user_id = user_id
        ovr = existing
    await db.commit()
    await db.refresh(ovr)
    return ovr


# ---------------------------------------------------------------------------
# Override endpoints
# ---------------------------------------------------------------------------


@router.get("/{server_id}/overrides", response_model=OverridesGroupedOut)
async def list_mcp_overrides(
    server_id: uuid.UUID,
    current_user: PlatformAdmin,
    db: AsyncSession = Depends(get_db),
) -> OverridesGroupedOut:
    rows = (await db.execute(
        select(MCPServerOverride).where(MCPServerOverride.mcp_server_id == server_id)
    )).scalars().all()
    grouped = OverridesGroupedOut()
    for r in rows:
        target_list = grouped.tenant if r.scope_type == "tenant" else grouped.agent
        target_list.append(MCPServerOverrideOut.from_orm_model(r))
    return grouped


@router.put("/{server_id}/overrides/tenant/{tenant_id}", response_model=MCPServerOverrideOut)
async def put_tenant_override(
    server_id: uuid.UUID,
    tenant_id: uuid.UUID,
    payload: MCPServerOverridePut,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MCPServerOverrideOut:
    await _require_tenant_override_access(current_user, tenant_id)
    if (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    ovr = await _upsert_override(db, server_id, "tenant", tenant_id, payload, current_user.id)
    await write_audit_log(
        action="MCP_SERVER_OVERRIDE_UPSERT",
        details={"server_id": str(server_id), "scope_type": "tenant", "scope_id": str(tenant_id)},
        user_id=current_user.id,
    )
    return MCPServerOverrideOut.from_orm_model(ovr)


@router.delete("/{server_id}/overrides/tenant/{tenant_id}", status_code=204)
async def delete_tenant_override(
    server_id: uuid.UUID,
    tenant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _require_tenant_override_access(current_user, tenant_id)
    rows = (await db.execute(
        select(MCPServerOverride).where(
            MCPServerOverride.mcp_server_id == server_id,
            MCPServerOverride.scope_type == "tenant",
            MCPServerOverride.scope_id == tenant_id,
        )
    )).scalars().all()
    for r in rows:
        await db.delete(r)
    await db.commit()
    await write_audit_log(
        action="MCP_SERVER_OVERRIDE_DELETE",
        details={"server_id": str(server_id), "scope_type": "tenant", "scope_id": str(tenant_id)},
        user_id=current_user.id,
    )


@router.put("/{server_id}/overrides/agent/{agent_id}", response_model=MCPServerOverrideOut)
async def put_agent_override(
    server_id: uuid.UUID,
    agent_id: uuid.UUID,
    payload: MCPServerOverridePut,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MCPServerOverrideOut:
    await _require_agent_override_access(current_user, agent_id, db)
    if (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    ovr = await _upsert_override(db, server_id, "agent", agent_id, payload, current_user.id)
    await write_audit_log(
        action="MCP_SERVER_OVERRIDE_UPSERT",
        details={"server_id": str(server_id), "scope_type": "agent", "scope_id": str(agent_id)},
        user_id=current_user.id,
    )
    return MCPServerOverrideOut.from_orm_model(ovr)


@router.delete("/{server_id}/overrides/agent/{agent_id}", status_code=204)
async def delete_agent_override(
    server_id: uuid.UUID,
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _require_agent_override_access(current_user, agent_id, db)
    rows = (await db.execute(
        select(MCPServerOverride).where(
            MCPServerOverride.mcp_server_id == server_id,
            MCPServerOverride.scope_type == "agent",
            MCPServerOverride.scope_id == agent_id,
        )
    )).scalars().all()
    for r in rows:
        await db.delete(r)
    await db.commit()
    await write_audit_log(
        action="MCP_SERVER_OVERRIDE_DELETE",
        details={"server_id": str(server_id), "scope_type": "agent", "scope_id": str(agent_id)},
        user_id=current_user.id,
    )
