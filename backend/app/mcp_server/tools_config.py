"""MCP configuration tools (write scope, all require manage)."""
from __future__ import annotations

import uuid as _uuid

from mcp.server.fastmcp import Context
from sqlalchemy import select

from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server._common import authed_write, resolve_manageable_agent, resolve_tenant_tool
from app.models.tool import AgentTool


# ── E1: set_agent_tools ──
async def _apply_tool(db, agent_id, tool, enabled: bool):
    row = (await db.execute(select(AgentTool).where(
        AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id))).scalar_one_or_none()
    if row:
        row.enabled = enabled
    else:
        db.add(AgentTool(agent_id=agent_id, tool_id=tool.id, enabled=enabled))


async def set_agent_tools_impl(ctx, agent, enable=None, disable=None) -> str:
    enable = enable or []
    disable = disable or []
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        applied, missing = [], []
        for ref in enable:
            tool = await resolve_tenant_tool(db, ag.tenant_id, ref)
            if tool is None:
                missing.append(ref)
                continue
            await _apply_tool(db, ag.id, tool, True)
            applied.append(f"+{tool.name}")
        for ref in disable:
            tool = await resolve_tenant_tool(db, ag.tenant_id, ref)
            if tool is None:
                missing.append(ref)
                continue
            await _apply_tool(db, ag.id, tool, False)
            applied.append(f"-{tool.name}")
        await db.commit()
        msg = f"✅ 已更新「{ag.name}」工具：{', '.join(applied) or '（无变化）'}。"
        if missing:
            msg += f"\n⚠ 找不到这些工具（用 list_available_tools 查看）：{', '.join(missing)}"
        return msg


# ── E2: set_agent_trigger ──
async def set_agent_trigger_impl(ctx, agent, name, type, config, reason,
                                 focus_ref=None, webhook_mode=None) -> str:
    from app.services.agent_tools import _handle_set_trigger
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        agent_id = ag.id  # capture UUID before leaving the session scope
    arguments = {"name": name, "type": type, "config": config, "reason": reason}
    if focus_ref is not None:
        arguments["focus_ref"] = focus_ref
    if webhook_mode is not None:
        arguments["webhook_mode"] = webhook_mode
    # _handle_set_trigger validates type/config and persists in its own session; returns a string.
    return await _handle_set_trigger(agent_id, arguments, user_id=pc.user.id)


# ── E3: delete_agent_trigger ──
async def delete_agent_trigger_impl(ctx, agent, trigger) -> str:
    from app.models.trigger import AgentTrigger
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        q = select(AgentTrigger).where(AgentTrigger.agent_id == ag.id)
        row = None
        try:
            tid = _uuid.UUID(str(trigger))
            row = (await db.execute(q.where(AgentTrigger.id == tid))).scalar_one_or_none()
        except (ValueError, TypeError):
            row = (await db.execute(q.where(AgentTrigger.name == trigger))).scalar_one_or_none()
        if row is None:
            return "❌ 找不到该触发器（按 name 或 id 指定）。"
        tname = row.name
        await db.delete(row)
        await db.commit()
        return f"✅ 已删除「{ag.name}」的触发器：{tname}。"


@mcp.tool()
async def set_agent_tools(ctx: Context, agent: str, enable: list[str] = [], disable: list[str] = []) -> str:  # noqa: D401,B006
    """Enable or disable tools on an agent (write scope + manage).
    agent: id or name. enable/disable: lists of tool ids or names (see list_available_tools)."""
    return await set_agent_tools_impl(ctx, agent=agent, enable=enable, disable=disable)


@mcp.tool()
async def set_agent_trigger(ctx: Context, agent: str, name: str, type: str, config: dict,  # noqa: D401
                            reason: str, focus_ref: str | None = None, webhook_mode: str | None = None) -> str:
    """Create or update an autonomous trigger on an agent (write scope + manage).
    type: cron | once | interval | poll | on_message | webhook.
    config per type — cron: {"expr": "0 9 * * *"}; once: {"at": "<iso8601>"}; interval: {"minutes": N};
    poll: {"url": "..."}; on_message: {"from_agent_name": "..."} or {"from_user_name": "..."}; webhook: {} (token auto-issued).
    reason explains what the trigger should do. For webhook triggers the returned text includes the callback URL."""
    return await set_agent_trigger_impl(ctx, agent=agent, name=name, type=type, config=config,
                                        reason=reason, focus_ref=focus_ref, webhook_mode=webhook_mode)


@mcp.tool()
async def delete_agent_trigger(ctx: Context, agent: str, trigger: str) -> str:  # noqa: D401
    """Delete an autonomous trigger from an agent (write scope + manage).
    agent: id or name. trigger: the trigger's id or name."""
    return await delete_agent_trigger_impl(ctx, agent=agent, trigger=trigger)
