"""Agent (Digital Employee) API routes."""

from app.api.agent_api_shared import *  # noqa: F401,F403
from app.api.agent_routes_directory import *  # noqa: F401,F403
from app.api.agent_routes_permissions import *  # noqa: F401,F403
from app.api.agent_routes_lifecycle import *  # noqa: F401,F403
from app.api.agent_routes_approvals import *  # noqa: F401,F403


_COMPAT_SYMBOLS = ('_get_active_admin_users', '_serialize_dt', '_archive_agent_task_history', '_lazy_reset_token_counters', '_build_unread_count_by_agent', '_build_unread_count_by_agent_ids', '_serialize_agent_out', 'list_templates', '_agent_to_out', '_agents_to_out', 'list_agents', 'explore_agents', 'create_agent', 'get_agent', 'get_agent_permissions', 'update_agent_permissions', 'get_agent_permission_departments', 'get_agent_permission_members', 'get_agent_permission_candidates', 'update_agent', 'delete_agent', 'start_agent', 'stop_agent', 'list_agent_approvals', 'resolve_agent_approval', 'generate_or_reset_api_key', 'list_gateway_messages')
for _compat_name in _COMPAT_SYMBOLS:
    _compat_symbol = globals().get(_compat_name)
    if _compat_symbol is not None:
        _compat_symbol.__module__ = __name__
