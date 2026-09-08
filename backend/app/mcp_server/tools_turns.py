"""MCP control-plane tools for live root turns."""

from __future__ import annotations

import asyncio
import uuid

from mcp.server.fastmcp import Context
from sqlalchemy import select

from app.core.permissions import is_platform_admin_user
from app.database import async_session
from app.mcp_server import mcp
from app.mcp_server._common import authed_write
from app.mcp_server.auth import resolve_pat_context
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.active_turns import (
    reserve_active_turn_stop,
    wait_until_stopped,
)
from app.services.active_turns import (
    list_active_turns as list_registered_turns,
)

_UNAUTH = "❌ 未鉴权：请在 MCP 客户端配置 Authorization: Bearer <clw_...> 令牌。"
_TURN_LABELS = {
    "web": "Web",
    "mcp": "MCP",
    "agent": "A2A",
    "gateway": "Gateway",
    "trigger": "触发器",
    "schedule": "定时任务",
    "task": "后台任务",
    "heartbeat": "心跳",
    "oneshot": "一次性执行",
    "subagent": "Subagent",
    "recovery": "恢复任务",
}


def _uuid_or_none(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError):
        return None


@mcp.tool()
async def list_active_turns(ctx: Context) -> str:
    """列出当前正在执行的所有类型 turn。普通用户只看自己的；平台超管看全部。"""

    async with async_session() as db:
        pc = await resolve_pat_context(ctx, db)
        if pc is None:
            return _UNAUTH
        is_admin = is_platform_admin_user(pc.user)
        records = await list_registered_turns(
            owner_user_id=None if is_admin else pc.user.id
        )
        if not records:
            return "（当前没有激活中的 turn）"

        agent_ids = {record.agent_id for record in records}
        user_ids = {record.owner_user_id for record in records}
        agent_names = dict(
            (
                await db.execute(
                    select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids))
                )
            ).all()
        )
        user_names = dict(
            (
                await db.execute(
                    select(User.id, User.display_name).where(User.id.in_(user_ids))
                )
            ).all()
        )

        session_ids = {
            sid
            for record in records
            if (sid := _uuid_or_none(record.session_id)) is not None
        }
        sessions = {}
        if session_ids:
            sessions = {
                row.id: row
                for row in (
                    await db.execute(
                        select(ChatSession).where(ChatSession.id.in_(session_ids))
                    )
                ).scalars()
            }

        lines = [f"当前共 {len(records)} 个激活 turn："]
        for index, record in enumerate(records, start=1):
            session = sessions.get(_uuid_or_none(record.session_id))
            turn_type = record.turn_type or (
                session.source_channel if session is not None else "background"
            )
            title = record.title or (
                (session.group_name or session.title) if session is not None else None
            )
            owner = user_names.get(record.owner_user_id, str(record.owner_user_id))
            agent = agent_names.get(record.agent_id, str(record.agent_id))
            lines.append(
                f"{index}. turn_id={record.turn_id} · 类型={_TURN_LABELS.get(turn_type, turn_type)}"
                f" · agent={agent} · owner={owner} · session={record.session_id}"
                f" · 标题={title or '—'} · 开始={record.started_at.isoformat()}"
            )
        return "\n".join(lines)


@mcp.tool()
async def stop_turn(ctx: Context, turn_id: str) -> str:
    """按 list_active_turns 返回的 turn_id 精确终止一个 turn；需要 write PAT。"""

    async with async_session() as db:
        pc, error = await authed_write(ctx, db)
        if error:
            return error
        is_admin = is_platform_admin_user(pc.user)
        actor_user_id = pc.user.id

    record, stop_token = await reserve_active_turn_stop(
        turn_id,
        owner_user_id=None if is_admin else actor_user_id,
    )
    if record is None:
        return "❌ 找不到该激活 turn，或你无权终止。"
    if stop_token is None:
        stopped = await wait_until_stopped(record)
        state = "已终止" if stopped else "已发送终止请求"
        return f"✅ turn {record.turn_id} {state}。"

    control_task = asyncio.create_task(
        _commit_reserved_stop(
            record=record,
            stop_token=stop_token,
            actor_user_id=actor_user_id,
            is_admin=is_admin,
        )
    )
    request_cancelled = False
    while True:
        try:
            outcome = await asyncio.shield(control_task)
            break
        except asyncio.CancelledError:
            request_cancelled = True
            if control_task.done():
                outcome = control_task.result()
                break

    if request_cancelled:
        raise asyncio.CancelledError
    if outcome == "completed":
        return f"✅ turn {record.turn_id} 已完成，无需终止。"
    if outcome == "ended":
        return f"✅ turn {turn_id} 已在终止请求提交前结束。"
    stopped = await wait_until_stopped(record)
    state = "已终止" if stopped else "已发送终止请求"
    return f"✅ turn {record.turn_id} {state}。"


async def _commit_reserved_stop(
    *,
    record,
    stop_token: str,
    actor_user_id: uuid.UUID,
    is_admin: bool,
) -> str:
    """Keep the MCP response/audit adapter on the shared stop commit protocol."""
    from app.services.active_turn_stop import commit_reserved_turn_stop

    return await commit_reserved_turn_stop(
        record=record,
        stop_token=stop_token,
        reason=f"MCP stop_turn by user {actor_user_id}",
        audit_action="mcp_turn_stop_requested",
        actor_user_id=actor_user_id,
        is_admin=is_admin,
    )
