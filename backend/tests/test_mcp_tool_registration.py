from __future__ import annotations
import pytest


async def test_all_mcp_tools_registered():
    from app.mcp_server import mcp
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "list_agents", "get_agent_info", "list_sessions", "get_session", "chat_with_agent",
        "list_available_tools", "list_models",
        "create_agent", "update_agent",
        "set_agent_tools", "set_agent_trigger", "delete_agent_trigger",
        "set_agent_relationships", "edit_agent_soul",
    }
    missing = expected - names
    assert not missing, f"missing tools: {missing}"
