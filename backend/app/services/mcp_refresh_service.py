"""Manual MCP tool discovery and idempotent persistence.

One implementation serves global admin refresh, Agent-scoped UI refresh, and
the Agent's own ``refresh_mcp_server`` tool. Refresh is intentionally additive:
it creates newly discovered tools and updates existing schemas, but does not
delete tools that are absent from a later ``tools/list`` response.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.project import ProjectCapabilityBinding
from app.models.tool import AgentTool, Tool
from app.services.mcp_client import MCPClient
from app.services.mcp_catalog_locks import lock_mcp_catalogs
from app.services.mcp_catalog_policy import shared_catalog
from app.services.llm.failure_outcome import render_message
from app.services.mcp_refresh_snapshot import MCPRefreshChanged, plan_refresh
from app.services.mcp_naming import tool_function_name
from app.services.mcp_connection_resolution import render_http_connection
from app.services.mcp_server_service import (
    agent_private_server_name,
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
    server_id: uuid.UUID | None = None

    def to_dict(self) -> dict[str, int]:
        return {"discovered": self.discovered, "created": self.created,
                "updated": self.updated, "assigned": self.assigned}


def _platform_context(server: MCPServer) -> PlaceholderContext:
    tenant = {"id": str(server.tenant_id)} if server.tenant_id else {}
    return PlaceholderContext(tenant=tenant)


def _tool_name(server: MCPServer, remote_name: str) -> str:
    """Build a stable Tool.name that fits the database's varchar(100)."""
    return tool_function_name(server, remote_name)


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
            invocation_id=uuid.uuid4().hex,
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

    url, headers, credential = render_http_connection(config, context)
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


async def _project_mcp_references(
    db,
    server: MCPServer,
    source_agent_id: uuid.UUID,
) -> dict[uuid.UUID, bool]:
    """Return project Agents that explicitly inherit this source Agent's MCP."""
    if server.tenant_id is None:
        return {}
    rows = (
        await db.execute(
            select(Agent.id, ProjectCapabilityBinding.is_enabled)
            .join(
                ProjectCapabilityBinding,
                ProjectCapabilityBinding.inherited_from_agent_id == Agent.id,
            )
            .where(
                Agent.scope == "project",
                Agent.source_agent_id == source_agent_id,
                Agent.project_id == ProjectCapabilityBinding.project_id,
                Agent.tenant_id == server.tenant_id,
                ProjectCapabilityBinding.tenant_id == server.tenant_id,
                ProjectCapabilityBinding.capability_type == "mcp",
                ProjectCapabilityBinding.capability_id == server.id,
                ProjectCapabilityBinding.source == "inherited",
            )
        )
    ).all()
    return {project_agent_id: is_enabled for project_agent_id, is_enabled in rows}


async def _assert_agent_refresh_is_isolated(
    db,
    server: MCPServer,
    agent_id: uuid.UUID,
    *,
    incoming_owner: bool = False,
) -> dict[uuid.UUID, bool]:
    """Allow one owner plus explicit project references to refresh a server."""
    if await shared_catalog(db, server) and not (
        incoming_owner and server.created_by_user_id is None
        and await db.scalar(select(Tool.id).where(Tool.mcp_server_id == server.id).limit(1)) is None
    ):
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
    project_references = await _project_mcp_references(db, server, agent_id)
    owns_server = incoming_owner or any(
        assignment.agent_id == agent_id
        and assignment.source == "user_installed"
        and assignment.installed_by_agent_id == agent_id
        for assignment in assignments
    )
    refresh_is_isolated = owns_server and all(
        (
            assignment.agent_id == agent_id
            and assignment.source == "user_installed"
            and assignment.installed_by_agent_id == agent_id
        )
        or assignment.agent_id in project_references
        for assignment in assignments
    )
    if not refresh_is_isolated:
        raise PermissionError(
            "Only an MCP server installed exclusively by the current Agent can "
            "be refreshed here; inherited or shared MCP servers must use the "
            "administrator global refresh"
        )
    return project_references


