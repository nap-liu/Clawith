"""Task-local tool-panel snapshot shared by context, schemas and execution."""

from contextvars import ContextVar
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps
from inspect import signature
from types import SimpleNamespace

from sqlalchemy import or_, select

from app.database import async_session
from app.models.agent import Agent
from app.models.mcp_server import MCPServerOverride
from app.models.tool import AgentTool, Tool
from app.services.scene_tool_settings import project_settings
from app.services.tool_config import decrypt_sensitive_fields, get_tool_company_config, merge_tool_config_layers
from app.services.tool_enablement import REQUIRED_AGENT_TOOL_NAMES, tool_is_required, tool_visibility_clause
from app.services.scene_service import load_turn_scene_context


@dataclass
class TurnToolSettings:
    agent_id: str
    assignments: list[dict]
    configs: dict[str, dict]
    mcp_overrides: dict[str, dict]
    enabled_names: frozenset[str]


_current: ContextVar[TurnToolSettings | None] = ContextVar("turn_tool_settings", default=None)
_MCP_FIELDS = ("system_prompt_block", "url_template", "headers_template", "credential_template",
               "command_template", "args_template", "env_template")


def current_tool_settings(agent_id):
    scope = _current.get()
    return scope if scope and scope.agent_id == str(agent_id) else None


def effective_assignment(agent_id, tool, assignment):
    scope = current_tool_settings(agent_id)
    if scope is None:
        return assignment if assignment and assignment.enabled else None
    if tool.source == "agent" and assignment is None:
        return None
    item = next((item for item in scope.assignments if str(item["tool_id"]) == str(tool.id)), None)
    return SimpleNamespace(enabled=True, config=item.get("config", {})) if item else None


def effective_mcp_override(agent_id, server_id, original):
    scope = current_tool_settings(agent_id)
    value = scope.mcp_overrides.get(str(server_id)) if scope else None
    if value is None:
        return original
    return SimpleNamespace(**{key: value.get(key) for key in _MCP_FIELDS})


async def _load_settings(agent_id, channel_context):
    settings = project_settings(channel_context["scene_tools"], runtime=True)
    configs = {}
    assignments = []
    enabled_names = set()
    mcp_overrides = {}
    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        if agent is None:
            raise ValueError("Scene agent is unavailable")
        from app.services.read_media_compat import project_legacy_image_settings

        settings = await project_legacy_image_settings(db, agent.tenant_id, settings)
        tools = (await db.scalars(select(Tool).where(
            or_(Tool.id.in_([item["tool_id"] for item in settings]), Tool.name.in_(REQUIRED_AGENT_TOOL_NAMES)),
            tool_visibility_clause(agent.tenant_id, select(AgentTool.tool_id).where(AgentTool.agent_id == agent_id)),
        ))).all()
        catalog = {str(tool.id): tool for tool in tools}
        configured_ids = {str(item["tool_id"]) for item in settings}
        for tool in tools:
            if tool_is_required(tool.name) and str(tool.id) not in configured_ids:
                assignment = await db.scalar(select(AgentTool).where(
                    AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id,
                ))
                settings.append({"tool_id": str(tool.id), "enabled": True,
                                 "config": dict(assignment.config or {}) if assignment else {}})
        for item in settings:
            tool = catalog.get(str(item["tool_id"]))
            if tool is None or (not tool_is_required(tool.name) and not tool.enabled):
                continue
            company = await get_tool_company_config(db, tool, agent.tenant_id)
            item["config"] = decrypt_sensitive_fields(item.get("config", {}), tool.config_schema)
            configs[tool.name] = merge_tool_config_layers(
                decrypt_sensitive_fields(tool.config or {}, tool.config_schema),
                company, item.get("config"), tool.config_schema,
            )
            if item["enabled"] or tool_is_required(tool.name):
                assignments.append(item)
                enabled_names.add(tool.name)
        await db.commit()
        server_ids = {tool.mcp_server_id for tool in tools if tool.mcp_server_id}
        overrides = (await db.scalars(select(MCPServerOverride).where(
            MCPServerOverride.mcp_server_id.in_(server_ids),
            MCPServerOverride.scope_type == "agent", MCPServerOverride.scope_id == agent_id,
        ))).all()
        originals = {str(item.mcp_server_id): item for item in overrides}
        for server_id in server_ids:
            original = originals.get(str(server_id))
            mcp_overrides[str(server_id)] = {key: getattr(original, key, None) for key in _MCP_FIELDS}
    for server_id, override in {
        str(item["server_id"]): item for item in project_settings(
            channel_context.get("scene_mcp_server_overrides", []), runtime=True,
        )
    }.items():
        if server_id in mcp_overrides:
            mcp_overrides[server_id].update({key: value for key, value in override.items() if key in _MCP_FIELDS and value is not None})
    return TurnToolSettings(str(agent_id), assignments, configs, mcp_overrides, frozenset(enabled_names))


@asynccontextmanager
async def scene_tool_settings_scope(agent_id, channel_context):
    context = channel_context or {}
    scope = await _load_settings(agent_id, context) if agent_id and context.get("scene_tools") is not None else None
    token = _current.set(scope)
    try:
        yield scope
    finally:
        _current.reset(token)


@asynccontextmanager
async def restore_turn_tool_settings(agent_id, session_id, turn_anchor_id):
    async with async_session() as db:
        context = await load_turn_scene_context(
            db, agent_id=agent_id, session_id=session_id, turn_anchor_id=turn_anchor_id,
        )
    async with scene_tool_settings_scope(agent_id, context) as scope:
        yield scope


def with_scene_tool_settings(func):
    """Bind once and always reset, including cancellation and nested calls."""
    contract = signature(func)

    @wraps(func)
    async def wrapped(*args, **kwargs):
        values = contract.bind(*args, **kwargs).arguments
        context = values.get("channel_context") or {}
        agent_id = values.get("agent_id")
        async with scene_tool_settings_scope(agent_id, context):
            return await func(*args, **kwargs)

    return wrapped
