"""MCP agent lifecycle tools (write scope): start/stop/soft-delete/restore."""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import Context
from sqlalchemy import select

from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server._common import authed_write, needs_confirm, resolve_manageable_agent
from app.models.agent import Agent

_ADMIN_ROLES = ("platform_admin", "org_admin")


async def start_agent_impl(ctx, agent, confirm=False) -> str:
    from app.services.agent_manager import agent_manager

    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        guidance = needs_confirm(confirm, f"将启动「{ag.name}」")
        if guidance:
            return guidance
        await agent_manager.start_container(db, ag)
        await db.commit()
        return f"✅ 已启动「{ag.name}」(id={ag.id})，状态={ag.status}。\n↩ 回滚：stop_agent"


async def stop_agent_impl(ctx, agent, confirm=False) -> str:
    from app.services.agent_manager import agent_manager

    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        guidance = needs_confirm(confirm, f"将停止「{ag.name}」")
        if guidance:
            return guidance
        await agent_manager.stop_container(ag)
        await db.commit()
        return f"✅ 已停止「{ag.name}」(id={ag.id})，状态={ag.status}。\n↩ 回滚：start_agent"


async def delete_agent_impl(ctx, agent, confirm=False) -> str:
    from app.core.permissions import is_agent_creator
    from app.services.agent_manager import agent_manager

    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        # Stricter gate: only the creator or a tenant/platform admin may delete.
        if not (is_agent_creator(pc.user, ag) or pc.user.role in _ADMIN_ROLES):
            return (
                "❌ 仅创建者或管理员可删除该 agent。"
                "请以 agent 创建者身份或 platform_admin/org_admin 角色重试，或联系管理员操作。"
            )
        if ag.is_system:
            return "❌ 系统 agent 不可删除。系统 agent 为平台内置，不支持删除操作。"
        guidance = needs_confirm(confirm, f"将软删除「{ag.name}」(可用 restore_agent 恢复)")
        if guidance:
            return guidance
        ag.is_deleted = True
        ag.deleted_at = datetime.now(timezone.utc)
        # Best-effort container stop — soft delete must succeed even if Docker errors.
        try:
            await agent_manager.stop_container(ag)
        except Exception:
            pass
        await db.commit()
        return (f"✅ 已软删除「{ag.name}」(id={ag.id})。"
                f"\n↩ 回滚：restore_agent(agent={ag.id})")


async def restore_agent_impl(ctx, agent, confirm=False) -> str:
    from app.core.permissions import get_agent_access_level_for_user_id, is_agent_creator
    from app.services.agent_manager import agent_manager

    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        # Soft-deleted agents are hidden from resolve_manageable_agent, so look
        # them up directly by id (tenant-scoped).
        try:
            aid = _uuid.UUID(str(agent))
        except (ValueError, TypeError):
            return (
                f"❌ 找不到该 agent（{agent!r} 不是合法 UUID）。"
                "restore_agent 需要传入 agent 的 UUID（从 delete_agent 的响应中获取）。"
            )
        ag = (await db.execute(select(Agent).where(Agent.id == aid))).scalar_one_or_none()
        if ag is None or ag.tenant_id != pc.tenant_id:
            return (
                f"❌ 找不到该 agent（id={agent}）。"
                "请确认 UUID 来自 delete_agent 的响应，且该 agent 属于你的租户。"
            )
        # Manage check (mirror delete's gate): creator/admin or explicit manage level.
        level = await get_agent_access_level_for_user_id(db, pc.user.id, ag)
        if not (level == "manage" or is_agent_creator(pc.user, ag) or pc.user.role in _ADMIN_ROLES):
            return (
                "❌ 无权恢复该 agent（需要 manage 权限）。"
                "请以 agent 创建者身份或 platform_admin/org_admin 角色重试，或联系管理员授予 manage 权限。"
            )
        if not ag.is_deleted:
            return f"（「{ag.name}」未被删除，无需恢复。若要修改其配置，请直接使用 update_agent 等工具。）"
        ag.is_deleted = False
        ag.deleted_at = None
        # Best-effort restart — restore must succeed even if Docker errors.
        try:
            await agent_manager.start_container(db, ag)
        except Exception:
            pass
        await db.commit()
        return f"✅ 已恢复「{ag.name}」(id={ag.id})，状态={ag.status}。"


@mcp.tool()
async def start_agent(ctx: Context, agent: str, confirm: bool = False) -> str:  # noqa: D401
    """Start an agent's runtime container (requires write scope + manage access).
    agent: id or name. SENSITIVE / confirm-guided — call once to get a confirmation
    prompt, then re-call with confirm=True to execute. Revert with stop_agent."""
    return await start_agent_impl(ctx, agent, confirm=confirm)


@mcp.tool()
async def stop_agent(ctx: Context, agent: str, confirm: bool = False) -> str:  # noqa: D401
    """Stop an agent's runtime container (requires write scope + manage access).
    agent: id or name. SENSITIVE / confirm-guided — call once to get a confirmation
    prompt, then re-call with confirm=True to execute. Revert with start_agent."""
    return await stop_agent_impl(ctx, agent, confirm=confirm)


@mcp.tool()
async def delete_agent(ctx: Context, agent: str, confirm: bool = False) -> str:  # noqa: D401
    """Soft-delete an agent (requires write scope; creator or admin only).
    agent: id or name. SOFT delete — the agent is marked deleted, stopped, and
    hidden from all listings, but is fully restorable via restore_agent. System
    agents cannot be deleted. confirm-guided — call once to get a confirmation
    prompt, then re-call with confirm=True to execute. The response includes the
    restore hint (restore_agent with the agent id)."""
    return await delete_agent_impl(ctx, agent, confirm=confirm)


@mcp.tool()
async def restore_agent(ctx: Context, agent: str) -> str:  # noqa: D401
    """Restore a soft-deleted agent (requires write scope + manage access).
    agent: the agent's UUID (soft-deleted agents are hidden from name/id discovery,
    so pass the id from the delete_agent response). Non-destructive — no confirm
    needed. Clears the deleted flag and best-effort restarts the runtime."""
    return await restore_agent_impl(ctx, agent)
