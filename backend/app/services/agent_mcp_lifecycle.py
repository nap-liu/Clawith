"""Current-Agent MCP inventory and uninstall lifecycle.

This service deliberately reuses the existing AgentTool provenance model.
It does not introduce a second MCP ownership model or delete shared servers.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import delete, select

from app.database import async_session
from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.tool import AgentTool, Tool
from app.services.mcp_access import removable_mcp_server_ids, visible_mcp_installations
from app.services.mcp_catalog_policy import shared_catalog
from app.services.turn_tool_settings import effective_assignment
from app.services.mcp_refresh_snapshot import MCPRefreshChanged, MCPRefreshUnavailable
from app.services.mcp_refresh_service import (
    refresh_mcp_server_tools,
)


async def list_installed_mcp_servers(agent_id: uuid.UUID) -> str:
    """List concise server-level MCP inventory with exact lifecycle IDs."""
    async with async_session() as db:
        pairs = [(tool, assignment) for tool, assignment in
                 await visible_mcp_installations(db, agent_id) if assignment is not None]
        server_ids = {tool.mcp_server_id for tool, _ in pairs if tool.mcp_server_id}
        removable_ids = await removable_mcp_server_ids(db, agent_id, server_ids)
        server_rows = (await db.scalars(
            select(MCPServer).where(MCPServer.id.in_(server_ids))
            .order_by(MCPServer.display_name, MCPServer.id)
        )).all()
        groups = {server.id: {
            "mcp_server_id": str(server.id), "display_name": server.display_name,
            "transport": server.transport, "tool_count": 0,
            "enabled_tool_count": 0, "removable": server.id in removable_ids,
        } for server in server_rows}
        legacy_tool_count = 0
        for tool, assignment in pairs:
            if tool.mcp_server_id is None:
                legacy_tool_count += 1
                continue
            item = groups[tool.mcp_server_id]
            item["tool_count"] += 1
            item["enabled_tool_count"] += int(
                effective_assignment(agent_id, tool, assignment) is not None
            )
        servers = list(groups.values())

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
                select(Agent).where(Agent.id == agent_id)
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
            result = await refresh_mcp_server_tools(
                db,
                effective_server_id,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                assign_to_agent=True,
            )
            effective_server_id = result.server_id or server_id
            await db.commit()
        except (MCPRefreshUnavailable, MCPRefreshChanged) as exc:
            await db.rollback()
            return json.dumps({"ok": False, "error": "mcp_refresh_unavailable", "detail": str(exc)}, ensure_ascii=False)
        except PermissionError as exc:
            detail = str(exc)
            await db.rollback()
            return json.dumps(
                {
                    "ok": False,
                    "error": "agent_refresh_not_isolated",
                    "mcp_server_id": str(server_id),
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
                    "mcp_server_id": str(server_id),
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
        from app.services.mcp_catalog_locks import lock_mcp_catalogs

        await lock_mcp_catalogs(db, [server_id], agent_id=agent_id)
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

        is_shared = await shared_catalog(db, server)
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
                    and not is_shared
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
            and not is_shared
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
        server_deleted = remaining_server_tools is None and not is_shared
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
