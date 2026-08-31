"""Shared helpers for the tools API."""

import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.okr_feature import OKR_TOOL_NAMES, is_retired_okr_tool, okr_feature_enabled
from app.core.plaza_feature import PLAZA_TOOL_NAMES
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.tool_config import (
    decrypt_sensitive_fields,
    encrypt_sensitive_fields,
    get_sensitive_keys,
    get_tool_company_config,
    mask_sensitive_fields,
    meaningful_config,
    set_tenant_tool_config,
)
from app.services.tool_enablement import (
    REQUIRED_AGENT_TOOL_NAMES,
    SUBAGENT_TOOL_NAMES,
    resolved_agent_tool_enabled,
    tool_is_required,
)
from app.services.user_project_tools import USER_PROJECT_TOOL_NAMES

router = APIRouter(prefix="/tools", tags=["tools"])


CATEGORY_CONFIG_PRIMARY_TOOL = {
    "agentbay": "agentbay_browser_navigate",
}


def _is_platform_admin(user: User) -> bool:
    """Recognize both legacy role and identity-backed platform admins."""
    return user.role == "platform_admin" or bool(
        getattr(getattr(user, "identity", None), "is_platform_admin", False)
    )


def _require_platform_admin(user: User) -> None:
    if not _is_platform_admin(user):
        raise HTTPException(status_code=403, detail="Platform admin required")


def _require_tenant_tool_admin(user: User, tenant_id: uuid.UUID | None) -> None:
    """Allow platform admins everywhere and org admins only in their tenant."""
    if _is_platform_admin(user):
        return
    if user.role == "org_admin" and tenant_id is not None and user.tenant_id == tenant_id:
        return
    raise HTTPException(status_code=403, detail="Organization admin required")


def _can_view_unmasked_company_config(user: User, tenant_id: uuid.UUID | None) -> bool:
    if _is_platform_admin(user):
        return True
    return bool(
        user.role == "org_admin"
        and tenant_id is not None
        and user.tenant_id == tenant_id
    )


def _tool_availability(tool_name: str) -> dict[str, str | bool]:
    """Public control-plane contract for required versus configurable tools."""
    required = tool_is_required(tool_name)
    return {
        "availability": "required" if required else "configurable",
        "can_disable": not required,
    }


def _reject_required_tool_disable(tool: Tool, enabled: bool | None) -> None:
    if enabled is False and tool_is_required(tool.name):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Tool '{tool.name}' is required by the platform and cannot be disabled"
            ),
        )


def _globally_visible_tool_clause():
    """Include required protocol tools even if legacy data marked them disabled."""
    enabled_clause = or_(Tool.enabled == True, Tool.name.in_(REQUIRED_AGENT_TOOL_NAMES))
    feature_clauses = [enabled_clause, Tool.name.not_in(PLAZA_TOOL_NAMES)]
    if not okr_feature_enabled():
        feature_clauses.append(Tool.name.not_in(OKR_TOOL_NAMES))
    return and_(*feature_clauses)


def _feature_visible_tool_clause():
    feature_clauses = [Tool.name.not_in(PLAZA_TOOL_NAMES)]
    if not okr_feature_enabled():
        feature_clauses.append(Tool.name.not_in(OKR_TOOL_NAMES))
    return and_(*feature_clauses)


def _require_feature_visible_tool(tool: Tool | None) -> Tool:
    if (
        tool is None
        or tool.name in PLAZA_TOOL_NAMES
        or is_retired_okr_tool(tool.name)
    ):
        raise HTTPException(status_code=404, detail="Tool not found")
    return tool


async def _load_agent_for_tool_scope(db: AsyncSession, agent_id: uuid.UUID):
    """Load the agent whose tenant boundary determines tool visibility."""
    from app.models.agent import Agent as AgentModel

    agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
    agent = agent_r.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="未找到数字员工")
    return agent


async def _load_agent_tool_assignments(db: AsyncSession, agent_id: uuid.UUID) -> dict[str, AgentTool]:
    """Return explicit tool assignments for one agent keyed by tool ID string."""
    agent_tools_r = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))
    return {str(at.tool_id): at for at in agent_tools_r.scalars().all()}


def _agent_visible_tool_clause(agent_tenant_id: uuid.UUID | None, assignments: dict[str, AgentTool]):
    """Build the DB filter for tools visible to an agent.

    Visibility rules:
    - builtin tools are global platform capabilities
    - admin tools belong only to the agent's company or are platform-wide (tenant_id is NULL)
    - explicitly assigned tools are always visible
    """
    clauses = [Tool.source == "builtin"]
    # Platform-level admin tools (tenant_id IS NULL) are visible to all tenants;
    # tenant-scoped admin tools are restricted to their own tenant.
    if agent_tenant_id:
        clauses.append((Tool.source == "admin") & (
            (Tool.tenant_id == agent_tenant_id) | (Tool.tenant_id.is_(None))
        ))
    else:
        clauses.append((Tool.source == "admin") & (Tool.tenant_id.is_(None)))

    assigned_tool_ids = [uuid.UUID(tool_id) for tool_id in assignments]
    if assigned_tool_ids:
        clauses.append(Tool.id.in_(assigned_tool_ids))

    return or_(*clauses)


def _tool_record_visible_to_agent(
    tool: Tool,
    agent_tenant_id: uuid.UUID | None,
    assignments: dict[str, AgentTool],
) -> bool:
    """Pure visibility check mirroring _agent_visible_tool_clause."""
    if str(tool.id) in assignments:
        return True
    if tool.source == "builtin":
        return True
    if tool.source == "admin":
        # Platform-level admin tool (tenant_id IS NULL) visible to all tenants.
        if tool.tenant_id is None:
            return True
        return bool(agent_tenant_id and tool.tenant_id == agent_tenant_id)
    if tool.source == "agent":
        return str(tool.id) in assignments
    return False


def _resolve_target_tenant_id(current_user: User, tenant_id: str | None = None) -> uuid.UUID | None:
    if tenant_id:
        try:
            requested_tenant_id = uuid.UUID(tenant_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid tenant_id format")
        if not _is_platform_admin(current_user) and requested_tenant_id != current_user.tenant_id:
            raise HTTPException(
                status_code=403,
                detail="Only platform admins may access another tenant",
            )
        return requested_tenant_id
    return current_user.tenant_id


def _get_sensitive_keys(config_schema: dict | None = None) -> set[str]:
    return get_sensitive_keys(config_schema)


def _encrypt_sensitive_fields(config: dict, config_schema: dict | None = None) -> dict:
    return encrypt_sensitive_fields(config, config_schema)


def _decrypt_sensitive_fields(config: dict, config_schema: dict | None = None) -> dict:
    return decrypt_sensitive_fields(config, config_schema)
