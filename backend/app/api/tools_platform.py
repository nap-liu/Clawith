"""Platform-wide tool management routes."""

import uuid

from fastapi import Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.tools_models import BulkToolUpdateItem, MCPServerUpdate, ToolCreate, ToolUpdate
from app.api.tools_shared import (
    _can_view_unmasked_company_config,
    _encrypt_sensitive_fields,
    _feature_visible_tool_clause,
    _reject_required_tool_disable,
    _require_feature_visible_tool,
    _require_platform_admin,
    _require_tenant_tool_admin,
    _resolve_target_tenant_id,
    _tool_availability,
    get_tool_company_config,
    mask_sensitive_fields,
    meaningful_config,
    router,
    set_tenant_tool_config,
    tool_is_required,
)
from app.core.security import get_current_user
from app.database import get_db
from app.models.mcp_server import MCPServer
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.user_output import sanitize_user_visible_text


@router.get("")
async def list_tools(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List platform tools scoped by tenant (builtin + tenant-specific)."""
    query = (
        select(Tool, MCPServer.display_name.label("mcp_server_display_name"))
        .outerjoin(MCPServer, MCPServer.id == Tool.mcp_server_id)
        .where(_feature_visible_tool_clause(), Tool.source.in_(["builtin", "admin"]))
        .order_by(Tool.category, Tool.name)
    )
    # Scope by tenant: show builtin (tenant_id is NULL) + tenant-specific tools
    target_tenant_id = _resolve_target_tenant_id(current_user, tenant_id)
    if target_tenant_id:
        from sqlalchemy import or_ as _or
        query = query.where(_or(Tool.tenant_id == None, Tool.tenant_id == target_tenant_id))
    result = await db.execute(query)
    response = []
    for t, mcp_server_display_name in result.all():
        company_config = await get_tool_company_config(db, t, target_tenant_id)
        visible_company_config = (
            company_config
            if _can_view_unmasked_company_config(current_user, target_tenant_id)
            else mask_sensitive_fields(company_config, t.config_schema)
        )
        response.append({
            "id": str(t.id),
            "name": t.name,
            "display_name": t.display_name,
            "description": t.description,
            "type": t.type,
            "category": t.category,
            "icon": t.icon,
            "parameters_schema": t.parameters_schema,
            "mcp_server_url": t.mcp_server_url,
            "mcp_server_name": t.mcp_server_name,
            "mcp_server_display_name": mcp_server_display_name,
            "mcp_server_id": str(t.mcp_server_id) if t.mcp_server_id else None,
            "mcp_tool_name": t.mcp_tool_name,
            "enabled": True if tool_is_required(t.name) else t.enabled,
            "is_default": t.is_default,
            "source": t.source,
            "config": visible_company_config,
            "config_schema": t.config_schema or {},
            "created_at": t.created_at.isoformat() if t.created_at else None,
            **_tool_availability(t.name),
        })
    return response


@router.post("")
async def create_tool(
    data: ToolCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new tool (typically MCP).

    The tool is scoped to the target tenant, which defaults to the caller's
    own tenant but can be overridden via data.tenant_id. This allows platform
    admins to import MCP tools while viewing another company's settings page.
    """
    # Resolve target tenant: explicit payload value takes priority so that
    # platform admins importing tools for another company work correctly.
    target_tenant_id = _resolve_target_tenant_id(current_user, data.tenant_id)
    _require_tenant_tool_admin(current_user, target_tenant_id)

    # Unique name check is scoped per tenant to avoid cross-tenant collisions.
    existing = await db.execute(
        select(Tool).where(Tool.name == data.name, Tool.tenant_id == target_tenant_id)
    )
    if existing.scalar_one_or_none():
        safe_name = sanitize_user_visible_text(data.name)
        raise HTTPException(status_code=400, detail=f"Tool '{safe_name}' already exists")

    tool = Tool(
        name=data.name,
        display_name=data.display_name,
        description=data.description,
        type=data.type,
        category=data.category,
        icon=data.icon,
        parameters_schema=data.parameters_schema,
        mcp_server_url=data.mcp_server_url,
        mcp_server_name=data.mcp_server_name,
        mcp_tool_name=data.mcp_tool_name,
        is_default=data.is_default,
        tenant_id=target_tenant_id,
        source="admin",
    )
    db.add(tool)
    await db.commit()
    await db.refresh(tool)
    return {"id": str(tool.id), "name": tool.name}


# NOTE: Literal path routes (/bulk, /mcp-server) MUST be defined BEFORE
# parameterized routes (/{tool_id}) to avoid older FastAPI/Starlette versions
# matching "bulk" as a uuid.UUID path parameter and returning 422.
@router.put("/bulk")
async def update_tools_bulk(
    updates: list[BulkToolUpdateItem],
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk update the enabled status of multiple tools."""
    _require_platform_admin(current_user)
    tool_ids = [uuid.UUID(u.tool_id) for u in updates]
    result = await db.execute(
        select(Tool).where(Tool.id.in_(tool_ids), _feature_visible_tool_clause())
        .order_by(Tool.id).with_for_update()
    )
    tools_map = {str(t.id): t for t in result.scalars().all()}

    # Validate the complete request before mutating anything so a mixed batch
    # cannot partially disable ordinary tools before failing on a required one.
    for update in updates:
        tool = tools_map.get(update.tool_id)
        if tool:
            _reject_required_tool_disable(tool, update.enabled)

    for update in updates:
        if update.tool_id in tools_map:
            tools_map[update.tool_id].enabled = update.enabled

    await db.commit()
    return {"ok": True}


# ─── MCP Server-level Credential Management ────────────────
# NOTE: This route must appear BEFORE PUT /{tool_id} or FastAPI will match
# "mcp-server" as a tool_id UUID and return 422.
@router.put("/mcp-server")
async def update_mcp_server(
    data: MCPServerUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-update the Server URL and API Key for all tools from an MCP server.

    All tools sharing the same mcp_server_name under the target tenant are
    updated atomically. The API Key is stored encrypted in tool.config so
    the agent runner can resolve it at execution time without re-configuring
    each tool individually.

    Authentication priority at runtime (handled by MCPClient):
    1. tool.config['api_key'] — sent as Authorization: Bearer header.
    2. URL query param (e.g. ?tavilyApiKey=xxx) — extracted from the URL
       and converted to Bearer by MCPClient automatically.
    """
    # Resolve target tenant
    target_tenant_id: uuid.UUID | None = None
    if data.tenant_id:
        try:
            target_tenant_id = uuid.UUID(data.tenant_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid tenant_id format")
    else:
        target_tenant_id = current_user.tenant_id

    # Permission gate. Two cases:
    # 1. mcp_servers row exists → must be allowed to edit it (assert_can_edit_server)
    # 2. mcp_servers row doesn't exist BUT tools do (legacy migration state where
    #    upsert_mcp_server_from_tools will create the row) → must be allowed to
    #    create in the target tenant. Without this, a non-admin member could
    #    silently introduce a brand-new MCP server row by calling PUT with a
    #    name that doesn't exist in mcp_servers yet.
    # We look up by name across tenants (rather than restricting to target_tenant)
    # so cross-tenant callers fall into the edit branch (and get 403) instead of
    # the create branch.
    from app.services.mcp_permissions import (
        assert_can_create_server_in_tenant,
        assert_can_patch_server,
    )
    srv_q = await db.execute(
        select(MCPServer).where(MCPServer.name == data.server_name).limit(1)
    )
    srv = srv_q.scalar_one_or_none()
    if srv is not None:
        await assert_can_patch_server(current_user, srv, db)
    else:
        assert_can_create_server_in_tenant(current_user, target_tenant_id)

    from app.services.mcp_catalog_locks import lock_mcp_catalogs

    # The legacy bridge can move tools to an existing same-tenant URL catalog.
    # Resolve both ends before writing tools, then lock in canonical order.
    target_server_id = await db.scalar(select(MCPServer.id).where(
        MCPServer.tenant_id == target_tenant_id,
        MCPServer.base_url_template == data.server_url,
    ))
    await lock_mcp_catalogs(db, {srv.id if srv else None, target_server_id} - {None})

    # Load all tools from this server under the target tenant
    result = await db.execute(
        select(Tool).where(
            Tool.mcp_server_name == data.server_name,
            Tool.tenant_id == target_tenant_id,
        )
    )
    tools = result.scalars().all()
    if not tools:
        raise HTTPException(
            status_code=404,
            detail=f"No tools found for server '{data.server_name}'",
        )

    # NEW: validate prompt placeholders BEFORE writing anything
    if data.system_prompt_block is not None:
        from app.services.placeholder_engine import PROMPT_SAFE_ROOTS, detect_used_roots
        used = detect_used_roots(data.system_prompt_block)
        bad = used - PROMPT_SAFE_ROOTS
        if bad:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"system_prompt_block contains disallowed placeholder roots: "
                    f"{sorted(bad)}. ${{user.*}} and ${{params.*}} can only be used "
                    f"in URL/headers/credential templates, not in prompt text."
                ),
            )

    for tool in tools:
        tool.mcp_server_url = data.server_url
        if data.api_key is not None:
            # Merge api_key into existing config (other keys preserved) and encrypt
            current_config = dict(tool.config or {})
            current_config["api_key"] = data.api_key
            tool.config = _encrypt_sensitive_fields(current_config, tool.config_schema)
        # If api_key is None (not provided), preserve the existing encrypted key

    # NEW: bridge to mcp_servers table
    from app.services.mcp_server_service import upsert_mcp_server_from_tools
    mcp_server_id = await upsert_mcp_server_from_tools(
        db,
        tenant_id=target_tenant_id,
        server_url=data.server_url,
        server_name=data.server_name,
        system_prompt_block=data.system_prompt_block,
        headers_template=data.headers_template,
        api_key=data.api_key,
        created_by_user_id=current_user.id,
    )
    # Link tools rows to the upserted mcp_servers row
    for tool in tools:
        if tool.mcp_server_id != mcp_server_id:
            tool.mcp_server_id = mcp_server_id

    await db.commit()
    return {"ok": True, "updated": len(tools), "mcp_server_id": str(mcp_server_id)}


@router.put("/{tool_id}")
async def update_tool(
    tool_id: uuid.UUID,
    data: ToolUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a tool."""
    result = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool = _require_feature_visible_tool(result.scalar_one_or_none())

    update_data = data.model_dump(exclude_unset=True)
    target_tenant_id = _resolve_target_tenant_id(current_user, update_data.pop("tenant_id", None))
    _reject_required_tool_disable(tool, update_data.get("enabled"))

    # Builtin metadata and global enabled state affect every tenant. A company
    # org admin may only update that company's builtin config. Tenant-owned
    # admin tools remain manageable by their own org admin.
    config_only_builtin_update = tool.source == "builtin" and set(update_data) <= {"config"}
    if config_only_builtin_update:
        _require_tenant_tool_admin(current_user, target_tenant_id)
    elif tool.source == "builtin" or tool.tenant_id is None:
        _require_platform_admin(current_user)
    else:
        _require_tenant_tool_admin(current_user, tool.tenant_id)

    config_changed = "config" in update_data
    if "config" in update_data:
        config_value = meaningful_config(update_data.pop("config") or {})
        if tool.source == "builtin":
            if not target_tenant_id:
                raise HTTPException(status_code=400, detail="tenant_id is required to configure builtin tools")
            await set_tenant_tool_config(db, target_tenant_id, tool.name, config_value, tool.config_schema)
        else:
            update_data["config"] = _encrypt_sensitive_fields(config_value, tool.config_schema)

    for field, value in update_data.items():
        setattr(tool, field, value)
    await db.commit()
    if config_changed:
        from app.services.agent_tools import invalidate_tool_config_cache

        invalidate_tool_config_cache(None, tool.name)
    return {"ok": True}


@router.delete("/{tool_id}")
async def delete_tool(
    tool_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a tool (only non-builtin)."""
    result = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool = _require_feature_visible_tool(result.scalar_one_or_none())
    if tool.type == "builtin":
        raise HTTPException(status_code=400, detail="Cannot delete builtin tools")
    if tool.tenant_id is None:
        _require_platform_admin(current_user)
    else:
        _require_tenant_tool_admin(current_user, tool.tenant_id)

    from app.services.mcp_catalog_locks import lock_tool_catalog

    await lock_tool_catalog(db, tool_id)
    await db.execute(delete(AgentTool).where(AgentTool.tool_id == tool_id))
    await db.delete(tool)
    await db.commit()
    return {"ok": True}
