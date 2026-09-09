"""Agent connection checks resolve overlays without changing shared catalogs."""

from types import SimpleNamespace
from sqlalchemy import select

from app.models.tool import AgentTool, Tool
from app.schemas.mcp_server import TestConnectionResult
from app.services.mcp_catalog_policy import shared_catalog, apply_shared_tool_config
from app.services.tool_config import decrypt_sensitive_fields
from app.services.mcp_refresh_service import _discover_tools
from app.services.mcp_server_service import build_placeholder_context_for_call, compose_runtime_config, lookup_overrides


async def check_agent_connection(db, server, agent_id, user_id, tenant_id):
    overrides = await lookup_overrides(db, server.id, tenant_id, agent_id)
    config = compose_runtime_config(server, *overrides)
    configs = [config]
    if await shared_catalog(db, server):
        pairs = (await db.execute(select(Tool, AgentTool).join(
            AgentTool, AgentTool.tool_id == Tool.id,
        ).where(Tool.mcp_server_id == server.id, AgentTool.agent_id == agent_id))).all()
        configs = []
        for tool, assignment in pairs:
            candidate = apply_shared_tool_config(config, decrypt_sensitive_fields(
                assignment.config or {}, tool.config_schema,
            ))
            if candidate not in configs:
                configs.append(candidate)
        configs = configs or [config]
    context = await build_placeholder_context_for_call(db, agent_id, user_id)
    snapshot = SimpleNamespace(name=server.name)
    await db.rollback()
    try:
        count = 0
        instructions = None
        for candidate in configs:
            tools, instructions = await _discover_tools(snapshot, candidate, context, agent_id)
            count = max(count, len(tools))
        return TestConnectionResult(success=True, instructions=instructions,
                                    server_info={"tool_count": count, "configurations_checked": len(configs)})
    except Exception as exc:
        return TestConnectionResult(success=False, error=str(exc)[:500])
