"""Platform visibility and effective MCP assignments for the current Agent."""

from sqlalchemy import select

from app.core.okr_feature import OKR_TOOL_NAMES, okr_feature_enabled
from app.core.plaza_feature import PLAZA_TOOL_NAMES
from app.models.agent import Agent
from app.models.tool import AgentTool, Tool
from app.services.tool_enablement import tool_visibility_clause
from app.services.turn_tool_settings import effective_assignment


async def visible_mcp_installations(db, agent_id, *, name=None):
    """Read platform-visible tools plus optional durable installations.

    Unassigned admin tools are included for scene-only execution; inventory
    callers must require a real assignment. No installation is created here.
    """
    tenant = await db.execute(select(Agent.tenant_id).where(Agent.id == agent_id))
    row = tenant.first()
    if row is None:
        return []
    assigned_ids = select(AgentTool.tool_id).where(AgentTool.agent_id == agent_id)
    query = select(Tool, AgentTool).outerjoin(
        AgentTool, (AgentTool.tool_id == Tool.id) & (AgentTool.agent_id == agent_id),
    ).where(
        Tool.type == "mcp", Tool.enabled.is_(True),
        tool_visibility_clause(row[0], assigned_ids),
        Tool.name.not_in(PLAZA_TOOL_NAMES),
    )
    if not okr_feature_enabled():
        query = query.where(Tool.name.not_in(OKR_TOOL_NAMES))
    if name is not None:
        # A canonical name cannot fall through to another tool's alias when
        # its assignment or platform permission has been revoked.
        exact = await db.scalar(select(Tool.id).where(Tool.name == name))
        query = query.where(Tool.name == name if exact else Tool.mcp_tool_name == name)
    return (await db.execute(query)).all()


async def resolve_mcp_execution(db, agent_id, name):
    candidates = []
    for tool, assignment in await visible_mcp_installations(db, agent_id, name=name):
        effective = effective_assignment(agent_id, tool, assignment)
        if effective is not None:
            candidates.append((tool, effective))
    return candidates


async def removable_mcp_server_ids(db, agent_id, visible_server_ids):
    """Installation ownership is independent of platform execution permission."""
    tenant = select(Agent.tenant_id).where(Agent.id == agent_id).scalar_subquery()
    installed = select(AgentTool.tool_id).where(AgentTool.agent_id == agent_id).correlate(None)
    return set(await db.scalars(select(Tool.mcp_server_id).join(
        AgentTool, AgentTool.tool_id == Tool.id,
    ).where(
        Tool.type == "mcp", Tool.mcp_server_id.in_(visible_server_ids),
        tool_visibility_clause(tenant, installed),
        AgentTool.agent_id == agent_id, AgentTool.source == "user_installed",
        AgentTool.installed_by_agent_id == agent_id,
    )))
