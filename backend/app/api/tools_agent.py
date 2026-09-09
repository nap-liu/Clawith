"""Agent-scoped tool routes."""

import json
import uuid

from fastapi import Depends, HTTPException
from loguru import logger
from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.tools_models import AgentToolConfigUpdate, AgentToolUpdate, MCPTestRequest
from app.api.tools_shared import (
    SUBAGENT_TOOL_NAMES,
    USER_PROJECT_TOOL_NAMES,
    _agent_visible_tool_clause,
    _decrypt_sensitive_fields,
    _encrypt_sensitive_fields,
    _feature_visible_tool_clause,
    _globally_visible_tool_clause,
    _load_agent_tool_assignments,
    _reject_required_tool_disable,
    _tool_availability,
    _tool_record_visible_to_agent,
    get_tool_company_config,
    mask_sensitive_fields,
    resolved_agent_tool_enabled,
    router,
    tool_is_required,
)
from app.core.okr_feature import is_retired_okr_tool
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.mcp_naming import load_mcp_display_names


@router.get("/agents/{agent_id}")
async def get_agent_tools(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get tools for a specific agent with their enabled status."""
    from app.core.permissions import check_agent_access
    from app.services.agent_tools import _agent_has_feishu

    agent_obj, _access_level = await check_agent_access(
        db, current_user, agent_id
    )
    has_feishu = await _agent_has_feishu(agent_id)

    # Determine if this is a system agent (e.g. OKR Agent).
    # System agents can see all tools; regular agents cannot see okr_agent_only tools.
    is_system_agent = bool(agent_obj and agent_obj.is_system)

    # Agent-specific assignments
    assignments = await _load_agent_tool_assignments(db, agent_id)

    # All tools visible within this agent's tenant boundary
    all_tools_r = await db.execute(
        select(Tool)
        .where(_globally_visible_tool_clause(), _agent_visible_tool_clause(agent_obj.tenant_id, assignments))
        .order_by(Tool.category, Tool.name)
    )
    all_tools = all_tools_r.scalars().all()
    mcp_display_names = await load_mcp_display_names(db, all_tools)

    # ── Backfill: create missing AgentTool records ──────────────────────
    # For agents that already have at least one AgentTool assignment (i.e.
    # the tool panel has been configured), create AgentTool records for any
    # visible tool that doesn't have one yet.  The initial `enabled` value
    # is taken from `is_default`.
    #
    # This keeps the UI state and `get_agent_tools_for_llm` in sync: both
    # now rely on explicit AgentTool records instead of the implicit
    # `is_default` fallback.
    if assignments:
        backfilled = 0
        for t in all_tools:
            tid = str(t.id)
            if tid not in assignments:
                new_at = AgentTool(
                    agent_id=agent_id,
                    tool_id=t.id,
                    enabled=True if tool_is_required(t.name) else t.is_default,
                )
                db.add(new_at)
                assignments[tid] = new_at
                backfilled += 1
        if backfilled:
            await db.commit()
            logger.info(
                f"[Tools] Backfilled {backfilled} AgentTool records for "
                f"agent={agent_id}"
            )

    result = []
    for t in all_tools:
        # Hide feishu tools for agents without Feishu channel
        if t.category == "feishu" and not has_feishu:
            continue
        # Hide OKR Agent-exclusive tools from regular agents.
        # These tools (create_objective, collect_okr_progress, etc.) should only
        # appear in the tool panel of system agents such as the OKR Agent.
        if (t.config or {}).get("okr_agent_only") and not is_system_agent:
            continue
        tid = str(t.id)
        at = assignments.get(tid)
        if not _tool_record_visible_to_agent(t, agent_obj.tenant_id, assignments):
            continue
        # No explicit AgentTool row → not enabled (no is_default fallback)
        enabled = resolved_agent_tool_enabled(t.name, at)
        result.append({
            "id": tid,
            "name": t.name,
            "display_name": t.display_name,
            "description": t.description,
            "type": t.type,
            "category": t.category,
            "icon": t.icon,
            "enabled": enabled,
            "is_default": t.is_default,
            "mcp_server_name": t.mcp_server_name,
            "mcp_server_display_name": mcp_display_names.get(t.mcp_server_id),
            "mcp_server_url": t.mcp_server_url,
            "mcp_server_id": str(t.mcp_server_id) if t.mcp_server_id else None,
            "source": t.source,
            **_tool_availability(t.name),
        })
    return result


@router.put("/agents/{agent_id}")
async def update_agent_tools(
    agent_id: uuid.UUID,
    updates: list[AgentToolUpdate],
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update tool assignments for an agent."""
    from app.core.permissions import check_agent_access

    agent_obj, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="需要数字员工管理权限")
    assignments = await _load_agent_tool_assignments(db, agent_id)
    resolved_updates: list[tuple[AgentToolUpdate, Tool]] = []
    for u in updates:
        tool_id = uuid.UUID(u.tool_id)
        tool_r = await db.execute(
            select(Tool).where(
                Tool.id == tool_id,
                _feature_visible_tool_clause(),
                _agent_visible_tool_clause(agent_obj.tenant_id, assignments),
            )
        )
        tool_obj = tool_r.scalar_one_or_none()
        if not tool_obj:
            raise HTTPException(status_code=404, detail="Tool not found")

        _reject_required_tool_disable(tool_obj, u.enabled)
        resolved_updates.append((u, tool_obj))

    # Subagent is one panel capability backed by four protocol functions.
    # Any update to one member atomically applies the same state to all four.
    subagent_states = {
        update.enabled
        for update, tool in resolved_updates
        if tool.name in SUBAGENT_TOOL_NAMES
    }
    if len(subagent_states) > 1:
        raise HTTPException(
            status_code=409,
            detail="Subagent tools must be enabled or disabled as one group",
        )
    if subagent_states:
        subagent_enabled = next(iter(subagent_states))
        group_tools = (
            await db.execute(
                select(Tool).where(
                    Tool.name.in_(SUBAGENT_TOOL_NAMES),
                    _agent_visible_tool_clause(agent_obj.tenant_id, assignments),
                )
            )
        ).scalars().all()
        if {tool.name for tool in group_tools} != set(SUBAGENT_TOOL_NAMES):
            raise HTTPException(status_code=409, detail="Subagent tool group is incomplete")
        resolved_updates = [
            (update, tool)
            for update, tool in resolved_updates
            if tool.name not in SUBAGENT_TOOL_NAMES
        ] + [
            (
                AgentToolUpdate(tool_id=str(tool.id), enabled=subagent_enabled),
                tool,
            )
            for tool in group_tools
        ]

    # Project management is one opt-in capability group. Keep every function
    # aligned so a digital employee never receives a partial management set.
    project_management_states = {
        update.enabled
        for update, tool in resolved_updates
        if tool.category == "project_management"
    }
    if len(project_management_states) > 1:
        raise HTTPException(
            status_code=409,
            detail="Project management tools must be enabled or disabled as one group",
        )
    if project_management_states:
        project_management_enabled = next(iter(project_management_states))
        group_tools = (
            await db.execute(
                select(Tool).where(
                    Tool.name.in_(USER_PROJECT_TOOL_NAMES),
                    _agent_visible_tool_clause(agent_obj.tenant_id, assignments),
                )
            )
        ).scalars().all()
        if {tool.name for tool in group_tools} != set(USER_PROJECT_TOOL_NAMES):
            raise HTTPException(status_code=409, detail="Project management tool group is incomplete")
        resolved_updates = [
            (update, tool)
            for update, tool in resolved_updates
            if tool.category != "project_management"
        ] + [
            (
                AgentToolUpdate(tool_id=str(tool.id), enabled=project_management_enabled),
                tool,
            )
            for tool in group_tools
        ]

    # Apply only after every requested tool has passed visibility and required
    # capability validation.
    for u, tool_obj in resolved_updates:
        tool_id = tool_obj.id

        # System-category tools (e.g. request_confirmation) are protocol-level
        # and must always remain enabled — reject any attempt to disable them.
        if tool_obj.category == "system" and not u.enabled:
            continue

        # Upsert
        result = await db.execute(
            select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id)
        )
        at = result.scalar_one_or_none()
        if at:
            at.enabled = u.enabled
        else:
            db.add(AgentTool(agent_id=agent_id, tool_id=tool_id, enabled=u.enabled))
    if agent_obj.scope == "project" and agent_obj.project_id is not None:
        mcp_server_ids = {
            tool.mcp_server_id
            for _update, tool in resolved_updates
            if tool.type == "mcp" and tool.mcp_server_id is not None
        }
        if mcp_server_ids:
            from app.models.project import Project
            from app.services.project_member_runtime import sync_project_agent_mcp_bindings

            project = await db.get(Project, agent_obj.project_id)
            if project is not None and project.tenant_id == agent_obj.tenant_id:
                await db.flush()
                await sync_project_agent_mcp_bindings(
                    db,
                    project,
                    project_agent_id=agent_obj.id,
                    server_ids=mcp_server_ids,
                )
    await db.commit()
    return {"ok": True}


