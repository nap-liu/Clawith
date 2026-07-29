"""Current-Agent MCP inventory and uninstall lifecycle.

This service deliberately reuses the existing AgentTool provenance model.
It does not introduce a second MCP ownership model or delete shared servers.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict

from sqlalchemy import delete, select

from app.database import async_session
from app.models.agent import Agent
from app.models.tool import AgentTool, Tool
from app.models.mcp_server import MCPServer
from app.services.mcp_refresh_service import refresh_mcp_server_tools


def _normalized_configs(
    items: list[tuple[AgentTool, Tool, MCPServer]],
    agent_id: uuid.UUID,
) -> list[dict]:
    """Return distinct per-assignment configs without reading shared secrets."""
    configs: list[dict] = []
    seen: set[str] = set()
    for assignment, _tool, _server in items:
        if not (
            assignment.source == "user_installed"
            and assignment.installed_by_agent_id == agent_id
        ):
            continue
        config = dict(assignment.config or {})
        if not config:
            continue
        marker = json.dumps(config, ensure_ascii=False, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        configs.append(config)
    return configs


async def list_installed_mcp_servers(agent_id: uuid.UUID) -> str:
    """List the current Agent's installed MCP servers with exact server IDs."""
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
        legacy_tools: list[dict] = []
        for assignment, tool, server in rows:
            if server is None:
                legacy_tools.append(
                    {
                        "tool_id": str(tool.id),
                        "tool_name": tool.name,
                        "display_name": tool.display_name,
                        "mcp_server_name": tool.mcp_server_name,
                    }
                )
                continue
            grouped[server.id].append((assignment, tool, server))

        servers: list[dict] = []
        for server_id, items in grouped.items():
            server = items[0][2]
            removable = any(
                assignment.source == "user_installed"
                and assignment.installed_by_agent_id == agent_id
                for assignment, _tool, _server in items
            )
            item: dict = {
                "mcp_server_id": str(server_id),
                "name": server.name,
                "display_name": server.display_name,
                "transport": server.transport,
                "tool_count": len(items),
                "enabled_tool_count": sum(1 for assignment, _, _ in items if assignment.enabled),
                "installed_by_current_agent": removable,
                "removable": removable,
                "tools": [
                    {
                        "tool_id": str(tool.id),
                        "name": tool.name,
                        "display_name": tool.display_name,
                        "enabled": assignment.enabled,
                    }
                    for assignment, tool, _server in items
                ],
            }
            if removable:
                configs = _normalized_configs(items, agent_id)
                if len(configs) == 1:
                    item["config"] = configs[0]
                elif configs:
                    item["configs"] = configs
            servers.append(item)

        result: dict = {"mcp_servers": servers}
        if legacy_tools:
            result["legacy_tools"] = legacy_tools
            result["legacy_notice"] = (
                "These historical MCP tools have no mcp_server_id. "
                "Re-import the same MCP configuration to attach an exact server ID."
            )
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)


async def refresh_mcp_server(agent_id: uuid.UUID, server_id: uuid.UUID) -> str:
    """Refresh one MCP server with the current Agent's effective configuration."""
    async with async_session() as db:
        agent = (
            await db.execute(
                select(Agent).where(Agent.id == agent_id).with_for_update()
            )
        ).scalar_one_or_none()
        if agent is None:
            return json.dumps(
                {"ok": False, "error": "agent_not_found"},
                ensure_ascii=False,
            )

        server = (
            await db.execute(select(MCPServer).where(MCPServer.id == server_id))
        ).scalar_one_or_none()
        if server is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": "mcp_server_not_found",
                    "mcp_server_id": str(server_id),
                },
                ensure_ascii=False,
            )

        assignment = (
            await db.execute(
                select(AgentTool.id)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(
                    AgentTool.agent_id == agent_id,
                    Tool.mcp_server_id == server_id,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if assignment is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": "not_installed_or_not_assigned",
                    "mcp_server_id": str(server_id),
                },
                ensure_ascii=False,
            )

        try:
            result = await refresh_mcp_server_tools(
                db,
                server_id,
                agent_id=agent_id,
                assign_to_agent=True,
            )
            await db.commit()
        except Exception as exc:
            await db.rollback()
            return json.dumps(
                {
                    "ok": False,
                    "error": "refresh_failed",
                    "mcp_server_id": str(server_id),
                    "detail": str(exc)[:500],
                },
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "ok": True,
                "mcp_server_id": str(server_id),
                **result.to_dict(),
                "effective": "next_turn",
            },
            ensure_ascii=False,
        )


async def uninstall_mcp_server(agent_id: uuid.UUID, server_id: uuid.UUID) -> str:
    """Detach only the current Agent's own installation from one exact server."""
    async with async_session() as db:
        # Serialize lifecycle writes for one Agent without adding a new schema
        # or ownership model. Other Agents remain fully concurrent.
        await db.execute(
            select(Agent.id).where(Agent.id == agent_id).with_for_update()
        )
        server = (
            await db.execute(select(MCPServer).where(MCPServer.id == server_id))
        ).scalar_one_or_none()
        if server is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": "mcp_server_not_found",
                    "mcp_server_id": str(server_id),
                },
                ensure_ascii=False,
            )

        pairs = (
            await db.execute(
                select(AgentTool, Tool)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(
                    AgentTool.agent_id == agent_id,
                    Tool.mcp_server_id == server_id,
                    AgentTool.source == "user_installed",
                    AgentTool.installed_by_agent_id == agent_id,
                )
            )
        ).all()
        if not pairs:
            return json.dumps(
                {
                    "ok": False,
                    "error": "not_installed_or_not_removable",
                    "mcp_server_id": str(server_id),
                },
                ensure_ascii=False,
            )

        assignment_ids = [assignment.id for assignment, _tool in pairs]
        tool_ids = {tool.id for _assignment, tool in pairs}
        tool_names = [tool.name for _assignment, tool in pairs]

        await db.execute(delete(AgentTool).where(AgentTool.id.in_(assignment_ids)))
        await db.flush()

        deleted_orphan_tools = 0
        for tool_id in tool_ids:
            remaining = (
                await db.execute(
                    select(AgentTool.id).where(AgentTool.tool_id == tool_id).limit(1)
                )
            ).scalar_one_or_none()
            if remaining is None:
                tool = (
                    await db.execute(select(Tool).where(Tool.id == tool_id))
                ).scalar_one_or_none()
                if tool is not None and tool.type == "mcp":
                    await db.delete(tool)
                    deleted_orphan_tools += 1

        await db.commit()
        return json.dumps(
            {
                "ok": True,
                "state": "uninstalled",
                "mcp_server_id": str(server_id),
                "removed_bindings": len(assignment_ids),
                "deleted_orphan_tools": deleted_orphan_tools,
                "removed_tool_names": tool_names,
                "shared_server_preserved": True,
                "other_agents_affected": 0,
                "effective": "immediate",
            },
            ensure_ascii=False,
        )
