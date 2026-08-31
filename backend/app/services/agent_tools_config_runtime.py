"""Tool configuration caching and layered resolution."""

import uuid
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select

def _root_agent_tools():
    from app.services import agent_tools as root_agent_tools

    return root_agent_tools


# ─── Tool Config Cache ──────────────────────────────────────────
# Cache tool configurations to avoid frequent DB queries
# Key: (agent_id, tool_name), Value: (config, expiry_time)
_tool_config_cache: dict[tuple, tuple[dict, datetime]] = {}
_TOOL_CONFIG_CACHE_TTL_SECONDS = 60

# Sensitive field keys that should be encrypted/decrypted
SENSITIVE_FIELD_KEYS = {"api_key", "private_key", "auth_code", "password", "secret", "atlassian_api_key"}


def _decrypt_sensitive_fields(config: dict, config_schema: dict | None = None) -> dict:
    """Decrypt sensitive fields in config dict.

    When config_schema is provided, also decrypts fields with type='password'
    (e.g. smithery_api_key) that are not in the hardcoded SENSITIVE_FIELD_KEYS.
    """
    if not config:
        return config

    from app.core.security import decrypt_data
    from app.config import get_settings

    settings = get_settings()
    result = dict(config)

    # Build the set of sensitive keys: hardcoded + schema-derived
    sensitive_keys = set(SENSITIVE_FIELD_KEYS)
    if config_schema:
        for field in config_schema.get("fields", []):
            if field.get("type") == "password":
                key = field.get("key", "")
                if key:
                    sensitive_keys.add(key)

    for key in sensitive_keys:
        if key in result and result[key]:
            value = result[key]
            if isinstance(value, str) and value:
                try:
                    result[key] = decrypt_data(value, settings.SECRET_KEY)
                except Exception:
                    # If decryption fails, assume it's plaintext
                    pass

    return result


def _get_cached_tool_config(agent_id: Optional[uuid.UUID], tool_name: str) -> Optional[dict]:
    """获取缓存的工具配置，过期返回 None。"""
    cache_key = (str(agent_id) if agent_id else None, tool_name)
    if cache_key in _tool_config_cache:
        config, expiry = _tool_config_cache[cache_key]
        if datetime.now() < expiry:
            return config
        # 过期，删除
        del _tool_config_cache[cache_key]
    return None


def _set_cached_tool_config(agent_id: Optional[uuid.UUID], tool_name: str, config: dict):
    """设置工具配置缓存。"""
    cache_key = (str(agent_id) if agent_id else None, tool_name)
    expiry = datetime.now() + timedelta(seconds=_TOOL_CONFIG_CACHE_TTL_SECONDS)
    _tool_config_cache[cache_key] = (config, expiry)


def invalidate_tool_config_cache(agent_id: Optional[uuid.UUID], tool_name: str | None = None) -> None:
    """Invalidate cached config after an Agent or company-level update.

    ``agent_id=None`` clears matching entries for every Agent because a company
    configuration change can affect all of them.
    """
    agent_key = str(agent_id) if agent_id else None
    for cache_key in list(_tool_config_cache):
        agent_matches = agent_id is None or cache_key[0] == agent_key
        if agent_matches and (tool_name is None or cache_key[1] == tool_name):
            _tool_config_cache.pop(cache_key, None)


async def _get_tool_config(agent_id: Optional[uuid.UUID], tool_name: str) -> Optional[dict]:
    """Get merged tool config (with caching).

    Priority:
    1. agent_tools.config (per-agent override)
    2. tenant_settings tool_config:<tool_name> for builtin company config
    3. tools.config (tenant-specific/admin tool config or non-secret defaults)

    Both configs are decrypted using the tool's config_schema for
    schema-aware field detection (e.g. smithery_api_key with type=password).
    """
    # Check cache first
    cached = _get_cached_tool_config(agent_id, tool_name)
    if cached is not None:
        logger.debug(f"[ToolConfig] Cache hit for {tool_name}, agent_id={agent_id}: {cached}")
        return cached

    from app.models.tool import Tool, AgentTool
    from app.models.agent import Agent as AgentModel
    from app.services.tool_config import (
        get_tenant_tool_config,
        merge_tool_config_layers,
    )

    root_agent_tools = _root_agent_tools()

    async with root_agent_tools.async_session() as db:
        agent_tenant_id = None
        if agent_id:
            tenant_r = await db.execute(select(AgentModel.tenant_id).where(AgentModel.id == agent_id))
            agent_tenant_id = tenant_r.scalar_one_or_none()

        # 1. Try per-agent + global config together
        if agent_id:
            result = await db.execute(
                select(AgentTool.config, Tool.config, Tool.config_schema, Tool.source, Tool.name)
                .join(Tool, AgentTool.tool_id == Tool.id)
                .where(AgentTool.agent_id == agent_id, Tool.name == tool_name)
            )
            row = result.first()
            if row:
                agent_config, global_config, config_schema, tool_source, db_tool_name = row
                base_config = global_config or {}
                tenant_config = {}
                if tool_source == "builtin":
                    tenant_config = await get_tenant_tool_config(db, agent_tenant_id, db_tool_name, config_schema)
                # Merge: agent overrides company/global. Fields declared
                # agent_only are never inherited from broader layers.
                merged = merge_tool_config_layers(
                    base_config,
                    tenant_config,
                    agent_config,
                    config_schema,
                )
                if merged:
                    # Decrypt with schema awareness
                    merged = _decrypt_sensitive_fields(merged, config_schema)
                    logger.info(f"[ToolConfig] DB merged config for {tool_name}, agent_id={agent_id}")
                    _set_cached_tool_config(agent_id, tool_name, merged)
                    return merged

        # 2. Fallback to global config only
        result = await db.execute(select(Tool).where(Tool.name == tool_name))
        tool = result.scalar_one_or_none()
        if tool:
            tenant_config = {}
            if tool.source == "builtin":
                tenant_config = await get_tenant_tool_config(db, agent_tenant_id, tool.name, tool.config_schema)
            base_config = tool.config or {}
            merged = merge_tool_config_layers(
                base_config,
                tenant_config,
                None,
                tool.config_schema,
            )
        else:
            merged = {}
        if tool and merged:
            # Decrypt with schema awareness
            decrypted = _decrypt_sensitive_fields(merged, tool.config_schema)
            logger.info(f"[ToolConfig] DB global config for {tool_name}")
            _set_cached_tool_config(agent_id, tool_name, decrypted)
            return decrypted

    logger.error(f"[ToolConfig] No DB config found for {tool_name}, agent_id={agent_id}")
    return None

__all__ = [name for name in globals() if not name.startswith("__")]
