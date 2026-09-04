"""Two-phase company MCP discovery and atomic catalog persistence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from loguru import logger

from app.config import get_settings
from app.models.mcp_server import MCPServer
from app.models.tool import Tool
from app.services.mcp_client import MCPClient
from app.services.mcp_naming import server_internal_name, tool_function_name
from app.services.placeholder_engine import ALL_ROOTS, PlaceholderContext, render, render_dict
from app.services.sandbox_mcp_host import SandboxMcpHost
from app.services.sandbox_mcp_hub_client import SandboxMcpHubClient


@dataclass(frozen=True)
class MCPImportDraft:
    display_name: str
    tenant_id: uuid.UUID | None
    transport: str
    base_url_template: str
    headers_template: dict
    credential_template: str | None
    command_template: str | None
    args_template: list[str]
    env_template: dict
    system_prompt_block: str | None
    placeholder_allowlist: list[str] | None


@dataclass(frozen=True)
class MCPDiscovery:
    tools: list[dict]
    instructions: str | None


async def discover_mcp_import(draft: MCPImportDraft) -> MCPDiscovery:
    """Perform provider I/O without an open application database transaction."""
    if draft.transport == "stdio":
        settings = get_settings()
        if not settings.SANDBOX_API_URL:
            raise RuntimeError("stdio MCP unavailable — SANDBOX_API_URL not configured")
        context = PlaceholderContext(
            tenant={"id": str(draft.tenant_id)} if draft.tenant_id else {}
        )
        command = render(
            draft.command_template or "", context, ALL_ROOTS, on_unknown="keep_literal"
        )
        args = [
            render(value, context, ALL_ROOTS, on_unknown="keep_literal")
            for value in draft.args_template
        ]
        env = render_dict(
            draft.env_template, context, ALL_ROOTS, on_unknown="keep_literal"
        )
        host = SandboxMcpHost(settings.SANDBOX_API_URL, settings.SANDBOX_API_KEY)
        import_key = f"company-import-{uuid.uuid4().hex}"
        entry = await host.ensure_registered(
            import_key,
            "__import__",
            {"command": command, "args": args, "env": env},
        )
        hub = SandboxMcpHubClient(settings.SANDBOX_API_URL, settings.SANDBOX_API_KEY)
        try:
            tools = await hub.list_tools(entry)
        finally:
            try:
                await host.deregister(entry)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask discovery
                logger.warning("Could not clean up temporary MCP import entry {}: {}", entry, exc)
        return MCPDiscovery(
            tools=tools,
            instructions=f"stdio MCP server; {len(tools)} tools discovered",
        )

    client = MCPClient(
        draft.base_url_template,
        api_key=draft.credential_template,
        headers=draft.headers_template or None,
    )
    tools = await client.list_tools()
    return MCPDiscovery(tools=tools, instructions=client.server_instructions)


async def persist_mcp_import(
    db,
    draft: MCPImportDraft,
    discovery: MCPDiscovery,
    *,
    actor_id: uuid.UUID,
) -> tuple[MCPServer, int]:
    """Create one server and all discovered tools in the caller's short transaction."""
    server_id = uuid.uuid4()
    server = MCPServer(
        id=server_id,
        tenant_id=draft.tenant_id,
        name=server_internal_name(draft.display_name, server_id),
        display_name=draft.display_name,
        base_url_template=draft.base_url_template,
        headers_template=draft.headers_template,
        credential_template=draft.credential_template,
        system_prompt_block=draft.system_prompt_block,
        placeholder_allowlist=draft.placeholder_allowlist,
        instructions=discovery.instructions,
        instructions_captured_at=datetime.now(UTC),
        created_by_user_id=actor_id,
        transport=draft.transport,
        command_template=draft.command_template,
        args_template=draft.args_template,
        env_template=draft.env_template,
    )
    db.add(server)

    seen: set[str] = set()
    tools: list[Tool] = []
    for item in discovery.tools:
        remote_name = str(item.get("name") or "").strip()
        if not remote_name or remote_name in seen:
            continue
        seen.add(remote_name)
        tools.append(
            Tool(
                name=tool_function_name(server, remote_name),
                display_name=remote_name.replace("_", " ").title(),
                description=str(item.get("description") or ""),
                type="mcp",
                category="mcp",
                icon="🔌",
                parameters_schema=item.get("inputSchema")
                or {"type": "object", "properties": {}},
                mcp_server_url=draft.base_url_template or None,
                mcp_server_name=server.name,
                mcp_tool_name=remote_name,
                mcp_server_instructions=discovery.instructions,
                mcp_server_id=server.id,
                enabled=True,
                is_default=False,
                source="admin",
                tenant_id=draft.tenant_id,
            )
        )
    if tools:
        db.add_all(tools)
    await db.flush()
    return server, len(tools)
