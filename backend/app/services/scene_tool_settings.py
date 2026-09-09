"""Scene tool-panel persistence using the ordinary tool configuration contracts."""

from copy import deepcopy

from fastapi import HTTPException
from sqlalchemy import select

from app.config import get_settings
from app.core.security import decrypt_data, encrypt_data
from app.models.agent import Agent
from app.models.mcp_server import MCPServer, MCPServerOverride
from app.services.mcp_catalog_policy import validate_agent_override, validate_shared_tool_config
from app.models.tool import AgentTool, Tool
from app.schemas.scene import SceneSaveRequest, SceneToolSaveRequest
from app.services.tool_config import get_sensitive_keys
from app.services.tool_enablement import tool_is_required, tool_visibility_clause

_SECRET_PREFIX = "scene-secret:"


def project_settings(value, *, runtime=False):
    """Never expose persisted secrets in management responses or prompts."""
    if isinstance(value, dict):
        return {key: project_settings(item, runtime=runtime) for key, item in value.items()}
    if isinstance(value, list):
        return [project_settings(item, runtime=runtime) for item in value]
    if isinstance(value, str) and value.startswith(_SECRET_PREFIX):
        return decrypt_data(value[len(_SECRET_PREFIX):], get_settings().SECRET_KEY) if runtime else "********"
    return value


def _protect(value, previous=None, *, sensitive=False):
    if isinstance(value, dict):
        previous = previous if isinstance(previous, dict) else {}
        return {key: _protect(item, previous.get(key), sensitive=sensitive or key.lower() in {
            *get_sensitive_keys(), "token", "authorization", "headers", "headers_template", "env_template",
        }) for key, item in value.items()}
    if isinstance(value, list):
        return [_protect(item, sensitive=sensitive) for item in value]
    if not isinstance(value, str) or not value:
        return value
    if value.startswith("****"):
        return _protect(previous, previous, sensitive=sensitive) if previous and previous != value else None
    if value.startswith(_SECRET_PREFIX):
        if value != previous:
            raise HTTPException(422, detail="sceneRuntime.invalidSecret")
        return value
    if sensitive and "{{" not in value and "${" not in value:
        return _SECRET_PREFIX + encrypt_data(value, get_settings().SECRET_KEY)
    return value


async def prepare_scene_settings(db, agent_id, payload, previous=None):
    """Validate catalog ownership and keep configuration changes scene-local."""
    previous = previous or {}
    if payload.get("tools") is None:
        payload["mcp_server_overrides"] = []
        return
    agent = await db.get(Agent, agent_id)
    from app.services.read_media_compat import project_legacy_image_settings

    payload["tools"] = await project_legacy_image_settings(db, agent.tenant_id, payload["tools"])
    assignments = {
        str(row.tool_id): row for row in
        (await db.scalars(select(AgentTool).where(AgentTool.agent_id == agent_id))).all()
    }
    tools = (await db.scalars(select(Tool).where(
        tool_visibility_clause(agent.tenant_id, [row.tool_id for row in assignments.values()]),
    ))).all()
    catalog = {str(tool.id): tool for tool in tools}
    prior_tools = {str(item["tool_id"]): item for item in previous.get("tools") or []}
    server_ids = set()
    for item in payload["tools"]:
        tool_id = str(item["tool_id"])
        tool = catalog.get(tool_id)
        if tool is None or (item["enabled"] and not tool.enabled and not tool_is_required(tool.name)):
            raise HTTPException(422, detail="sceneRuntime.toolUnavailable")
        await validate_shared_tool_config(db, tool, item.get("config", {}))
        item["enabled"] = tool_is_required(tool.name) or item["enabled"]
        assignment = assignments.get(tool_id)
        prior = prior_tools.get(tool_id, {}).get("config", assignment.config if assignment else {}) or {}
        sensitive = get_sensitive_keys(tool.config_schema)
        item["config"] = {
            key: _protect(value, prior.get(key), sensitive=key in sensitive)
            for key, value in item.get("config", {}).items()
        }
        if tool.mcp_server_id:
            server_ids.add(str(tool.mcp_server_id))
    prior_overrides = {str(item["server_id"]): item for item in previous.get("mcp_server_overrides") or []}
    for item in payload.get("mcp_server_overrides", []):
        server_id = str(item["server_id"])
        if server_id not in server_ids:
            raise HTTPException(422, detail="sceneRuntime.serverUnavailable")
        server = await db.get(MCPServer, item["server_id"])
        await validate_agent_override(db, server, {key: value for key, value in item.items() if key != "server_id"})
        original = await db.scalar(select(MCPServerOverride).where(
            MCPServerOverride.mcp_server_id == server_id,
            MCPServerOverride.scope_type == "agent", MCPServerOverride.scope_id == agent_id,
        ))
        prior = prior_overrides.get(server_id) or {
            key: getattr(original, key, None) for key in item if key != "server_id"
        }
        for key in ("credential_template", "headers_template", "env_template"):
            if key == "credential_template" and item.get(key) is None:
                item[key] = prior.get(key)
            item[key] = _protect(item.get(key), prior.get(key), sensitive=True)


