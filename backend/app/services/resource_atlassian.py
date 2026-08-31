"""Atlassian Rovo MCP tool seeding and credential refresh."""

from loguru import logger
from sqlalchemy import select

from app.database import async_session
from app.models.tool import Tool


# ── Atlassian Rovo MCP Auto-Seeding ─────────────────────────────────────────

ATLASSIAN_ROVO_MCP_URL = "https://mcp.atlassian.com/v1/mcp"
ATLASSIAN_ROVO_SERVER_NAME = "Atlassian Rovo"
ATLASSIAN_ROVO_TOOL_PREFIX = "atlassian_rovo_"


async def seed_atlassian_rovo_tools(api_key: str) -> None:
    """Connect to Atlassian Rovo MCP and seed all available tools as platform-level MCP tools.

    Called on startup when an API key is configured. Existing tools are updated in-place;
    new tools discovered from the server are created. The api_key is stored in each tool's
    config so _execute_mcp_tool can authenticate requests.
    """
    from app.services.mcp_client import MCPClient

    logger.info(f"[AtlassianRovo] Connecting to {ATLASSIAN_ROVO_MCP_URL} ...")
    try:
        client = MCPClient(ATLASSIAN_ROVO_MCP_URL, api_key=api_key)
        tools_discovered = await client.list_tools()
    except Exception as e:
        logger.error(f"[AtlassianRovo] Could not list tools: {e}")
        return

    if not tools_discovered:
        logger.warning("[AtlassianRovo] No tools returned from server")
        return

    logger.info(f"[AtlassianRovo] Discovered {len(tools_discovered)} tools")

    async with async_session() as db:
        upserted = 0
        for mcp_tool in tools_discovered:
            raw_name = mcp_tool.get("name", "")
            if not raw_name:
                continue

            tool_name = f"{ATLASSIAN_ROVO_TOOL_PREFIX}{raw_name}"
            tool_display = f"Atlassian: {raw_name}"
            tool_desc = mcp_tool.get("description", "")[:500]
            tool_schema = mcp_tool.get("inputSchema", {"type": "object", "properties": {}})

            # Determine icon based on tool name hints
            if "jira" in raw_name.lower() or "issue" in raw_name.lower():
                icon = "🔵"
            elif "confluence" in raw_name.lower() or "page" in raw_name.lower():
                icon = "📘"
            elif "compass" in raw_name.lower() or "component" in raw_name.lower():
                icon = "🧭"
            else:
                icon = "🔷"

            existing_r = await db.execute(select(Tool).where(Tool.name == tool_name))
            existing_tool = existing_r.scalar_one_or_none()

            if existing_tool:
                # Update description and schema in case they changed
                existing_tool.description = tool_desc
                existing_tool.parameters_schema = tool_schema
                existing_tool.config = {"api_key": api_key}
            else:
                tool = Tool(
                    name=tool_name,
                    display_name=tool_display,
                    description=tool_desc,
                    type="mcp",
                    category="atlassian",
                    icon=icon,
                    parameters_schema=tool_schema,
                    mcp_server_url=ATLASSIAN_ROVO_MCP_URL,
                    mcp_server_name=ATLASSIAN_ROVO_SERVER_NAME,
                    mcp_tool_name=raw_name,
                    enabled=True,
                    is_default=False,
                    config={"api_key": api_key},
                    source="admin",
                )
                db.add(tool)
                upserted += 1

        await db.commit()

    logger.info(f"[AtlassianRovo] Seeded {upserted} new Atlassian Rovo tools")


async def refresh_atlassian_rovo_api_key(api_key: str) -> None:
    """Update the stored api_key in all Atlassian Rovo tool records.

    Called when the user updates the API key via the config UI.
    """
    async with async_session() as db:
        from sqlalchemy import update as _update
        await db.execute(
            _update(Tool)
            .where(Tool.mcp_server_name == ATLASSIAN_ROVO_SERVER_NAME, Tool.type == "mcp")
            .values(config={"api_key": api_key})
        )
        await db.commit()
    logger.info("[AtlassianRovo] API key refreshed for all Rovo tools")
