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
        "Read/chat operations:\n"
        "- list_agents: discover agents visible to your account\n"
        "- get_agent_info: get detailed info about a specific agent\n"
        "- chat_with_agent: send a message to an agent; by default continues the most recent session; "
        "pass new_conversation=true to start fresh, or session_id to resume a specific session\n"
        "- list_sessions: list chat sessions for a given agent\n"
        "- get_session: retrieve the message history for a specific session\n"
        "\n"
        "With a write-scope PAT you can also provision and manage agents:\n"
        "- list_available_tools / list_models: discover enablable tools / pick a model\n"
        "- create_agent: create a new native agent in your tenant\n"
        "- update_agent: change model, role, autonomy and other settings\n"
        "- set_agent_tools: enable/disable tools\n"
        "- set_agent_trigger / update_agent_trigger / delete_agent_trigger / list_agent_triggers: "
        "manage autonomous triggers\n"
        "- set_agent_relationships: wire up A2A / human collaboration\n"
        "- set_agent_access / set_agent_tool_config: fine-tune access policy and tool parameters\n"
        "- read_agent_file / write_agent_file: read or edit agent workspace files "
        "(e.g. soul.md, memory.md)\n"
        "- start_agent / stop_agent: control an agent's runtime (confirm-guided)\n"
        "- delete_agent: soft-delete an agent (confirm-guided, restorable)\n"
        "- restore_agent: undo a soft-delete (pass the agent id)\n"
        "- install_agent_mcp_server / uninstall_agent_mcp_server: manage MCP server integrations "
        "for an agent (confirm-guided)\n"
        "- list_agent_credentials / set_agent_credential / delete_agent_credential: "
        "manage per-agent encrypted credentials (confirm-guided for mutations)\n"
        "- list_agent_schedules / set_agent_schedule / delete_agent_schedule / run_agent_schedule: "
        "manage scheduled tasks for an agent\n"
        "- get_agent_channel_config / set_agent_channel_config / delete_agent_channel_config: "
        "manage IM channel configuration for an agent\n"
        "\n"
        "Sensitive operations are confirm-guided: first call returns a confirmation prompt; "
        "re-call with confirm=True to execute. "
        "Mutation responses include revert hints (e.g. restore_agent, re-supply credential) "
        "for rollback guidance.\n"
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
from app.mcp_server import tools_config  # noqa: F401,E402  set_agent_* config tools
from app.mcp_server import tools_lifecycle  # noqa: F401,E402  start/stop/delete/restore_agent
from app.mcp_server import tools_files  # noqa: F401,E402  read/write_agent_file
from app.mcp_server import tools_mcp  # noqa: F401,E402  install/uninstall_agent_mcp_server
from app.mcp_server import tools_credentials  # noqa: F401,E402  agent credentials
from app.mcp_server import tools_schedules  # noqa: F401,E402  agent schedules
from app.mcp_server import tools_channel  # noqa: F401,E402  agent channel config
