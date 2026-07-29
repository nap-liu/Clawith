"""Manual MCP tool discovery and idempotent persistence.

One implementation serves global admin refresh, Agent-scoped UI refresh, and
the Agent's own ``refresh_mcp_server`` tool. Refresh is intentionally additive:
it creates newly discovered tools and updates existing schemas, but does not
delete tools that are absent from a later ``tools/list`` response.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tool import AgentTool, Tool
from app.services.mcp_client import MCPClient
from app.services.mcp_server_service import (
    build_placeholder_context_for_call,
    compose_runtime_config,
    lookup_overrides,
)
from app.services.placeholder_engine import ALL_ROOTS, PlaceholderContext, render, render_dict
from app.services.sandbox_mcp_host import SandboxMcpHost
from app.services.sandbox_mcp_hub_client import SandboxMcpHubClient


@dataclass(frozen=True)
class MCPToolRefreshResult:
    discovered: int
    created: int
    updated: int
    assigned: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _platform_context(server: MCPServer) -> PlaceholderContext:
    tenant = {"id": str(server.tenant_id)} if server.tenant_id else {}
    return PlaceholderContext(tenant=tenant)


def _tool_name(server: MCPServer, remote_name: str) -> str:
    """Build a stable Tool.name that fits the database's varchar(100)."""
    value = f"mcp_{server.name}_{remote_name}"
    if len(value) <= 100:
        return value
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"{value[:89]}_{digest}"


def _agent_workspace_root(agent_id: uuid.UUID):
    """Import lazily to keep MCP admin API startup independent of agent runtime."""
    from app.services.agent_tools import _agent_workspace_root as resolve_workspace

    return resolve_workspace(agent_id)


async def _discover_tools(
    server: MCPServer,
    config,
    context: PlaceholderContext,
    agent_id: uuid.UUID | None,
) -> tuple[list[dict], str | None]:
    if config.transport == "stdio":
        settings = get_settings()
        if not settings.SANDBOX_API_URL:
            raise RuntimeError("stdio MCP unavailable — SANDBOX_API_URL not configured")

        command = render(config.command_template or "", context, ALL_ROOTS, on_unknown="raise")
        args = [
            render(arg, context, ALL_ROOTS, on_unknown="raise")
            for arg in (config.args_template or [])
        ]
        env = render_dict(config.env_template or {}, context, ALL_ROOTS, on_unknown="raise")
        work_dir = None
        if agent_id is not None:
            workspace = _agent_workspace_root(agent_id)
            workspace.mkdir(parents=True, exist_ok=True)
            work_dir = str(workspace.resolve())

        host = SandboxMcpHost(settings.SANDBOX_API_URL, settings.SANDBOX_API_KEY)
        entry = await host.ensure_registered(
            server.name,
            str(agent_id) if agent_id else "__refresh__",
            {"command": command, "args": args, "env": env},
            cwd=work_dir,
        )
        hub = SandboxMcpHubClient(settings.SANDBOX_API_URL, settings.SANDBOX_API_KEY)
        try:
            tools = await hub.list_tools(entry)
        finally:
            try:
                await host.deregister(entry)
            except Exception:
                pass
        return tools, f"stdio MCP server; {len(tools)} tools discovered"

    url = render(config.url_template or "", context, ALL_ROOTS, on_unknown="raise")
    headers = render_dict(config.headers_template or {}, context, ALL_ROOTS, on_unknown="raise")
    credential = (
        render(config.credential_template, context, ALL_ROOTS, on_unknown="raise")
        if config.credential_template
        else None
    )
    client = MCPClient(url, api_key=credential, headers=headers or None)
    tools = await client.list_tools()
    return tools, client.server_instructions


