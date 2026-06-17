"""Clawith MCP Server — outbound channel for external MCP clients.

Tools are registered in G5 (app/mcp_server/tools.py).
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "Clawith",
    instructions=(
        "Welcome to the Clawith MCP server. "
        "Authenticate by passing your Personal Access Token as a Bearer token in the Authorization header. "
        "\n\n"
        "Available operations (registered in a later release):\n"
        "- list_agents: discover agents visible to your account\n"
        "- chat_with_agent: send a message to an agent; by default continues the most recent session; "
        "pass new_conversation=true to start fresh, or session_id to resume a specific session\n"
        "- list_sessions: list chat sessions for a given agent\n"
        "- get_session: retrieve the message history for a specific session\n"
    ),
)

# Set the streamable HTTP path to "/" so that when the sub-app is mounted at
# /mcp the endpoint is reachable at /mcp, not /mcp/mcp.
mcp.settings.streamable_http_path = "/"

# TODO(G5): from app.mcp_server import tools  # noqa — registers @mcp.tool() definitions
