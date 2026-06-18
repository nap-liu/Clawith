"""MCP schedule tools (write scope, creator/admin only).

Mirrors REST /agents/{id}/schedules.
delete and run are confirm-guided (SENSITIVE).
create/update return revert hints.
"""
from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import Context
from sqlalchemy import select

from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server._common import authed_write, needs_confirm, resolve_manageable_agent
from app.core.permissions import is_agent_creator, is_agent_expired
from app.models.schedule import AgentSchedule
from app.services.scheduler import compute_next_run

_NOT_CREATOR = "❌ 仅创建者或管理员可管理 schedules。"
_NOT_FOUND = "❌ 找不到该 schedule。"
_INVALID_CRON = "❌ 无效 cron 表达式：{}"
_AGENT_EXPIRED = "❌ Agent 已过期，无法触发执行。"


def _is_creator_or_admin(user, agent) -> bool:
    return is_agent_creator(user, agent) or user.role in ("platform_admin", "org_admin")


def _fmt_schedule(s: AgentSchedule) -> str:
    return (
        f"  id={s.id} name={s.name!r} cron={s.cron_expr!r} enabled={s.is_enabled}"
        f" instruction={s.instruction[:60]!r} last_run={s.last_run_at} next_run={s.next_run_at}"
        f" run_count={s.run_count}"
    )


# ── list ──────────────────────────────────────────────────────────────────────

async def list_agent_schedules_impl(ctx, agent: str) -> str:
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        if not _is_creator_or_admin(pc.user, ag):
            return _NOT_CREATOR

        result = await db.execute(
            select(AgentSchedule)
            .where(AgentSchedule.agent_id == ag.id)
            .order_by(AgentSchedule.created_at.desc())
        )
        schedules = result.scalars().all()
        if not schedules:
            return f"「{ag.name}」暂无 schedule。"
        lines = [f"「{ag.name}」共 {len(schedules)} 个 schedule："]
        for s in schedules:
            lines.append(_fmt_schedule(s))
        return "\n".join(lines)


# ── set (create or update) ────────────────────────────────────────────────────

async def set_agent_schedule_impl(
    ctx,
    agent: str,
    name: str,
    cron_expr: str,
    instruction: str = "",
    is_enabled: bool = True,
    schedule_id: str | None = None,
) -> str:
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        if not _is_creator_or_admin(pc.user, ag):
            return _NOT_CREATOR

        # Validate cron
        next_run = compute_next_run(cron_expr)
        if next_run is None:
            return _INVALID_CRON.format(cron_expr)

        if schedule_id is not None:
            # UPDATE path
            try:
                sid = _uuid.UUID(str(schedule_id))
            except (ValueError, TypeError):
                return f"❌ schedule_id 不是合法 UUID：{schedule_id}"

            result = await db.execute(
                select(AgentSchedule).where(
                    AgentSchedule.id == sid,
                    AgentSchedule.agent_id == ag.id,
                )
            )
            sched = result.scalar_one_or_none()
            if sched is None:
                return _NOT_FOUND

            # Capture before values for revert hint
            old_name = sched.name
            old_cron = sched.cron_expr
            old_instruction = sched.instruction
            old_enabled = sched.is_enabled

            sched.name = name
            sched.cron_expr = cron_expr
            sched.instruction = instruction
            sched.is_enabled = is_enabled
            sched.next_run_at = next_run if is_enabled else None

            await db.commit()
            revert = (
                f"↩ 回滚：set_agent_schedule(agent={ag.name!r}, schedule_id={schedule_id!r},"
                f" name={old_name!r}, cron_expr={old_cron!r},"
                f" instruction={old_instruction[:60]!r}, is_enabled={old_enabled})"
            )
            return f"✅ 已更新「{ag.name}」的 schedule「{name}」(id={sid})。\n{revert}"

        else:
            # CREATE path
            sched = AgentSchedule(
                agent_id=ag.id,
                name=name,
                instruction=instruction,
                cron_expr=cron_expr,
                is_enabled=is_enabled,
                next_run_at=next_run if is_enabled else None,
                created_by=pc.user.id,
            )
            db.add(sched)
            await db.flush()
            new_id = sched.id
            await db.commit()
            revert = (
                f"↩ 回滚：delete_agent_schedule(agent={ag.name!r}, schedule_id={new_id!r}, confirm=True)"
            )
            return f"✅ 已创建「{ag.name}」的 schedule「{name}」(id={new_id})，下次执行：{next_run}。\n{revert}"


# ── delete ────────────────────────────────────────────────────────────────────

