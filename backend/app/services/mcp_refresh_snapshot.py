"""Read-only refresh planning and short-transaction revocation checks."""

from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import inspect, select

from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.project import ProjectCapabilityBinding
from app.models.tool import AgentTool, Tool
from app.services.llm.failure_outcome import render_message
from app.services.mcp_access import visible_mcp_installations
from app.services.mcp_catalog_policy import shared_catalog
from app.services.mcp_server_service import (
    build_placeholder_context_for_call, compose_runtime_config, lookup_overrides,
)
from app.services.placeholder_engine import PlaceholderContext


class MCPRefreshUnavailable(PermissionError):
    def __init__(self):
        super().__init__(render_message("mcpAccess.refreshUnavailable"))


class MCPRefreshChanged(PermissionError):
    def __init__(self):
        super().__init__(render_message("mcpAccess.refreshChanged"))


def _record(value):
    return {column.key: deepcopy(getattr(value, column.key))
            for column in inspect(type(value)).column_attrs}


@dataclass(frozen=True)
class RefreshPlan:
    server: SimpleNamespace
    config: object
    context: PlaceholderContext
    fingerprint: dict
    server_ids: tuple


async def plan_refresh(db, server_id, agent_id, user_id, session_id):
    # The public service owns transport and compatibility exports. This import
    # avoids a module cycle while sharing its exact legacy isolation planner.
    from app.services.mcp_refresh_service import (
        _assert_agent_refresh_is_isolated,
        ensure_agent_mcp_server_isolated,
    )

    server = await db.get(MCPServer, server_id)
    if server is None:
        raise LookupError(render_message("mcpAccess.refreshUnavailable"))
    if agent_id is None and not await shared_catalog(db, server):
        raise PermissionError(render_message("mcpAccess.agentScopeRequired"))
    prospective = server
    tenant_id = server.tenant_id
    references = {}
    if agent_id is not None:
        agent = await db.get(Agent, agent_id)
        if agent is None or server.tenant_id not in (None, agent.tenant_id):
            raise MCPRefreshUnavailable()
        tenant_id = agent.tenant_id
        permitted = await visible_mcp_installations(db, agent_id)
        if not any(tool.mcp_server_id == server_id and assignment is not None
                   for tool, assignment in permitted):
            raise MCPRefreshUnavailable()
        prospective = await ensure_agent_mcp_server_isolated(db, server, agent_id, plan_only=True)
        if prospective is server:
            references = await _assert_agent_refresh_is_isolated(db, server, agent_id)
        elif prospective.id is not None:
            references = await _assert_agent_refresh_is_isolated(
                db, prospective, agent_id, incoming_owner=True,
            )

    overrides = await lookup_overrides(db, prospective.id, tenant_id, agent_id) if prospective.id else (None, None)
    config = deepcopy(compose_runtime_config(prospective, *overrides))
    context = (
        await build_placeholder_context_for_call(db, agent_id, user_id, session_id=session_id)
        if agent_id else PlaceholderContext(tenant={"id": str(tenant_id)} if tenant_id else {})
    )
    server_ids = tuple(sorted({server_id, prospective.id} - {None}))
    tools = (await db.scalars(select(Tool).where(Tool.mcp_server_id.in_(server_ids)).order_by(Tool.id))).all()
    if agent_id is not None and prospective is not server and prospective.id is not None:
        target_tools = [tool for tool in tools if tool.mcp_server_id == prospective.id]
        if target_tools and not any(
            tool.enabled and tool.tenant_id in (None, tenant_id) for tool in target_tools
        ):
            raise MCPRefreshUnavailable()
    assignments = (await db.scalars(select(AgentTool).where(
        AgentTool.tool_id.in_([tool.id for tool in tools]),
    ).order_by(AgentTool.id))).all()
    project_bindings = (await db.scalars(select(ProjectCapabilityBinding).where(
        ProjectCapabilityBinding.capability_type == "mcp",
        ProjectCapabilityBinding.capability_id.in_(server_ids),
    ).order_by(ProjectCapabilityBinding.id))).all()
    fingerprint = {
        "server": _record(server), "config": config, "context": context,
        "tools": [_record(tool) for tool in tools],
        "assignments": [_record(assignment) for assignment in assignments],
        "references": references,
        "prospective": _record(prospective),
        "project_bindings": [_record(binding) for binding in project_bindings],
    }
    return RefreshPlan(SimpleNamespace(**_record(prospective)), config, deepcopy(context), fingerprint, server_ids)
