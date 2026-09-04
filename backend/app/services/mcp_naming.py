"""Stable internal names and model-facing labels for MCP capabilities."""

from __future__ import annotations

import hashlib
import re
import uuid

from sqlalchemy import select

from app.models.mcp_server import MCPServer


def _slug(value: str, fallback: str = "mcp") -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "_", value.lower()).strip("_-")
    return slug or fallback


def server_internal_name(display_name: str, server_id: uuid.UUID) -> str:
    """Return an immutable, globally collision-resistant registry name."""
    suffix = server_id.hex[:12]
    return f"{_slug(display_name)[:87]}-{suffix}"[:100]


def tool_function_name(server: MCPServer, remote_name: str) -> str:
    """Return a valid, globally unique function name within varchar(100)."""
    remote = _slug(remote_name, fallback="tool")
    # Existing servers may still have tenant-local names. The immutable id
    # isolates tenants; the raw-name digest also separates names that slugify
    # to the same value (for example, "search.docs" and "search_docs").
    value = f"mcp_{_slug(server.name)}_{server.id.hex[:8]}_{remote}"
    digest = hashlib.sha1(f"{server.id}:{remote_name}".encode()).hexdigest()[:10]
    return f"{value[:89]}_{digest}"


def model_group_label(value: str | None) -> str:
    """Bound a user-managed display label before placing it in a tool schema."""
    return " ".join(str(value or "").split())[:80]


def model_mcp_description(
    description: str | None,
    display_name: str | None,
    tool_display_name: str | None = None,
) -> str:
    label = model_group_label(display_name)
    tool_label = model_group_label(tool_display_name)
    original = str(description or "").strip()
    if not label:
        return original
    local_part = f'; local tool: "{tool_label}"' if tool_label else ""
    prefix = f'[MCP tool group: "{label}"{local_part}]'
    return f"{prefix} {original}" if original else prefix


async def load_mcp_display_names(db, tools: list) -> dict[uuid.UUID, str]:
    server_ids = {tool.mcp_server_id for tool in tools if tool.mcp_server_id}
    if not server_ids:
        return {}
    rows = await db.execute(
        select(MCPServer.id, MCPServer.display_name).where(MCPServer.id.in_(server_ids))
    )
    return {server_id: display_name for server_id, display_name in rows.all()}