async def delete_agent_schedule_impl(
    ctx,
    agent: str,
    schedule_id: str,
    confirm: bool = False,
) -> str:
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        if not _is_creator_or_admin(pc.user, ag):
            return _NOT_CREATOR

        try:
            sid = _uuid.UUID(str(schedule_id))
        except (ValueError, TypeError):
            return f"❌ schedule_id 不是合法 UUID：{schedule_id}"

        result = await db.execute(
            select(AgentSchedule).where(
                AgentSchedule.id == sid,
                AgentSchedule.agent_id == ag.id,
            )
        )
        sched = result.scalar_one_or_none()
        if sched is None:
            return _NOT_FOUND

        guidance = needs_confirm(confirm, f"将删除 schedule {schedule_id}（名称：{sched.name!r}）")
        if guidance:
            return guidance

        sched_name = sched.name
        await db.delete(sched)
        await db.commit()
        return f"✅ 已删除「{ag.name}」的 schedule「{sched_name}」(id={sid})。"


# ── run ───────────────────────────────────────────────────────────────────────

async def run_agent_schedule_impl(
    ctx,
    agent: str,
    schedule_id: str,
    confirm: bool = False,
) -> str:
    async with async_session() as db:
        pc, err = await authed_write(ctx, db)
        if err:
            return err
        ag, err = await resolve_manageable_agent(db, pc, agent)
        if err:
            return err
        if not _is_creator_or_admin(pc.user, ag):
            return _NOT_CREATOR

        if is_agent_expired(ag):
            return _AGENT_EXPIRED

        try:
            sid = _uuid.UUID(str(schedule_id))
        except (ValueError, TypeError):
            return f"❌ schedule_id 不是合法 UUID：{schedule_id}"

        result = await db.execute(
            select(AgentSchedule).where(
                AgentSchedule.id == sid,
                AgentSchedule.agent_id == ag.id,
            )
        )
        sched = result.scalar_one_or_none()
        if sched is None:
            return _NOT_FOUND

        guidance = needs_confirm(confirm, f"将立即执行 schedule {schedule_id}（名称：{sched.name!r}，指令：{sched.instruction[:60]!r}）")
        if guidance:
            return guidance

        # Fire in background — same mechanism as REST trigger_schedule
        import asyncio
        from app.services.scheduler import _execute_schedule
        asyncio.create_task(_execute_schedule(sched.id, sched.agent_id, sched.instruction))

        # Update tracking
        sched.last_run_at = datetime.now(timezone.utc)
        sched.run_count = (sched.run_count or 0) + 1
        await db.commit()

        return f"✅ 已触发「{ag.name}」的 schedule「{sched.name}」(id={sid}) 异步执行。"


# ── @mcp.tool() wrappers ──────────────────────────────────────────────────────

@mcp.tool()
async def list_agent_schedules(ctx: Context, agent: str) -> str:
    """List all cron schedules for an agent (creator/admin only).
    agent: agent id or name.
    Returns each schedule's id, name, cron_expr, is_enabled, instruction, last_run_at, next_run_at,
    and run_count. Requires write-scope PAT."""
    return await list_agent_schedules_impl(ctx, agent)


@mcp.tool()
async def set_agent_schedule(
    ctx: Context,
    agent: str,
    name: str,
    cron_expr: str,
    instruction: str = "",
    is_enabled: bool = True,
    schedule_id: str | None = None,
) -> str:
    """Create or update a cron schedule for an agent (creator/admin only, write-scope PAT).
    agent: agent id or name.
    name: schedule name (required).
    cron_expr: standard 5-field cron expression (e.g. "0 9 * * *"). Validated — returns ❌ on invalid.
    instruction: the instruction the agent will run on each trigger.
    is_enabled: whether the schedule is active (default True).
    schedule_id: if provided, updates the existing schedule; otherwise creates a new one.
    Returns ✅ with a revert hint on success (for creates: delete call; for updates: set call with old values)."""
    return await set_agent_schedule_impl(
        ctx, agent=agent, name=name, cron_expr=cron_expr,
        instruction=instruction, is_enabled=is_enabled, schedule_id=schedule_id,
    )


@mcp.tool()
async def delete_agent_schedule(
    ctx: Context,
    agent: str,
    schedule_id: str,
    confirm: bool = False,
) -> str:
    """Delete a cron schedule from an agent (creator/admin only, SENSITIVE — confirm-guided).
    agent: agent id or name.
    schedule_id: UUID of the schedule to delete.
    confirm: must be True to execute; omitting or False returns a confirmation prompt with no changes made.
    Requires write-scope PAT."""
    return await delete_agent_schedule_impl(ctx, agent=agent, schedule_id=schedule_id, confirm=confirm)


@mcp.tool()
async def run_agent_schedule(
    ctx: Context,
    agent: str,
    schedule_id: str,
    confirm: bool = False,
) -> str:
    """Manually trigger an agent schedule for immediate execution (creator/admin only, SENSITIVE — confirm-guided).
    agent: agent id or name.
    schedule_id: UUID of the schedule to run.
    confirm: must be True to execute; omitting or False returns a confirmation prompt with no changes made.
    The schedule runs asynchronously in the background (same mechanism as the automatic cron trigger).
    Blocked if the agent has expired. Requires write-scope PAT."""
    return await run_agent_schedule_impl(ctx, agent=agent, schedule_id=schedule_id, confirm=confirm)
