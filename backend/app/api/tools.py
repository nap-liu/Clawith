"""Tool management API — CRUD for tools and per-agent assignments."""

import sys
from types import ModuleType

from app.api import (
    tools_shared as _tools_shared,
    tools_models as _tools_models,
)
from app.api.tools_models import (
    AgentToolConfigUpdate,
    AgentToolUpdate,
    BulkToolUpdateItem,
    CategoryConfigUpdate,
    EmailTestRequest,
    MCPServerUpdate,
    MCPTestRequest,
    ToolCreate,
    ToolUpdate,
)
from app.api.tools_shared import (
    CATEGORY_CONFIG_PRIMARY_TOOL,
    REQUIRED_AGENT_TOOL_NAMES,
    SUBAGENT_TOOL_NAMES,
    USER_PROJECT_TOOL_NAMES,
    _agent_visible_tool_clause,
    _can_view_unmasked_company_config,
    _decrypt_sensitive_fields,
    _encrypt_sensitive_fields,
    _feature_visible_tool_clause,
    _get_sensitive_keys,
    _globally_visible_tool_clause,
    _is_platform_admin,
    _load_agent_for_tool_scope,
    _load_agent_tool_assignments,
    _reject_required_tool_disable,
    _require_feature_visible_tool,
    _require_platform_admin,
    _require_tenant_tool_admin,
    _resolve_target_tenant_id,
    _tool_availability,
    _tool_record_visible_to_agent,
    decrypt_sensitive_fields,
    encrypt_sensitive_fields,
    get_sensitive_keys,
    get_tool_company_config,
    mask_sensitive_fields,
    meaningful_config,
    resolved_agent_tool_enabled,
    router,
    set_tenant_tool_config,
    tool_is_required,
)


def _prepare_tools_module(module: ModuleType, symbol_names: tuple[str, ...]) -> None:
    for symbol_name in symbol_names:
        module.__dict__[symbol_name].__module__ = __name__


def _rebuild_tools_models(module: ModuleType, symbol_names: tuple[str, ...]) -> None:
    for symbol_name in symbol_names:
        symbol = module.__dict__[symbol_name]
        model_rebuild = getattr(symbol, "model_rebuild", None)
        if callable(model_rebuild):
            model_rebuild(force=True)


_prepare_tools_module(
    _tools_shared,
    (
        "_is_platform_admin",
        "_require_platform_admin",
        "_require_tenant_tool_admin",
        "_can_view_unmasked_company_config",
        "_tool_availability",
        "_reject_required_tool_disable",
        "_globally_visible_tool_clause",
        "_feature_visible_tool_clause",
        "_require_feature_visible_tool",
        "_load_agent_for_tool_scope",
        "_load_agent_tool_assignments",
        "_agent_visible_tool_clause",
        "_tool_record_visible_to_agent",
        "_resolve_target_tenant_id",
        "_get_sensitive_keys",
        "_encrypt_sensitive_fields",
        "_decrypt_sensitive_fields",
    ),
)
_prepare_tools_module(
    _tools_models,
    (
        "ToolCreate",
        "ToolUpdate",
        "AgentToolUpdate",
        "CategoryConfigUpdate",
        "BulkToolUpdateItem",
        "MCPServerUpdate",
        "MCPTestRequest",
        "AgentToolConfigUpdate",
        "EmailTestRequest",
    ),
)
_rebuild_tools_models(
    _tools_models,
    (
        "ToolCreate",
        "ToolUpdate",
        "AgentToolUpdate",
        "CategoryConfigUpdate",
        "BulkToolUpdateItem",
        "MCPServerUpdate",
        "MCPTestRequest",
        "AgentToolConfigUpdate",
        "EmailTestRequest",
    ),
)

from app.api import (
    tools_platform as _tools_platform,
    tools_agent as _tools_agent,
    tools_integrations as _tools_integrations,
)
from app.api.tools_agent import (
    delete_agent_tool,
    get_agent_tool_config,
    get_agent_tools,
    get_agent_tools_with_config,
    list_agent_installed_tools,
    test_mcp_connection,
    uninstall_agent_mcp_server_group,
    update_agent_tool_config,
    update_agent_tools,
)
from app.api.tools_integrations import (
    delete_category_config,
    get_category_config,
    get_email_providers,
    test_category_config,
    test_email_connection,
    update_category_config,
)
from app.api.tools_platform import (
    create_tool,
    delete_tool,
    list_tools,
    update_mcp_server,
    update_tool,
    update_tools_bulk,
)
_prepare_tools_module(
    _tools_platform,
    (
        "list_tools",
        "create_tool",
        "update_tools_bulk",
        "update_mcp_server",
        "update_tool",
        "delete_tool",
    ),
)
_prepare_tools_module(
    _tools_agent,
    (
        "get_agent_tools",
        "update_agent_tools",
        "test_mcp_connection",
        "list_agent_installed_tools",
        "delete_agent_tool",
        "uninstall_agent_mcp_server_group",
        "get_agent_tool_config",
        "update_agent_tool_config",
        "get_agent_tools_with_config",
    ),
)
_prepare_tools_module(
    _tools_integrations,
    (
        "test_email_connection",
        "get_email_providers",
        "get_category_config",
        "update_category_config",
        "delete_category_config",
        "test_category_config",
    ),
)


_SYNC_MODULES = (
    _tools_shared,
    _tools_models,
    _tools_platform,
    _tools_agent,
    _tools_integrations,
)


def _sync_root_exports() -> None:
    module = sys.modules[__name__]
    for name, value in tuple(module.__dict__.items()):
        if name.startswith("__"):
            continue
        for target in _SYNC_MODULES:
            if name in target.__dict__:
                target.__dict__[name] = value


class _ToolsModule(ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for target in _SYNC_MODULES:
            if name in target.__dict__:
                target.__dict__[name] = value


sys.modules[__name__].__class__ = _ToolsModule
_sync_root_exports()
