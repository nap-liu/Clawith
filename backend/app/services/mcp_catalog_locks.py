"""Common row-lock order for MCP catalog refresh and deletion."""

from sqlalchemy import or_, select, text

from app.models.agent import Agent
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.models.project import ProjectCapabilityBinding
from app.models.tool import AgentTool, Tool


async def lock_mcp_catalogs(db, server_ids=(), *, agent_id=None, tool_ids=()):
    """Lock Agent -> servers -> project references -> tools -> assignments.

    Call before any catalog deletion or assignment mutation. Tool-only callers
    also cover legacy MCP rows without a server. No network work belongs here.
    """
    await db.execute(text("SET LOCAL lock_timeout = '3s'"))
    if agent_id is not None:
        await db.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update())
    await db.execute(select(MCPServer.id).where(
        MCPServer.id.in_(server_ids),
    ).order_by(MCPServer.id).with_for_update())
    await db.execute(select(ProjectCapabilityBinding.id).where(
        ProjectCapabilityBinding.capability_type == "mcp",
        ProjectCapabilityBinding.capability_id.in_(server_ids),
    ).order_by(ProjectCapabilityBinding.id).with_for_update())
    ids = (await db.scalars(select(Tool.id).where(or_(
        Tool.mcp_server_id.in_(server_ids), Tool.id.in_(tool_ids),
    )).order_by(Tool.id).with_for_update())).all()
    await db.execute(select(AgentTool.id).where(
        AgentTool.tool_id.in_(ids),
    ).order_by(AgentTool.id).with_for_update())
    await db.execute(select(MCPServerOverride.id).where(
        MCPServerOverride.mcp_server_id.in_(server_ids),
    ).order_by(MCPServerOverride.id).with_for_update())
    return ids


async def lock_tool_catalog(db, tool_id, *, agent_id=None):
    server_id = await db.scalar(select(Tool.mcp_server_id).where(Tool.id == tool_id))
    return await lock_mcp_catalogs(
        db, [server_id] if server_id else [], agent_id=agent_id, tool_ids=[tool_id],
    )
