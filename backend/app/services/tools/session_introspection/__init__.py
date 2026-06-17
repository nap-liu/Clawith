"""Session-introspection builtin tools — let an agent inspect the conversations
it is a party to, gated by the conversation partner's authoritative access level
and structurally isolated per-agent.

Dispatched from agent_tools.execute_tool for tool names
``list_sessions`` / ``read_session_messages`` / ``search_sessions``.
"""

from app.services.tools.session_introspection.handlers import (
    handle_list_sessions,
    handle_read_session_messages,
    handle_search_sessions,
)

__all__ = [
    "handle_list_sessions",
    "handle_read_session_messages",
    "handle_search_sessions",
]
