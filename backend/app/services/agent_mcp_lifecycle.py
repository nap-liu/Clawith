"""Current-Agent MCP inventory and uninstall lifecycle.

This service deliberately reuses the existing AgentTool provenance model.
It does not introduce a second MCP ownership model or delete shared servers.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import delete, func, select

from app.database import async_session
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tool import AgentTool, Tool
from app.services.mcp_refresh_service import (
    ensure_agent_mcp_server_isolated,
    refresh_mcp_server_tools,
)


async def list_installed_mcp_servers(agent_id: uuid.UUID) -> str:
    """List concise server-level MCP inventory with exact lifecycle IDs."""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(
                    MCPServer.id.label("mcp_server_id"),
                    MCPServer.display_name,
                    MCPServer.transport,
                    func.count(AgentTool.id).label("tool_count"),
                    func.count(AgentTool.id)
                    .filter(AgentTool.enabled.is_(True))
                    .label("enabled_tool_count"),
                    func.count(AgentTool.id)
                    .filter(
                        AgentTool.source == "user_installed",
                        AgentTool.installed_by_agent_id == agent_id,
                    )
                    .label("removable_count"),
                )
                .select_from(AgentTool)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .join(MCPServer, MCPServer.id == Tool.mcp_server_id)
                .where(
                    AgentTool.agent_id == agent_id,
                    Tool.type == "mcp",
                )
                .group_by(
                    MCPServer.id,
                    MCPServer.display_name,
                    MCPServer.transport,
                )
                .order_by(MCPServer.display_name, MCPServer.id)
            )
        ).all()

        servers = [
            {
                "mcp_server_id": str(row.mcp_server_id),
                "display_name": row.display_name,
                "transport": row.transport,
                "tool_count": row.tool_count,
                "enabled_tool_count": row.enabled_tool_count,
                "removable": row.removable_count > 0,
            }
            for row in rows
        ]

        legacy_tool_count = await db.scalar(
            select(func.count(AgentTool.id))
            .select_from(AgentTool)
            .join(Tool, Tool.id == AgentTool.tool_id)
            .where(
                AgentTool.agent_id == agent_id,
                Tool.type == "mcp",
                Tool.mcp_server_id.is_(None),
            )
        )

        result: dict = {"mcp_servers": servers}
        if legacy_tool_count:
            result["legacy_tool_count"] = legacy_tool_count
            result["legacy_notice"] = (
                "These historical MCP tools have no mcp_server_id. "
                "Re-import the same MCP configuration to attach an exact server ID."
            )
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)


async def refresh_mcp_server(
    agent_id: uuid.UUID,
    server_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    session_id: str,
) -> str:
    """Refresh one MCP server installed exclusively by the current Agent."""
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

        effective_server_id = server_id
        try:
            server = await ensure_agent_mcp_server_isolated(
                db,
                server,
                agent_id,
            )
            effective_server_id = server.id
            result = await refresh_mcp_server_tools(
                db,
                effective_server_id,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                assign_to_agent=True,
            )
            await db.commit()
        except PermissionError as exc:
            detail = str(exc)
            await db.rollback()
            return json.dumps(
                {
                    "ok": False,
                    "error": "agent_refresh_not_isolated",
                    "mcp_server_id": str(effective_server_id),
                    "detail": detail,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:500]
            await db.rollback()
            return json.dumps(
                {
                    "ok": False,
                    "error": "refresh_failed",
                    "mcp_server_id": str(effective_server_id),
                    "detail": detail,
                },
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "ok": True,
                "mcp_server_id": str(effective_server_id),
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
                if (
                    tool is not None
                    and tool.type == "mcp"
                    and tool.source == "agent"
                    and server.created_by_user_id is None
                ):
                    await db.delete(tool)
                    deleted_orphan_tools += 1

        await db.flush()
        remaining_server_assignments = (
            await db.execute(
                select(AgentTool.id)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(Tool.mcp_server_id == server_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if (
            remaining_server_assignments is None
            and server.created_by_user_id is None
        ):
            stale_agent_tools = (
                await db.execute(
                    select(Tool).where(
                        Tool.mcp_server_id == server_id,
                        Tool.source == "agent",
                    )
                )
            ).scalars().all()
            for tool in stale_agent_tools:
                await db.delete(tool)
                deleted_orphan_tools += 1
            await db.flush()

        remaining_server_tools = (
            await db.execute(
                select(Tool.id)
                .where(Tool.mcp_server_id == server_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        server_deleted = remaining_server_tools is None
        if server_deleted:
            await db.delete(server)

        await db.commit()
        return json.dumps(
            {
                "ok": True,
                "state": "uninstalled",
                "mcp_server_id": str(server_id),
                "removed_bindings": len(assignment_ids),
                "deleted_orphan_tools": deleted_orphan_tools,
                "removed_tool_names": tool_names,
                "server_deleted": server_deleted,
                "shared_server_preserved": not server_deleted,
                "other_agents_affected": 0,
                "effective": "immediate",
            },
            ensure_ascii=False,
        )