async def _assignment_template(
    db,
    server_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> AgentTool | None:
    rows = (
        await db.execute(
            select(AgentTool)
            .join(Tool, Tool.id == AgentTool.tool_id)
            .where(
                AgentTool.agent_id == agent_id,
                Tool.mcp_server_id == server_id,
            )
            .order_by(AgentTool.created_at)
        )
    ).scalars().all()
    if not rows:
        return None
    return next(
        (
            row
            for row in rows
            if row.source == "user_installed"
            and row.installed_by_agent_id == agent_id
        ),
        rows[0],
    )


async def _assert_agent_refresh_is_isolated(
    db,
    server: MCPServer,
    agent_id: uuid.UUID,
) -> None:
    """Allow Agent-scoped refresh only for an exclusively self-installed server."""
    if server.created_by_user_id is not None:
        raise PermissionError(
            "Inherited enterprise MCP servers cannot be refreshed by an Agent; "
            "use the administrator global refresh instead"
        )

    assignments = (
        await db.execute(
            select(AgentTool)
            .join(Tool, Tool.id == AgentTool.tool_id)
            .where(Tool.mcp_server_id == server.id)
        )
    ).scalars().all()
    exclusively_self_installed = bool(assignments) and all(
        assignment.agent_id == agent_id
        and assignment.source == "user_installed"
        and assignment.installed_by_agent_id == agent_id
        for assignment in assignments
    )
    if not exclusively_self_installed:
        raise PermissionError(
            "Only an MCP server installed exclusively by the current Agent can "
            "be refreshed here; inherited or shared MCP servers must use the "
            "administrator global refresh"
        )


async def refresh_mcp_server_tools(
    db,
    server_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    assign_to_agent: bool = False,
) -> MCPToolRefreshResult:
    """Discover and persist one MCP server's current tool catalog.

    Existing Tool and AgentTool state is preserved. When ``assign_to_agent`` is
    true, only newly created/missing assignments are enabled for that Agent.
    """
    server = (
        await db.execute(select(MCPServer).where(MCPServer.id == server_id))
    ).scalar_one_or_none()
    if server is None:
        raise LookupError("MCP server not found")

    agent = None
    tenant_id = server.tenant_id
    if agent_id is not None:
        agent = (
            await db.execute(select(Agent).where(Agent.id == agent_id))
        ).scalar_one_or_none()
        if agent is None:
            raise LookupError("Agent not found")
        if (
            server.tenant_id is not None
            and agent.tenant_id != server.tenant_id
        ):
            raise PermissionError("Agent and MCP server belong to different tenants")
        tenant_id = agent.tenant_id
        await _assert_agent_refresh_is_isolated(db, server, agent_id)

    tenant_override, agent_override = await lookup_overrides(
        db,
        server.id,
        tenant_id,
        agent_id,
    )
    config = compose_runtime_config(server, tenant_override, agent_override)
    context = (
        await build_placeholder_context_for_call(db, agent_id, user_id)
        if agent_id is not None
        else _platform_context(server)
    )

    discovered_tools, instructions = await _discover_tools(
        server,
        config,
        context,
        agent_id,
    )

    existing_rows = (
        await db.execute(select(Tool).where(Tool.mcp_server_id == server.id))
    ).scalars().all()
    existing_by_remote = {
        row.mcp_tool_name: row
        for row in existing_rows
        if row.mcp_tool_name
    }
    assignment_seed = (
        await _assignment_template(db, server.id, agent_id)
        if assign_to_agent and agent_id is not None
        else None
    )

    created = 0
    updated = 0
    assigned = 0
    for item in discovered_tools:
        remote_name = str(item.get("name") or "").strip()
        if not remote_name:
            continue
        description = str(item.get("description") or "")
        parameters = item.get("inputSchema") or {
            "type": "object",
            "properties": {},
        }
        tool = existing_by_remote.get(remote_name)
        if tool is None:
            tool = Tool(
                name=_tool_name(server, remote_name),
                display_name=remote_name.replace("_", " ").title(),
                description=description,
                type="mcp",
                category="mcp",
                icon="🔌",
                parameters_schema=parameters,
                mcp_server_url=server.base_url_template or None,
                mcp_server_name=server.name,
                mcp_tool_name=remote_name,
                mcp_server_instructions=instructions,
                mcp_server_id=server.id,
                enabled=True,
                is_default=False,
                source=(
                    "agent"
                    if assignment_seed is not None
                    and assignment_seed.source == "user_installed"
                    else "admin"
                ),
                tenant_id=server.tenant_id,
            )
            db.add(tool)
            await db.flush()
            existing_by_remote[remote_name] = tool
            created += 1
        else:
            tool.description = description
            tool.parameters_schema = parameters
            tool.mcp_server_url = server.base_url_template or None
            tool.mcp_server_name = server.name
            tool.mcp_server_instructions = instructions
            updated += 1

        if assign_to_agent and agent_id is not None:
            assignment = (
                await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == agent_id,
                        AgentTool.tool_id == tool.id,
                    )
                )
            ).scalar_one_or_none()
            if assignment is None:
                assignment = AgentTool(
                    agent_id=agent_id,
                    tool_id=tool.id,
                    enabled=True,
                    config=dict(assignment_seed.config or {}) if assignment_seed else {},
                    source=assignment_seed.source if assignment_seed else "system",
                    installed_by_agent_id=(
                        assignment_seed.installed_by_agent_id
                        if assignment_seed
                        else None
                    ),
                )
                db.add(assignment)
                assigned += 1

    server.instructions = instructions
    server.instructions_captured_at = datetime.now(timezone.utc)
    await db.flush()
    return MCPToolRefreshResult(
        discovered=created + updated,
        created=created,
        updated=updated,
        assigned=assigned,
    )
