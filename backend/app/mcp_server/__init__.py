"""Clawith MCP Server — outbound channel for external MCP clients.

Tools are registered in G5 (app/mcp_server/tools.py).
"""

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

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
        "\n"
        "With a write-scope PAT you can also provision agents:\n"
        "- list_available_tools / list_models: discover enablable tools / pick a model\n"
        "- create_agent: create a new native agent in your tenant\n"
        "- update_agent: change model, role, autonomy and other settings\n"
        "- set_agent_tools: enable/disable tools\n"
        "- set_agent_trigger / delete_agent_trigger: manage autonomous triggers\n"
        "- set_agent_relationships: wire up A2A / human collaboration\n"
        "- edit_agent_soul: edit Personality / Boundaries\n"
        "- start_agent / stop_agent: control an agent's runtime (confirm-guided)\n"
        "- delete_agent: soft-delete an agent (confirm-guided, restorable) / "
        "restore_agent: undo a soft-delete (pass the agent id)\n"
    ),
    # FastMCP otherwise auto-enables DNS-rebinding Host validation that only allows
    # localhost, which 421-rejects every real domain (e.g. ai.yeyecha.com or a
    # local-* host). This server is reached via a configurable public domain behind
    # nginx/TLS, authenticated by per-user PAT, and consumed by CLI MCP clients (not
    # browsers) — so Host allow-listing adds no security here; the PAT bearer check does.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# Set the streamable HTTP path to "/" so that when the sub-app is mounted at
# /mcp the endpoint is reachable at /mcp, not /mcp/mcp.
mcp.settings.streamable_http_path = "/"

# Register tool definitions (import for @mcp.tool() side-effects).
from app.mcp_server import tools  # noqa: F401,E402  read/chat tools
from app.mcp_server import tools_discovery  # noqa: F401,E402  list_available_tools / list_models
from app.mcp_server import tools_provisioning  # noqa: F401,E402  create_agent / update_agent
from app.mcp_server import tools_config  # noqa: F401,E402  set_agent_* / edit_agent_soul
from app.mcp_server import tools_lifecycle  # noqa: F401,E402  start/stop/delete/restore_agent
