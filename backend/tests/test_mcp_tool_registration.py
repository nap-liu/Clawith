from __future__ import annotations
import pytest


async def test_all_mcp_tools_registered():
    from app.mcp_server import mcp
    tools = await mcp.list_tools()
    names = {t.name for t in tools}

    expected = {
        # read/chat
        "list_agents", "get_agent_info", "list_sessions", "get_session", "chat_with_agent",
        # discovery
        "list_available_tools", "list_models",
        # provisioning
        "create_agent", "update_agent",
        # config
        "set_agent_tools", "set_agent_trigger", "delete_agent_trigger", "set_agent_relationships",
        "set_agent_access", "list_agent_triggers", "update_agent_trigger", "set_agent_tool_config",
        # files
        "read_agent_file", "write_agent_file",
        # lifecycle
        "start_agent", "stop_agent", "delete_agent", "restore_agent",
        # mcp servers
        "install_agent_mcp_server", "uninstall_agent_mcp_server",
        # credentials
        "list_agent_credentials", "set_agent_credential", "delete_agent_credential",
        # schedules
        "list_agent_schedules", "set_agent_schedule", "delete_agent_schedule", "run_agent_schedule",
        # channel
        "get_agent_channel_config", "set_agent_channel_config", "delete_agent_channel_config",
    }
    missing = expected - names
    assert not missing, f"missing tools: {missing}"

    # edit_agent_soul was superseded by write_agent_file and must not be registered
    assert "edit_agent_soul" not in names, "edit_agent_soul should not be registered (superseded by write_agent_file)"