async def ensure_agent_mcp_server_isolated(
    db,
    server: MCPServer,
    agent_id: uuid.UUID,
    *,
    plan_only: bool = False,
) -> MCPServer:
    """Split a historical same-URL catalog shared by self-installing Agents.

    Older HTTP imports reused one MCPServer/Tool catalog for every Agent in a
    tenant. If all consumers are genuine self-installs, move only the current
    Agent's bindings to a private clone before refresh. Enterprise/inherited
    sharing is intentionally left untouched so the normal guard rejects it.
    """
    if await shared_catalog(db, server):
        return server

    pairs = (
        await db.execute(
            select(AgentTool, Tool)
            .join(Tool, Tool.id == AgentTool.tool_id)
            .where(Tool.mcp_server_id == server.id)
            .order_by(AgentTool.created_at)
        )
    ).all()
    current_pairs = [
        (assignment, tool)
        for assignment, tool in pairs
        if assignment.agent_id == agent_id
    ]
    if not current_pairs or not all(
        assignment.source == "user_installed"
        and assignment.installed_by_agent_id == agent_id
        for assignment, _tool in current_pairs
    ):
        return server

    other_pairs = [
        (assignment, tool)
        for assignment, tool in pairs
        if assignment.agent_id != agent_id
    ]
    if not other_pairs:
        return server
    project_references = await _project_mcp_references(db, server, agent_id)
    if any(
        assignment.agent_id in project_references
        for assignment, _tool in other_pairs
    ):
        # Never split a server away from its explicit project references. The
        # final guard will still reject any unrelated owner in this mixed case.
        return server
    if not all(
        assignment.source == "user_installed"
        and assignment.installed_by_agent_id == assignment.agent_id
        for assignment, _tool in other_pairs
    ):
        return server

    tenant_override, agent_override = await lookup_overrides(
        db,
        server.id,
        server.tenant_id,
        agent_id,
    )
    resolved = compose_runtime_config(server, tenant_override, agent_override)
    assignment_config = dict(current_pairs[0][0].config or {})
    private_url = str(
        assignment_config.get("mcp_url")
        or resolved.url_template
        or server.base_url_template
        or ""
    )
    private_name = agent_private_server_name(
        server.display_name,
        private_url,
        agent_id,
    )
    tenant_clause = (
        MCPServer.tenant_id == server.tenant_id
        if server.tenant_id is not None
        else MCPServer.tenant_id.is_(None)
    )
    private_server = (
        await db.execute(
            select(MCPServer).where(
                MCPServer.base_url_template == private_url,
                MCPServer.name.endswith(private_name[private_name.rfind("-a"):]),
                tenant_clause,
            )
        )
    ).scalar_one_or_none()
    if private_server is None:
        private_server = MCPServer(
            tenant_id=server.tenant_id,
            name=private_name,
            display_name=server.display_name,
            base_url_template=private_url,
            headers_template=deepcopy(
                assignment_config.get("headers")
                if isinstance(assignment_config.get("headers"), dict)
                else resolved.headers_template or {}
            ),
            credential_template=(
                assignment_config.get("api_key")
                or resolved.credential_template
            ),
            system_prompt_block="\n\n".join(resolved.prompt_blocks) or None,
            instructions=server.instructions,
            instructions_captured_at=server.instructions_captured_at,
            placeholder_allowlist=deepcopy(server.placeholder_allowlist),
            transport=resolved.transport,
            command_template=(
                assignment_config.get("command")
                or resolved.command_template
            ),
            args_template=deepcopy(
                assignment_config.get("args")
                or resolved.args_template
            ),
            env_template=deepcopy(
                assignment_config.get("env")
                or resolved.env_template
            ),
        )
        if not plan_only:
            db.add(private_server)
            await db.flush()

    if plan_only:
        return private_server

    for assignment, old_tool in current_pairs:
        remote_name = old_tool.mcp_tool_name
        private_tool = (
            await db.execute(
                select(Tool).where(
                    Tool.mcp_server_id == private_server.id,
                    Tool.mcp_tool_name == remote_name,
                )
            )
        ).scalar_one_or_none()
        if private_tool is None:
            name_seed = remote_name or old_tool.name
            private_tool = Tool(
                name=_tool_name(private_server, name_seed),
                display_name=old_tool.display_name,
                description=old_tool.description,
                type="mcp",
                category=old_tool.category,
                icon=old_tool.icon,
                parameters_schema=deepcopy(old_tool.parameters_schema or {}),
                config=deepcopy(old_tool.config or {}),
                config_schema=deepcopy(old_tool.config_schema or {}),
                mcp_server_url=private_url or None,
                mcp_server_name=private_server.name,
                mcp_tool_name=remote_name,
                system_prompt_block=old_tool.system_prompt_block,
                mcp_server_instructions=old_tool.mcp_server_instructions,
                enabled=old_tool.enabled,
                is_default=False,
                source="agent",
                mcp_server_id=private_server.id,
                tenant_id=server.tenant_id,
            )
            db.add(private_tool)
            await db.flush()

        existing_assignment = (
            await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id == agent_id,
                    AgentTool.tool_id == private_tool.id,
                )
            )
        ).scalar_one_or_none()
        if existing_assignment is None:
            assignment.tool_id = private_tool.id
        else:
            existing_assignment.enabled = assignment.enabled
            existing_assignment.config = deepcopy(assignment.config or {})
            existing_assignment.source = assignment.source
            existing_assignment.installed_by_agent_id = (
                assignment.installed_by_agent_id
            )
            await db.delete(assignment)

    await db.flush()
    return private_server


