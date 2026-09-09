"""Shared MCP server permission checks used by:
- /api/admin/mcp-servers PATCH/dry-run/test-connection (mcp_servers.py)
- /api/tools/mcp-server PUT bulk-update (tools.py)

One source of truth so the role taxonomy stays in lockstep across endpoints.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp_server import MCPServer
from app.models.user import User


def is_platform_admin(user: User) -> bool:
    """Recognize platform_admin via role string OR identity flag.
    The identity-flag path is a backstop for users elevated through identity
    without having their `users.role` column updated.
    """
    if user.role == "platform_admin":
        return True
    if user.identity is not None and getattr(user.identity, "is_platform_admin", False):
        return True
    return False


def can_edit_server(user: User, server: MCPServer) -> bool:
    """True if `user` may edit `server`.

    Allowed:
    - platform_admin
    - server creator (server.created_by_user_id)
    - same-tenant org_admin or agent_admin
    """
    if is_platform_admin(user):
        return True
    if server.created_by_user_id is not None and server.created_by_user_id == user.id:
        return True
    if (
        user.role in ("org_admin", "agent_admin")
        and server.tenant_id is not None
        and user.tenant_id == server.tenant_id
    ):
        return True
    return False


def assert_can_edit_server(user: User, server: MCPServer) -> None:
    """Raise 403 unless `user` may edit `server`. See `can_edit_server`."""
    if not can_edit_server(user, server):
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to modify this MCP server",
        )


def assert_can_manage_tenant_tools(user: User, tenant_id) -> None:
    """Allow platform admins everywhere and org admins only in their tenant."""
    if is_platform_admin(user):
        return
    if (
        user.role == "org_admin"
        and tenant_id is not None
        and user.tenant_id == tenant_id
    ):
        return
    raise HTTPException(
        status_code=403,
        detail="Organization admin required",
    )


def assert_can_manage_company_server(user: User, server: MCPServer) -> None:
    """Restrict tenant-wide MCP definitions to company administrators."""
    assert_can_manage_tenant_tools(user, server.tenant_id)


async def assert_can_patch_server(
    user: User,
    server: MCPServer,
    db: AsyncSession,
) -> None:
    """Shared definitions require catalog authority; private ones require Agent management."""
    from app.services.mcp_catalog_policy import shared_catalog

    if await shared_catalog(db, server):
        assert_can_manage_company_server(user, server)
    else:
        from sqlalchemy import select
        from app.models.tool import AgentTool, Tool
        from app.core.permissions import check_agent_access
        from app.services.llm.failure_outcome import render_message

        if is_platform_admin(user):
            return
        owner_ids = (await db.scalars(select(AgentTool.agent_id).join(
            Tool, Tool.id == AgentTool.tool_id,
        ).where(Tool.mcp_server_id == server.id).distinct())).all()
        if not owner_ids:
            assert_can_manage_company_server(user, server)
        for owner_id in owner_ids:
            _, access = await check_agent_access(db, user, owner_id)
            if access != "manage":
                raise HTTPException(403, detail=render_message("mcpAccess.manageRequired"))


def assert_can_create_server_in_tenant(
    user: User,
    tenant_id: "uuid.UUID | None",  # noqa: F821  — uuid imported lazily by callers
) -> None:
    """Raise 403 unless `user` may CREATE a new mcp_servers row in the given tenant.

    Used by code paths that auto-create server rows on bulk-update (legacy
    /api/tools/mcp-server). Without this gate, any logged-in user could
    introduce a brand-new MCP server in any tenant by simply naming it.
    """
    assert_can_manage_tenant_tools(user, tenant_id)
