"""Agent-owned MCP installation lifecycle.

This module is the single authority for private server identity, creator-only
configuration projection, assignment removal, and private-server cleanup.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import defaultdict
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import delete, func, select

from app.config import get_settings
from app.database import async_session
from app.models.agent import Agent
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.models.tool import AgentTool, Tool


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (value or "mcp").lower()).strip("-")
    return value[:42] or "mcp"


def build_mcp_tool_name(server_name: str, raw_name: str) -> str:
    """Return a stable globally-unique-compatible Tool.name (varchar(100))."""
    candidate = f"mcp_{server_name}_{raw_name}"
    if len(candidate) <= 100:
        return candidate
    digest = hashlib.sha256(candidate.encode()).hexdigest()[:12]
    return f"{candidate[:87]}-{digest}"


def build_installation_key(
    kind: str,
    *,
    explicit_name: str | None = None,
    url: str | None = None,
    command: str | None = None,
    args: list | None = None,
    smithery_id: str | None = None,
) -> str:
    """Build a stable non-secret identity for one private installation."""
    if smithery_id:
        identity = f"smithery:{smithery_id.lstrip('@').strip().lower()}"
    elif explicit_name:
        identity = f"{kind}:name:{explicit_name.strip().lower()}"
    elif kind == "http" and url:
        parsed = urlsplit(url)
        # Query values may be credentials.  Key names still distinguish APIs
        # while credential rotation keeps the same logical installation.
        query_keys = sorted({key for key, _value in parse_qsl(parsed.query, keep_blank_values=True)})
        identity = json.dumps(
            ["http", parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", query_keys],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    else:
        identity = json.dumps(
            ["stdio", command or "", list(args or [])],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return f"{kind}:{hashlib.sha256(identity.encode()).hexdigest()}"


async def get_or_create_owned_server(
    db,
    *,
    agent_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    installation_key: str,
    display_name: str,
    transport: str,
    url_template: str | None = None,
    headers_template: dict | None = None,
    credential_template: str | None = None,
    command_template: str | None = None,
    args_template: list | None = None,
    env_template: dict | None = None,
) -> MCPServer:
    """Lock the owner, upsert its private server and exact agent override."""
    await db.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update())
    server = (
        await db.execute(
            select(MCPServer).where(
                MCPServer.owner_agent_id == agent_id,
                MCPServer.installation_key == installation_key,
            )
        )
    ).scalar_one_or_none()

    if server is None:
        agent8 = str(agent_id).replace("-", "")[:8]
        base = f"{_slug(display_name)}-a{agent8}"
        name = base[:100]
        suffix = 2
        while (
            await db.execute(
                select(MCPServer.id).where(
                    MCPServer.tenant_id == tenant_id,
                    MCPServer.name == name,
                )
            )
        ).scalar_one_or_none() is not None:
            tail = f"-{suffix}"
            name = f"{base[:100-len(tail)]}{tail}"
            suffix += 1
        server = MCPServer(
            tenant_id=tenant_id,
            name=name,
            display_name=display_name[:200],
            # Private runtime data lives only in the owner override.
            base_url_template="",
            headers_template={},
            credential_template=None,
            transport=transport,
            command_template=None,
            args_template=None,
            env_template=None,
            owner_agent_id=agent_id,
            installation_key=installation_key,
        )
        db.add(server)
        await db.flush()
    else:
        server.display_name = display_name[:200]
        server.transport = transport

    override = (
        await db.execute(
            select(MCPServerOverride).where(
                MCPServerOverride.mcp_server_id == server.id,
                MCPServerOverride.scope_type == "agent",
                MCPServerOverride.scope_id == agent_id,
            )
        )
    ).scalar_one_or_none()
    values = {
        "url_template": url_template,
        "headers_template": headers_template or {},
        "credential_template": credential_template,
        "command_template": command_template,
        "args_template": list(args_template or []),
        "env_template": dict(env_template or {}),
    }
    if override is None:
        override = MCPServerOverride(
            mcp_server_id=server.id,
            scope_type="agent",
            scope_id=agent_id,
            **values,
        )
        db.add(override)
    else:
        for key, value in values.items():
            setattr(override, key, value)
    await db.flush()
    return server


async def ensure_owned_agent_tool(db, agent_id: uuid.UUID, tool_id: uuid.UUID, *, config: dict | None = None) -> None:
    assignment = (
        await db.execute(
            select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id)
        )
    ).scalar_one_or_none()
    if assignment is None:
        db.add(
            AgentTool(
                agent_id=agent_id,
                tool_id=tool_id,
                enabled=True,
                source="user_installed",
                installed_by_agent_id=agent_id,
                config=dict(config or {}),
            )
        )
    else:
        assignment.enabled = True
        assignment.source = "user_installed"
        assignment.installed_by_agent_id = agent_id
        assignment.config = dict(config or {})


def _owner_config(server: MCPServer, override: MCPServerOverride | None, tool_configs: list[dict]) -> dict:
    if server.transport == "stdio":
        return {
            "command": override.command_template if override else None,
            "args": list((override.args_template if override else None) or []),
            "env": dict((override.env_template if override else None) or {}),
        }
    config = {
        "url": override.url_template if override else None,
        "headers": dict((override.headers_template if override else None) or {}),
        "credential": override.credential_template if override else None,
    }
    operational = next((c for c in tool_configs if c), None)
    if operational:
        config["runtime"] = operational
    return config


async def list_installed_mcp_servers(agent_id: uuid.UUID) -> str:
    """Return exact installed inventory; private config is projected automatically."""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(AgentTool, Tool, MCPServer)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .outerjoin(MCPServer, MCPServer.id == Tool.mcp_server_id)
                .where(AgentTool.agent_id == agent_id, Tool.type == "mcp")
                .order_by(MCPServer.name, Tool.name)
            )
        ).all()
        grouped: dict[uuid.UUID, list[tuple[AgentTool, Tool, MCPServer]]] = defaultdict(list)
        legacy = 0
        for assignment, tool, server in rows:
            if server is None:
                legacy += 1
                continue
            grouped[server.id].append((assignment, tool, server))

        payload = []
        for server_id, items in grouped.items():
            server = items[0][2]
            owner = server.owner_agent_id == agent_id
            removable = any(
                at.source == "user_installed" and at.installed_by_agent_id == agent_id
                for at, _tool, _server in items
            )
            item = {
                "mcp_server_id": str(server_id),
                "name": server.name,
                "display_name": server.display_name,
                "transport": server.transport,
                "tool_count": len(items),
                "created_by_current_agent": owner,
                "removable": removable,
            }
            if owner:
                override = (
                    await db.execute(
                        select(MCPServerOverride).where(
                            MCPServerOverride.mcp_server_id == server.id,
                            MCPServerOverride.scope_type == "agent",
                            MCPServerOverride.scope_id == agent_id,
                        )
                    )
                ).scalar_one_or_none()
                item["config"] = _owner_config(server, override, [at.config or {} for at, _, _ in items])
            payload.append(item)
        result = {"mcp_servers": payload}
        if legacy:
            result["legacy_tool_count"] = legacy
            result["legacy_notice"] = "旧版 MCP 工具尚无精确 server id；重新导入后即可自主卸载。"
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)


async def uninstall_mcp_server(agent_id: uuid.UUID, server_id: uuid.UUID) -> str:
    """Detach a self-installed server and delete an unreferenced private owner server."""
    cleanup_prefix: str | None = None
    async with async_session() as db:
        await db.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update())
        server = (
            await db.execute(select(MCPServer).where(MCPServer.id == server_id).with_for_update())
        ).scalar_one_or_none()
        if server is None:
            return json.dumps({"ok": True, "state": "already_uninstalled", "mcp_server_id": str(server_id)})

        assignments = (
            await db.execute(
                select(AgentTool)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(
                    AgentTool.agent_id == agent_id,
                    Tool.mcp_server_id == server_id,
                    AgentTool.source == "user_installed",
                    AgentTool.installed_by_agent_id == agent_id,
                )
            )
        ).scalars().all()
        if not assignments:
            return json.dumps(
                {"ok": False, "error": "not_removable", "mcp_server_id": str(server_id)},
                ensure_ascii=False,
            )

        removed = len(assignments)
        await db.execute(delete(AgentTool).where(AgentTool.id.in_([row.id for row in assignments])))
        await db.flush()
        remaining = (
            await db.execute(
                select(func.count())
                .select_from(AgentTool)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(Tool.mcp_server_id == server_id)
            )
        ).scalar_one()
        private_deleted = False
        if server.owner_agent_id == agent_id and remaining == 0:
            if server.transport == "stdio":
                cleanup_prefix = f"{server.name}__"
            await db.delete(server)  # cascades Tool -> AgentTool and overrides
            private_deleted = True
        await db.commit()

    runtime_cleaned = None
    cleanup_error = None
    if cleanup_prefix:
        settings = get_settings()
        if settings.SANDBOX_API_URL:
            try:
                from app.services.sandbox_mcp_host import SandboxMcpHost

                await SandboxMcpHost(settings.SANDBOX_API_URL, settings.SANDBOX_API_KEY).deregister_prefix(cleanup_prefix)
                runtime_cleaned = True
            except Exception as exc:  # logical uninstall remains complete
                runtime_cleaned = False
                cleanup_error = f"{type(exc).__name__}: {str(exc)[:160]}"

    return json.dumps(
        {
            "ok": True,
            "state": "uninstalled",
            "mcp_server_id": str(server_id),
            "removed_tools": removed,
            "private_server_deleted": private_deleted,
            "runtime_cleaned": runtime_cleaned,
            "cleanup_error": cleanup_error,
            "other_agents_affected": 0,
            "effective": "immediate",
        },
        ensure_ascii=False,
    )