async def merge_scene_settings(db, agent_id, data, payload, previous):
    """Older clients must not reset fields they cannot yet edit."""
    for key, default in (("auto_activation", {}), ("include_soul", True), ("include_memory", True), ("tools", None), ("mcp_server_overrides", [])):
        if key not in data.model_fields_set:
            payload[key] = deepcopy(previous.get(key, default))
    await prepare_scene_settings(db, agent_id, payload, previous)


async def scene_mcp_override_options(db, agent_id, tools):
    server_ids = {tool["mcp_server_id"] for tool in tools if tool.get("mcp_server_id")}
    rows = (await db.scalars(select(MCPServerOverride).where(
        MCPServerOverride.mcp_server_id.in_(server_ids),
        MCPServerOverride.scope_type == "agent", MCPServerOverride.scope_id == agent_id,
    ))).all()
    fields = ("system_prompt_block", "url_template", "headers_template", "credential_template",
              "command_template", "args_template", "env_template")
    return [project_settings({
        "server_id": str(row.mcp_server_id),
        **{key: _protect(getattr(row, key), sensitive=key in {
            "credential_template", "headers_template", "env_template",
        }) for key in fields},
    }) for row in rows]


def _merge_tool_save_request(
    current: dict | None,
    patch: SceneToolSaveRequest,
) -> SceneSaveRequest:
    """Build a full draft while preserving omitted tool fields by default."""
    current = current or {}
    provided = patch.model_fields_set
    force_overwrite = patch.force_overwrite

    name = patch.name if "name" in provided else current.get("name")
    if not name:
        raise ValueError("name is required when creating a scene")

    if force_overwrite:
        values = {
            "name": name,
            "enabled": patch.enabled if "enabled" in provided else True,
            "expected_revision": patch.expected_revision,
            "welcome_message": patch.welcome_message if "welcome_message" in provided else "",
            "system_prompts": patch.system_prompts if "system_prompts" in provided else [],
            "quick_actions": patch.quick_actions if "quick_actions" in provided else [],
        }
    else:
        values = {
            "name": name,
            "enabled": patch.enabled if "enabled" in provided else current.get("enabled", True),
            "expected_revision": patch.expected_revision,
            "welcome_message": (
                patch.welcome_message
                if "welcome_message" in provided
                else current.get("welcome_message", "")
            ),
            "system_prompts": (
                patch.system_prompts
                if "system_prompts" in provided
                else current.get("system_prompts", [])
            ),
            "quick_actions": (
                patch.quick_actions
                if "quick_actions" in provided
                else current.get("quick_actions", [])
            ),
        }
    for key, default in (("auto_activation", {}), ("include_soul", True), ("include_memory", True), ("tools", None), ("mcp_server_overrides", [])):
        values[key] = getattr(patch, key) if key in provided else current.get(key, default)
    return SceneSaveRequest.model_validate(values)
