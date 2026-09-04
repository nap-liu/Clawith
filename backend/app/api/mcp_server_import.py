"""One-request company MCP import endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.tools_shared import _require_tenant_tool_admin, _resolve_target_tenant_id
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.mcp_server import MCPServerImport, MCPServerImportOut, MCPServerOut
from app.services.audit_logger import write_audit_log
from app.services.mcp_import_service import (
    MCPImportDraft,
    discover_mcp_import,
    persist_mcp_import,
)
from app.services.user_output import sanitize_user_visible_text

router = APIRouter()


@router.post("/import", response_model=MCPServerImportOut, status_code=status.HTTP_201_CREATED)
async def import_mcp_server(
    payload: MCPServerImport,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MCPServerImportOut:
    """Discover once, then atomically persist the server and complete catalog."""
    target_tenant_id = _resolve_target_tenant_id(
        current_user,
        str(payload.tenant_id) if payload.tenant_id else None,
    )
    _require_tenant_tool_admin(current_user, target_tenant_id)
    actor_id = current_user.id
    draft = MCPImportDraft(
        display_name=payload.display_name,
        tenant_id=target_tenant_id,
        transport=payload.transport,
        base_url_template=payload.base_url_template,
        headers_template=dict(payload.headers_template),
        credential_template=payload.credential_template,
        command_template=payload.command_template,
        args_template=list(payload.args_template),
        env_template=dict(payload.env_template),
        system_prompt_block=payload.system_prompt_block,
        placeholder_allowlist=payload.placeholder_allowlist,
    )

    # Authentication opened a read transaction. End it before waiting on the
    # external MCP server; only immutable actor/tenant/config snapshots survive.
    await db.rollback()
    try:
        discovery = await discover_mcp_import(draft)
    except Exception as exc:
        detail = sanitize_user_visible_text(str(exc))[:300]
        raise HTTPException(status_code=422, detail=detail or "MCP discovery failed") from exc
    if not any(str(item.get("name") or "").strip() for item in discovery.tools):
        raise HTTPException(status_code=422, detail="MCP server returned no tools")

    try:
        server, created = await persist_mcp_import(
            db,
            draft,
            discovery,
            actor_id=actor_id,
        )
        await db.commit()
        await db.refresh(server)
    except Exception:
        await db.rollback()
        raise

    await write_audit_log(
        action="MCP_SERVER_IMPORT",
        details={
            "server_id": str(server.id),
            "tenant_id": str(target_tenant_id) if target_tenant_id else None,
            "transport": draft.transport,
            "discovered": len(discovery.tools),
            "created": created,
        },
        user_id=actor_id,
    )
    return MCPServerImportOut(
        server=MCPServerOut.from_orm_model(server),
        discovered=len(discovery.tools),
        created=created,
    )