async def refresh_mcp_server_tools(
    db,
    server_id: uuid.UUID,
    *,
    agent_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    session_id: str = "",
    assign_to_agent: bool = False,
) -> MCPToolRefreshResult:
    """Discover and persist one MCP server's current tool catalog.

    Existing Tool and AgentTool state is preserved. When ``assign_to_agent`` is
    true, only newly created/missing assignments are enabled for that Agent.
    """
    # Callers supply a clean session. Never commit caller-owned pending work
    # merely to release the read transaction before provider discovery.
    if db.new or db.dirty or db.deleted:
        raise ValueError(render_message("mcpAccess.pendingWrites"))
    plan = await plan_refresh(db, server_id, agent_id, user_id, session_id)
    await db.rollback()
    discovered_tools, instructions = await _discover_tools(
        plan.server, plan.config, plan.context, agent_id,
    )

    # Rollback expired the identity map; explicit expiration also protects
    # callers using a session configured with expire_on_commit=False.
    db.expire_all()
    await lock_mcp_catalogs(db, plan.server_ids, agent_id=agent_id)
    current = await plan_refresh(db, server_id, agent_id, user_id, session_id)
    if current.fingerprint != plan.fingerprint:
        raise MCPRefreshChanged()
    server = await db.get(MCPServer, server_id)
    project_references = {}
    if agent_id is not None:
        server = await ensure_agent_mcp_server_isolated(db, server, agent_id)
        project_references = await _assert_agent_refresh_is_isolated(db, server, agent_id)

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
    catalog_source = "admin" if await shared_catalog(db, server) else "agent"
    refreshed_tool_ids: set[uuid.UUID] = set()
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
                source=catalog_source,
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
        refreshed_tool_ids.add(tool.id)

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

    if assign_to_agent and project_references and refreshed_tool_ids:
        existing_project_assignments = set(
            (
                await db.execute(
                    select(AgentTool.agent_id, AgentTool.tool_id).where(
                        AgentTool.agent_id.in_(project_references),
                        AgentTool.tool_id.in_(refreshed_tool_ids),
                    )
                )
            ).all()
        )
        for project_agent_id, is_enabled in project_references.items():
            for tool_id in refreshed_tool_ids:
                if (project_agent_id, tool_id) in existing_project_assignments:
                    continue
                db.add(
                    AgentTool(
                        agent_id=project_agent_id,
                        tool_id=tool_id,
                        enabled=is_enabled,
                        config={},
                        source="user_installed",
                    )
                )

    server.instructions = instructions
    server.instructions_captured_at = datetime.now(timezone.utc)
    await db.flush()
    return MCPToolRefreshResult(
        discovered=created + updated,
        created=created,
        updated=updated,
        assigned=assigned,
        server_id=server.id,
    )
