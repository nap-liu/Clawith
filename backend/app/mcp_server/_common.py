"""Shared helpers for MCP write/discovery tools."""
from __future__ import annotations

import uuid as _uuid

from sqlalchemy import or_, select

from app.core.permissions import user_can_manage_agent_id
from app.mcp_server.auth import resolve_pat_context, require_write
from app.models.agent import Agent  # noqa: F401  (kept for type clarity / future use)
from app.models.tool import Tool

_UNAUTH = "❌ 未鉴权：请在 MCP 客户端配置 Authorization: Bearer <clw_...> 令牌。"
_NEED_WRITE = "❌ 权限不足：此操作需要 write 范围的 PAT（当前令牌为只读）。请在账户设置中签发 write 令牌。"
_NO_MANAGE = "❌ 无权管理该 agent（需要 manage 权限）。"
_NO_AGENT = "❌ 找不到该 agent，或你无权访问。"


async def authed_write(ctx, db):
    """Return (PatContext, None) on success, or (None, error_str) to return verbatim."""
    pc = await resolve_pat_context(ctx, db)
    if pc is None:
        return None, _UNAUTH
    if not require_write(pc):
        return None, _NEED_WRITE
    return pc, None


async def resolve_manageable_agent(db, pc, agent_ref: str):
    """Resolve agent_ref (id or name) in the user's visible set AND require manage.
    Returns (Agent, None) or (None, error_str)."""
    from app.mcp_server.tools import _resolve_visible_agent  # function-local: avoid import cycle
    agent = await _resolve_visible_agent(db, pc.user, agent_ref)
    if agent is None:
        return None, _NO_AGENT
    if not await user_can_manage_agent_id(db, pc.user.id, agent):
        return None, _NO_MANAGE
    return agent, None


def _tenant_tool_clause(tenant_id):
    """Tools enablable for a tenant: builtin (global) + admin (tenant or platform)."""
    clauses = [Tool.source == "builtin"]
    if tenant_id:
        clauses.append((Tool.source == "admin") & ((Tool.tenant_id == tenant_id) | (Tool.tenant_id.is_(None))))
    else:
        clauses.append((Tool.source == "admin") & (Tool.tenant_id.is_(None)))
    return or_(*clauses)


async def resolve_tenant_tool(db, tenant_id, tool_ref: str):
    """Resolve a tool by UUID or name within the tenant's enablable set, or None."""
    base = select(Tool).where(Tool.enabled == True, _tenant_tool_clause(tenant_id))  # noqa: E712
    try:
        tid = _uuid.UUID(str(tool_ref))
        row = (await db.execute(base.where(Tool.id == tid))).scalar_one_or_none()
        if row:
            return row
    except (ValueError, TypeError):
        pass
    rows = (await db.execute(base.where(Tool.name == tool_ref))).scalars().all()
    return rows[0] if len(rows) == 1 else None