# ─── MCP Server Testing ────────────────────────────────────
@router.post("/test-mcp")
async def test_mcp_connection(
    data: MCPTestRequest,
    current_user: User = Depends(get_current_user),
):
    """Test connection to an MCP server and list available tools.

    Supports two authentication modes:
    - URL-embedded key (e.g. ?tavilyApiKey=xxx) — include in server_url.
    - Bearer token — pass via api_key field; sent as Authorization header.
    """
    from app.services.mcp_client import MCPClient

    try:
        client = MCPClient(data.server_url, api_key=data.api_key or None)
        tools = await client.list_tools()
        return {"ok": True, "tools": tools}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ─── Agent-installed Tools Management (admin) ───────────────
@router.get("/agent-installed")
async def list_agent_installed_tools(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin endpoint: list user-installed tools scoped by tenant."""
    from app.models.agent import Agent
    query = (
        select(AgentTool, Tool, Agent)
        .join(Tool, cast(AgentTool.tool_id, String) == cast(Tool.id, String))
        .outerjoin(Agent, cast(AgentTool.installed_by_agent_id, String) == cast(Agent.id, String))
        .where(or_(AgentTool.source == "user_installed", Tool.source == "agent"))
        .order_by(AgentTool.created_at.desc())
    )
    # Scope by tenant: only show tools installed by agents in this tenant
    tid = tenant_id or (str(current_user.tenant_id) if current_user.tenant_id else None)
    if tid:
        from app.models.agent import Agent as Ag
        # Some local/prod databases still have agents.tenant_id as varchar from
        # older migrations, while newer models bind tenant_id as UUID. Cast the
        # column to text so this admin listing works across both schemas.
        tenant_agent_ids = select(cast(Ag.id, String)).where(cast(Ag.tenant_id, String) == str(tid))
        query = query.where(cast(AgentTool.agent_id, String).in_(tenant_agent_ids))
    result = await db.execute(query)
    rows = result.all()
    return [
        {
            "agent_tool_id": str(at.id),
            "agent_id": str(at.agent_id),
            "tool_id": str(t.id),
            "tool_name": t.name,
            "tool_display_name": t.display_name,
            "description": t.description,
            "type": t.type,
            "category": t.category,
            "source": t.source,
            "mcp_server_name": t.mcp_server_name,
            "mcp_server_url": t.mcp_server_url,
            "mcp_server_id": str(t.mcp_server_id) if t.mcp_server_id else None,
            "mcp_tool_name": t.mcp_tool_name,
            "installed_by_agent_id": str(at.installed_by_agent_id) if at.installed_by_agent_id else None,
            "installed_by_agent_name": a.name if a else None,
            "enabled": at.enabled,
            "configured": bool(at.config and len(at.config) > 0),
            "installed_at": at.created_at.isoformat() if at.created_at else None,
        }
        for at, t, a in rows
    ]


@router.delete("/agent-tool/{agent_tool_id}")
async def delete_agent_tool(
    agent_tool_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin: remove an agent-tool assignment. Also deletes the tool record if no other agents use it."""
    at_r = await db.execute(select(AgentTool).where(AgentTool.id == agent_tool_id))
    at = at_r.scalar_one_or_none()
    if not at:
        raise HTTPException(status_code=404, detail="Agent tool assignment not found")
    from app.services.mcp_catalog_locks import lock_tool_catalog

    await db.execute(select(Agent.id).where(Agent.id == at.agent_id).with_for_update())
    at = await db.scalar(select(AgentTool).where(
        AgentTool.id == agent_tool_id,
    ).execution_options(populate_existing=True))
    if at is None:
        raise HTTPException(status_code=404, detail="Agent tool assignment not found")
    await lock_tool_catalog(db, at.tool_id)
    tool_id = at.tool_id
    await db.delete(at)
    await db.flush()
    # If no other agent uses this tool, delete the tool record too (for MCP tools)
    remaining_r = await db.execute(select(AgentTool).where(AgentTool.tool_id == tool_id).limit(1))
    if not remaining_r.scalar_one_or_none():
        tool_r = await db.execute(select(Tool).where(Tool.id == tool_id))
        tool = tool_r.scalar_one_or_none()
        if tool and tool.type == "mcp":
            await db.delete(tool)
    await db.commit()
    return {"ok": True}


@router.delete("/agents/{agent_id}/mcp-servers/{server_id}")
async def uninstall_agent_mcp_server_group(
    agent_id: uuid.UUID,
    server_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove one complete self-installed MCP group from an Agent."""
    from app.core.permissions import check_agent_access
    from app.services.agent_mcp_lifecycle import uninstall_mcp_server

    _agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="Manage access required")
    # Release the request-scoped read transaction before the lifecycle service
    # opens its serialized write transaction for this Agent.
    await db.rollback()

    result = json.loads(await uninstall_mcp_server(agent_id, server_id))
    if not result.get("ok"):
        status_code = 404 if result.get("error") == "mcp_server_not_found" else 409
        raise HTTPException(status_code=status_code, detail=result)
    return result


# ─── Per-Agent Tool Config ───────────────────────────────────
@router.get("/agents/{agent_id}/tool-config/{tool_id}")
async def get_agent_tool_config(
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get merged tool config (global defaults + agent overrides) and config_schema.

    Both configs are decrypted before returning. Global sensitive fields are
    masked so the frontend can show a key is configured without exposing it.
    """
    from app.core.permissions import check_agent_access

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    assignments = await _load_agent_tool_assignments(db, agent_id)
    tool_r = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool = tool_r.scalar_one_or_none()
    if tool and is_retired_okr_tool(tool.name):
        tool = None
    if not tool or not _tool_record_visible_to_agent(
        tool, agent.tenant_id, assignments
    ):
        raise HTTPException(status_code=404, detail="Tool not found")
    at_r = await db.execute(
        select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id)
    )
    at = at_r.scalar_one_or_none()

    # Decrypt both configs using the tool's config_schema for field type awareness
    schema = tool.config_schema
    raw_global = await get_tool_company_config(db, tool, agent.tenant_id)
    raw_agent = _decrypt_sensitive_fields(at.config if at else {}, schema)

    # Mask sensitive fields in global config for display
    masked_global = mask_sensitive_fields(raw_global, schema)
    visible_agent_config = (
        raw_agent
        if access_level == "manage"
        else mask_sensitive_fields(raw_agent, schema)
    )

    # The API is a UI surface, not the runtime resolver: never return an
    # unmasked company secret through merged_config.
    merged = {**masked_global, **(visible_agent_config or {})}
    return {
        "global_config": masked_global,
        "agent_config": visible_agent_config or {},
        "merged_config": merged,
        "config_schema": tool.config_schema or {},
    }


@router.put("/agents/{agent_id}/tool-config/{tool_id}")
async def update_agent_tool_config(
    agent_id: uuid.UUID,
    tool_id: uuid.UUID,
    data: AgentToolConfigUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save per-agent config override for a tool."""
    from app.core.permissions import check_agent_access

    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=403, detail="需要数字员工管理权限")

    # Check permission: only platform_admin and org_admin can modify allow_network
    if "allow_network" in data.config:
        if current_user.role not in ("platform_admin", "org_admin"):
            raise HTTPException(
                status_code=403,
                detail="Only platform admin or organization admin can modify network access settings"
            )

    # Encrypt sensitive fields using the tool's config_schema for field type awareness
    tool_r2 = await db.execute(select(Tool).where(Tool.id == tool_id))
    tool_for_schema = tool_r2.scalar_one_or_none()
    if tool_for_schema and is_retired_okr_tool(tool_for_schema.name):
        tool_for_schema = None
    assignments = await _load_agent_tool_assignments(db, agent_id)
    if not tool_for_schema or not _tool_record_visible_to_agent(
        tool_for_schema, agent.tenant_id, assignments
    ):
        raise HTTPException(status_code=404, detail="Tool not found")
    encrypted_config = _encrypt_sensitive_fields(data.config, tool_for_schema.config_schema if tool_for_schema else None)

    at_r = await db.execute(
        select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool_id)
    )
    at = at_r.scalar_one_or_none()
    if at:
        at.config = encrypted_config
    else:
        # Create assignment if not exists
        db.add(AgentTool(agent_id=agent_id, tool_id=tool_id, enabled=True, config=encrypted_config))
    await db.commit()
    if tool_for_schema:
        from app.services.agent_tools import invalidate_tool_config_cache

        invalidate_tool_config_cache(agent_id, tool_for_schema.name)
    return {"ok": True}


@router.get("/agents/{agent_id}/with-config")
async def get_agent_tools_with_config(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get agent's enabled tools with per-agent config info and config_schema for settings UI.

    Both global_config and agent_config are decrypted before returning.
    For global_config, sensitive fields are masked (e.g. "sk-****abcd") so the
    frontend can show that a company key is configured without exposing it.

    Special handling: some tools (Jina) store their API key in system_settings
    rather than Tool.config. We resolve those as part of the global config so
    the agent-level UI can show the inherited key hint.
    """
    from app.core.permissions import check_agent_access
    from app.services.agent_tools import _agent_has_feishu

    agent_obj2, access_level = await check_agent_access(
        db, current_user, agent_id
    )
    has_feishu = await _agent_has_feishu(agent_id)

    # Determine if this is a system agent (e.g. OKR Agent).
    is_system_agent2 = bool(agent_obj2 and agent_obj2.is_system)

    assignments = await _load_agent_tool_assignments(db, agent_id)
    all_tools_r = await db.execute(
        select(Tool)
        .where(_globally_visible_tool_clause(), _agent_visible_tool_clause(agent_obj2.tenant_id, assignments))
        .order_by(Tool.category, Tool.name)
    )
    all_tools = all_tools_r.scalars().all()
    mcp_display_names = await load_mcp_display_names(db, all_tools)

    # Pre-fetch system_settings keys that some tools use as an alternative
    # config storage (e.g. Jina stores its API key in system_settings.jina_api_key)
    system_keys_cache: dict[str, str] = {}
    SYSTEM_SETTINGS_TOOL_MAP = {
        # tool_name -> system_settings key + value path
        "jina_search": ("jina_api_key", "api_key"),
        "jina_read": ("jina_api_key", "api_key"),
    }

    result = []
    for t in all_tools:
        # Hide feishu tools for agents without Feishu channel
        if t.category == "feishu" and not has_feishu:
            continue
        # Hide OKR Agent-exclusive tools from regular agents.
        if (t.config or {}).get("okr_agent_only") and not is_system_agent2:
            continue
        tid = str(t.id)
        at = assignments.get(tid)
        if not _tool_record_visible_to_agent(t, agent_obj2.tenant_id, assignments):
            continue
        enabled = resolved_agent_tool_enabled(t.name, at)

        # Decrypt tenant/company config for the frontend. Builtin tool configs
        # are tenant-scoped via tenant_settings, not shared Tool.config.
        raw_global = await get_tool_company_config(db, t, agent_obj2.tenant_id)

        # Fallback: resolve api_key from system_settings for tools that store
        # their key there (e.g. Jina). Only if Tool.config doesn't have it.
        if t.name in SYSTEM_SETTINGS_TOOL_MAP and not raw_global.get("api_key"):
            ss_key, ss_field = SYSTEM_SETTINGS_TOOL_MAP[t.name]
            if ss_key not in system_keys_cache:
                try:
                    from app.models.system_settings import SystemSetting
                    ss_r = await db.execute(
                        select(SystemSetting).where(SystemSetting.key == ss_key)
                    )
                    ss = ss_r.scalar_one_or_none()
                    system_keys_cache[ss_key] = (
                        ss.value.get(ss_field, "") if ss and ss.value else ""
                    )
                except Exception:
                    system_keys_cache[ss_key] = ""
            if system_keys_cache[ss_key]:
                raw_global["api_key"] = system_keys_cache[ss_key]

        raw_agent = _decrypt_sensitive_fields((at.config if at else {}) or {}, t.config_schema)
        visible_agent_config = (
            raw_agent
            if access_level == "manage"
            else mask_sensitive_fields(raw_agent, t.config_schema)
        )

        # Mask sensitive fields in global_config so users can see that a key
        # is configured at the company level without exposing the full value.
        masked_global = mask_sensitive_fields(raw_global, t.config_schema)

        result.append({
            "id": tid,
            "agent_tool_id": str(at.id) if at else None,
            "name": t.name,
            "display_name": t.display_name,
            "description": t.description,
            "type": t.type,
            "category": t.category,
            "icon": t.icon,
            "enabled": enabled,
            "is_default": t.is_default,
            "mcp_server_name": t.mcp_server_name,
            "mcp_server_display_name": mcp_display_names.get(t.mcp_server_id),
            "mcp_server_url": t.mcp_server_url,
            "mcp_server_id": str(t.mcp_server_id) if t.mcp_server_id else None,
            "config_schema": t.config_schema or {},
            "global_config": masked_global,
            "agent_config": visible_agent_config,
            "source": t.source,
            "agent_tool_source": at.source if at else None,
            **_tool_availability(t.name),
            "installed_by_agent_id": (
                str(at.installed_by_agent_id)
                if at and at.installed_by_agent_id
                else None
            ),
        })

    project_management_tools = [
        tool for tool in result if tool.get("category") == "project_management"
    ]
    if project_management_tools:
        visible_names = {str(tool.get("name") or "") for tool in project_management_tools}
        enabled_count = sum(bool(tool.get("enabled")) for tool in project_management_tools)
        member_count = len(project_management_tools)
        complete = visible_names == set(USER_PROJECT_TOOL_NAMES)
        if enabled_count == 0:
            state = "disabled"
        elif complete and enabled_count == member_count:
            state = "enabled"
        else:
            state = "partial"
        group_contract = {
            "key": "project_management",
            "state": state,
            "member_count": member_count,
            "enabled_count": enabled_count,
            "complete": complete,
        }
        for tool in project_management_tools:
            tool["capability_group"] = group_contract
    return result
