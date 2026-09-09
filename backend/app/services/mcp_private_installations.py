"""Private MCP installation identity, independent of display names and secrets."""

import uuid
from copy import deepcopy

from sqlalchemy import or_, select

from app.models.agent import Agent
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.models.tool import AgentTool, Tool
from app.services.llm.failure_outcome import render_message
from app.services.mcp_naming import server_internal_name, tool_function_name


async def private_server(db, agent_id, tenant_id, display_name, config, *, installation_config=None, create=True):
    """Reuse identical owner/configuration; allocate a random suffix otherwise.

    Credentials are compared only inside the transaction and never encoded in
    names. Serializing one Agent makes concurrent identical imports idempotent.
    Real AgentTool provenance remains the authority for persisted installations.
    """
    agent_id = uuid.UUID(str(agent_id))
    agent = await db.scalar(select(Agent).where(Agent.id == agent_id).with_for_update())
    if agent is None or agent.tenant_id != tenant_id:
        raise PermissionError(render_message("mcpAccess.unavailable"))
    owned_ids = select(Tool.mcp_server_id).join(AgentTool, AgentTool.tool_id == Tool.id).where(
        AgentTool.agent_id == agent_id, AgentTool.source == "user_installed",
        AgentTool.installed_by_agent_id == agent_id,
    )
    # Empty rows can occur while the current transaction is building a catalog.
    # The full owner UUID marker applies only to these unbound private rows.
    marker = f"-a{agent_id.hex}-"
    candidates = (await db.scalars(select(MCPServer).where(
        MCPServer.tenant_id == tenant_id, MCPServer.created_by_user_id.is_(None),
        or_(MCPServer.id.in_(owned_ids), MCPServer.name.contains(marker)),
    ).order_by(MCPServer.id))).all()
    for server in candidates:
        tools = (await db.scalars(select(Tool).where(Tool.mcp_server_id == server.id))).all()
        if any(tool.source != "agent" for tool in tools):
            continue
        bindings = (await db.scalars(select(AgentTool).where(
            AgentTool.tool_id.in_([tool.id for tool in tools]),
        ))).all()
        if bindings and any(binding.agent_id != agent_id or binding.source != "user_installed"
                            or binding.installed_by_agent_id != agent_id for binding in bindings):
            continue
        if (tools or installation_config is not None) and not bindings:
            continue
        if not all(getattr(server, key) == value for key, value in config.items()):
            continue
        if installation_config is not None and bindings and any(
            {k: v for k, v in (binding.config or {}).items() if k not in ("smithery_namespace", "smithery_connection_id")}
            != {k: v for k, v in installation_config.items() if k not in ("smithery_namespace", "smithery_connection_id")} for binding in bindings
        ):
            continue
        overrides = (await db.scalars(select(MCPServerOverride).where(
            MCPServerOverride.mcp_server_id == server.id,
            MCPServerOverride.scope_id.in_([agent_id, tenant_id]),
        ))).all()
        # An installation edited after import is a distinct connection identity.
        # Never reuse it and silently execute with a different credential.
        if any(any(getattr(override, field) is not None for field in (
            "url_template", "credential_template", "headers_template", "env_template",
            "command_template", "args_template",
        )) for override in overrides):
            continue
        return server
    if not create:
        return None
    server_id = uuid.uuid4()
    labels = {candidate.display_name for candidate in candidates}
    label = display_name[:190]
    number = 2
    while label in labels:
        label = f"{display_name[:190]}-{number}"
        number += 1
    base = server_internal_name(display_name, server_id).split("-")[0][:24]
    server = MCPServer(id=server_id, tenant_id=tenant_id, display_name=label,
                       name=f"{base}{marker}{server_id.hex}", **deepcopy(config))
    db.add(server)
    await db.flush()
    return server


async def persist_private_catalog(db, server, agent_id, tools, config):
    """Persist only one exact private catalog; never deduplicate globally by name."""
    names = []
    for item in tools:
        remote = item.get("name")
        tool = await db.scalar(select(Tool).where(
            Tool.mcp_server_id == server.id, Tool.mcp_tool_name == remote,
        ))
        if tool is None:
            tool = Tool(name=tool_function_name(server, remote or "server"), type="mcp", category="mcp",
                        source="agent", tenant_id=server.tenant_id, mcp_server_id=server.id,
                        mcp_server_name=server.name, mcp_server_url=server.base_url_template,
                        mcp_tool_name=remote, enabled=True, is_default=False)
            db.add(tool)
        tool.display_name = str(remote or server.display_name)[:100]
        tool.description = str(item.get("description") or "")[:500]
        tool.parameters_schema = item.get("inputSchema") or {"type": "object", "properties": {}}
        await db.flush()
        assignment = await db.scalar(select(AgentTool).where(
            AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id,
        ))
        if assignment is None:
            db.add(AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=True,
                             source="user_installed", installed_by_agent_id=agent_id,
                             config=deepcopy(config)))
        names.append(tool.display_name)
    await db.flush()
    return names
