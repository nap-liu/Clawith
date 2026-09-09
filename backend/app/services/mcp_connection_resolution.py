"""Resolve the same installation credentials for discovery and execution."""

from urllib.parse import quote

from sqlalchemy import select

from app.database import async_session
from app.models.tool import Tool
from app.services.llm.failure_outcome import render_message
from app.services.mcp_catalog_policy import (
    SHARED_TOOL_CONFIG_FIELDS, apply_shared_tool_config, shared_catalog,
)
from app.services.mcp_server_service import (
    build_placeholder_context_for_call, compose_runtime_config,
    lookup_overrides, lookup_project_source_tool_config,
)
from app.services.placeholder_engine import ALL_ROOTS, render, render_dict
from app.services.tool_config import decrypt_sensitive_fields


async def resolve_assignment_config(db, tool, assignment, agent, user_id):
    source = await lookup_project_source_tool_config(
        db, project_agent_id=agent.id, tool_id=tool.id, execution_user_id=user_id,
    ) if agent.scope == "project" else {}
    return decrypt_sensitive_fields(
        {**source, **dict(assignment.config or {})}, tool.config_schema,
    )


async def resolve_agent_connection(db, server, agent, tool, assignment, user_id, session_id=""):
    values = await resolve_assignment_config(db, tool, assignment, agent, user_id) if assignment else {}
    overrides = await lookup_overrides(
        db, server.id, agent.tenant_id, agent.id, execution_user_id=user_id,
        allow_project_source_reference=agent.scope == "project",
    )
    config = compose_runtime_config(server, *overrides)
    if await shared_catalog(db, server):
        values = {key: value for key, value in values.items() if key in SHARED_TOOL_CONFIG_FIELDS}
        config = apply_shared_tool_config(config, values)
        # Shared routing is canonical definition, never an Agent override.
        values.update({key: value for key, value in ((tool.config or {}) if tool else {}).items()
                       if key in {"smithery_namespace", "smithery_connection_id"}})
    context = await build_placeholder_context_for_call(db, agent.id, user_id, session_id=session_id)
    return config, values, context


def render_http_connection(config, context):
    url = render(config.url_template or "", context, ALL_ROOTS, on_unknown="raise")
    headers = render_dict(config.headers_template or {}, context, ALL_ROOTS, on_unknown="raise")
    headers = {key: value if value.isascii() else quote(value, safe="") for key, value in headers.items()}
    credential = render(config.credential_template, context, ALL_ROOTS, on_unknown="raise") \
        if config.credential_template else None
    return url, headers, credential


async def resolve_smithery_connection(config, agent_id, *, allow_legacy_defaults=False):
    """Known installations must never borrow another installation's route/key."""
    api_key = config.get("smithery_api_key")
    namespace = config.get("smithery_namespace")
    connection_id = config.get("smithery_connection_id")
    if allow_legacy_defaults:
        from app.services.resource_discovery import _get_smithery_api_key

        api_key = api_key or await _get_smithery_api_key(agent_id)
        if not namespace or not connection_id:
            async with async_session() as db:
                tool = await db.scalar(select(Tool).where(Tool.name == "discover_resources"))
                defaults = (tool.config or {}) if tool else {}
                namespace = namespace or defaults.get("smithery_namespace")
                connection_id = connection_id or defaults.get("smithery_connection_id")
    if not api_key or not namespace or not connection_id:
        raise ValueError(render_message("mcpAccess.connectionNotConfigured"))
    endpoint = f"https://api.smithery.ai/connect/{quote(str(namespace), safe='')}/{quote(str(connection_id), safe='')}/mcp"
    return endpoint, api_key, namespace, connection_id
