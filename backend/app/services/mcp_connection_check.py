"""Read-only checks using the execution configuration contract."""

from dataclasses import replace
from types import SimpleNamespace
from sqlalchemy import select

from app.models.agent import Agent
from app.models.tool import AgentTool, Tool
from app.schemas.mcp_server import TestConnectionResult
from app.services.agent_runtime_workspace import bind_agent_runtime_workspace, resolve_agent_runtime_workspace
from app.services.llm.failure_outcome import render_message
from app.services.mcp_catalog_policy import shared_catalog
from app.services.mcp_connection_resolution import (
    render_http_connection, resolve_agent_connection, resolve_smithery_connection,
)
from app.services.mcp_refresh_service import _discover_tools


async def check_agent_connection(db, server, agent_id, user_id, tenant_id):
    agent = await db.get(Agent, agent_id)
    workspace = resolve_agent_runtime_workspace(
        agent_id=agent_id, agent_scope=agent.scope, agent_project_id=agent.project_id,
        tenant_id=tenant_id, session_project_id=agent.project_id,
    )
    unbound_shared = agent.scope != "project" and await shared_catalog(db, server)
    pairs = (await db.execute(select(Tool, AgentTool).join(
        AgentTool, (AgentTool.tool_id == Tool.id) & (AgentTool.agent_id == agent_id),
    ).where(Tool.mcp_server_id == server.id,
            Tool.enabled.is_(True), (Tool.tenant_id == tenant_id) | Tool.tenant_id.is_(None)))).all()
    if not pairs and unbound_shared:
        catalog = (await db.scalars(select(Tool).where(Tool.mcp_server_id == server.id))).all()
        pairs = [(tool, None) for tool in catalog if tool.enabled and tool.tenant_id in (None, tenant_id)]
        if not catalog:
            pairs = [(None, None)]
    configs = []
    for tool, assignment in pairs:
        candidate = await resolve_agent_connection(db, server, agent, tool, assignment, user_id)
        if candidate not in configs:
            configs.append(candidate)
    snapshot = SimpleNamespace(name=server.name)
    await db.rollback()
    try:
        count = 0
        instructions = None
        if not configs:
            return TestConnectionResult(success=False, error=render_message("mcpAccess.unavailable"))
        with bind_agent_runtime_workspace(workspace):
            for config, values, context in configs:
                if config.transport != "stdio":
                    url, _, credential = render_http_connection(config, context)
                    if ".run.tools" in url:
                        endpoint, key, _, _ = await resolve_smithery_connection(
                            {**values, "smithery_api_key": credential or values.get("smithery_api_key")}, agent_id,
                        )
                        config = replace(config, url_template=endpoint, credential_template=key, headers_template={})
                tools, instructions = await _discover_tools(snapshot, config, context, agent_id)
                count = max(count, len(tools))
        return TestConnectionResult(success=True, instructions=instructions,
                                    server_info={"tool_count": count, "configurations_checked": len(configs)})
    except Exception as exc:
        return TestConnectionResult(success=False, error=str(exc)[:500])
