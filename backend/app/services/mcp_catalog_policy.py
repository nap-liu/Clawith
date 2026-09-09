"""Shared catalog authority and the narrower Agent configuration surface."""

from types import SimpleNamespace
from dataclasses import replace

from fastapi import HTTPException
from sqlalchemy import select

from app.models.mcp_server import MCPServer
from app.models.tool import Tool
from app.services.llm.failure_outcome import render_message

SHARED_OVERRIDE_FIELDS = frozenset({"credential_template", "headers_template", "env_template"})
OVERRIDE_FIELDS = (*SHARED_OVERRIDE_FIELDS, "url_template", "command_template", "args_template", "system_prompt_block")
SHARED_TOOL_CONFIG_FIELDS = frozenset({"api_key", "headers", "env"})


async def shared_catalog(db, server):
    """Admin-owned or unclassified catalogs stay shared, even with zero users.

    Tool.source describes catalog origin; AgentTool.source only describes a
    binding and cannot turn an administrator's catalog into a private one.
    """
    if server.created_by_user_id is not None:
        return True
    sources = (await db.scalars(select(Tool.source).where(Tool.mcp_server_id == server.id))).all()
    return not sources or any(source != "agent" for source in sources)


def safe_shared_override(override):
    if override is None:
        return None
    values = {
        key: getattr(override, key, None) if key in SHARED_OVERRIDE_FIELDS else None for key in OVERRIDE_FIELDS
    }
    values.update({key: getattr(override, key, None)
                   for key in ("id", "scope_type", "scope_id", "mcp_server_id")})
    return SimpleNamespace(**values)


async def validate_agent_override(db, server, values):
    if await shared_catalog(db, server) and any(
        key not in SHARED_OVERRIDE_FIELDS and value not in (None, "", [], {})
        for key, value in values.items()
    ):
        raise HTTPException(403, detail=render_message("mcpAccess.sharedDefinition"))


async def can_remove_private_tool(db, tool):
    if tool.type != "mcp" or tool.source != "agent":
        return False
    if tool.mcp_server_id is None:
        return True
    server = await db.get(MCPServer, tool.mcp_server_id)
    return server is not None and not await shared_catalog(db, server)


async def validate_shared_tool_config(db, tool, values):
    if tool.type != "mcp" or tool.mcp_server_id is None:
        return
    server = await db.get(MCPServer, tool.mcp_server_id)
    if server and await shared_catalog(db, server):
        if set(values) - SHARED_TOOL_CONFIG_FIELDS:
            raise HTTPException(403, detail=render_message("mcpAccess.sharedDefinition"))
        for key, value in values.items():
            valid = isinstance(value, str) if key == "api_key" else (
                isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
            )
            if not valid:
                raise HTTPException(422, detail=render_message("mcpAccess.invalidConfig"))


def apply_shared_tool_config(config, values):
    """One credential composition for execution and connection checking."""
    headers = values.get("headers")
    env = values.get("env")
    return replace(config,
        credential_template=values.get("api_key") or config.credential_template,
        headers_template={**(config.headers_template or {}), **(headers if isinstance(headers, dict) else {})},
        env_template={**(config.env_template or {}), **(env if isinstance(env, dict) else {})},
    )
