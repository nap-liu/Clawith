"""MCP resource discovery and import tools."""

import uuid


async def _discover_resources(agent_id: uuid.UUID, arguments: dict) -> str:
    """Search Smithery registry for MCP servers."""
    query = arguments.get("query", "")
    if not query:
        return "❌ Please provide a search query describing the capability you need."
    max_results = min(arguments.get("max_results", 5), 10)

    from app.services.resource_discovery import search_smithery

    return await search_smithery(query, max_results, agent_id=agent_id)


async def _import_mcp_server(agent_id: uuid.UUID, arguments: dict) -> str:
    """Import an MCP server.

    Direct import (no third-party account required) is the primary path; the
    Smithery registry path is only used when the caller explicitly passes a
    `server_id` that resolves on Smithery.

    Accepted shapes (any of):
      • mcp_url   = "https://..."                                     bare URL
      • mcp_config = {"url": "...", "headers": {...}}                 single-server spec
      • mcp_config = {"mcpServers": {"<name>": {...}}}                standard MCP config
      • mcp_config = "<JSON-stringified version of any of the above>"
      • config = "<same as mcp_config>"                               legacy field name
      • server_id = "@anthropic/brave-search"                         Smithery (advanced)
    """
    import json as _json
    from app.services.mcp_config_parser import parse_mcp_input

    reauthorize = bool(arguments.get("reauthorize", False))

    # Sniff every plausible direct-import field
    parsed = None
    for field in ("mcp_url", "mcp_config", "config", "url"):
        candidate = arguments.get(field)
        parsed = parse_mcp_input(candidate)
        if parsed:
            break

    if parsed:
        if parsed.get("error") and not parsed.get("url"):
            return f"❌ {parsed['error']}"
        if parsed.get("transport") == "stdio":  # stdio self-install via aio-sandbox hub
            from app.services.resource_discovery import import_mcp_stdio_direct

            return await import_mcp_stdio_direct(agent_id, parsed)
        if parsed.get("url"):
            from app.services.resource_discovery import import_mcp_direct

            server_name = arguments.get("server_name") or parsed.get("name") or arguments.get("server_id")
            api_key = arguments.get("api_key") or parsed.get("api_key")
            headers = parsed.get("headers")
            warning = parsed.get("_warning")
            result = await import_mcp_direct(
                mcp_url=parsed["url"],
                agent_id=agent_id,
                server_name=server_name,
                api_key=api_key,
                headers=headers,
            )
            if warning:
                result = f"ℹ️ {warning}\n\n{result}"
            return result

    # Smithery path — opt-in, only when caller explicitly provided server_id
    server_id = (arguments.get("server_id") or "").strip()
    if server_id:
        # Legacy callers may still pass `config` as JSON string; normalize before forwarding.
        smithery_config = arguments.get("config")
        if isinstance(smithery_config, str):
            try:
                smithery_config = _json.loads(smithery_config) if smithery_config else None
            except _json.JSONDecodeError:
                smithery_config = None
        if smithery_config is not None and not isinstance(smithery_config, dict):
            smithery_config = None
        from app.services.resource_discovery import import_mcp_from_smithery

        return await import_mcp_from_smithery(server_id, agent_id, smithery_config, reauthorize=reauthorize)

    return (
        "❌ Provide one of:\n"
        "• `mcp_url`: full http/https endpoint of the MCP server, e.g. "
        "`https://mcp-gw.dingtalk.com/server/<id>?key=<token>`\n"
        "• `mcp_config`: standard `mcpServers` JSON config (object or JSON string)\n"
        "• `server_id`: Smithery registry ID (advanced — only if you want to discover via the Smithery registry)"
    )

__all__ = [name for name in globals() if not name.startswith("__")]
