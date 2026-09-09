"""Agent and tenant boundaries for MCP configuration access."""

import uuid
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.core.permissions import check_agent_access
from app.services.mcp_permissions import is_platform_admin as _is_platform_admin
from app.services.mcp_catalog_policy import shared_catalog
from app.services.llm.failure_outcome import render_message

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


async def require_tenant_server_access(db, user, tenant_id, server_id):
    await _require_tenant_override_access(user, tenant_id)
    server = await db.get(MCPServer, server_id)
    if server is None or server.tenant_id not in (None, tenant_id):
        raise HTTPException(404, detail=render_message("mcpAccess.unavailable"))
    return server


async def _require_agent_override_access(
    current_user: User, agent_id: uuid.UUID, db: AsyncSession,
) -> Agent:
    """Require authority to mutate one Agent's override, including project ACL."""
    agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="agent not found")
    if _is_platform_admin(current_user):
        return agent
    if agent.scope == "project" and agent.project_id is not None:
        from app.services.project_service import require_project

        await require_project(db, current_user, agent.project_id, edit=True)
        return agent
    agent, access = await check_agent_access(db, current_user, agent_id)
    if access != "manage":
        raise HTTPException(403, detail=render_message("mcpAccess.manageRequired"))
    return agent



async def _require_agent_server_access(
    current_user: User,
    agent_id: uuid.UUID,
    server: MCPServer,
    db: AsyncSession,
) -> Agent:
    """Authorize one Agent-scoped MCP view or override without global edit authority."""
    agent = await _require_agent_override_access(current_user, agent_id, db)
    if server.tenant_id is not None and server.tenant_id != agent.tenant_id:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if agent.scope != "project" and await shared_catalog(db, server):
        return agent

    assigned_tool_id = await db.scalar(
        select(AgentTool.id)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .where(
            AgentTool.agent_id == agent.id,
            Tool.type == "mcp",
            Tool.mcp_server_id == server.id,
        )
        .limit(1)
    )
    if assigned_tool_id is None:
        # Project configuration may only override MCP servers already copied
        # into this project's isolated Digital Employee snapshot.
        raise HTTPException(status_code=404, detail="MCP server not found")
    return agent
