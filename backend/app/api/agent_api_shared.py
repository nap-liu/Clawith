"""Agent (Digital Employee) API routes."""

import hashlib
import json
import sys
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import String, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.config import get_settings
from app.core.permissions import (
    build_agent_accessible_user_ids_query,
    build_visible_agents_query,
    check_agent_access as _check_agent_access_impl,
    is_agent_creator as _is_agent_creator_impl,
)
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent, AgentPermission
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.org import OrgDepartment, OrgMember
from app.models.subagent_run import SubagentRun
from app.models.user import Identity, User
from app.schemas.schemas import (
    AgentCreate,
    AgentExplorePageOut,
    AgentOut,
    AgentUpdate,
)
from app.services.access_relationships import ensure_access_granted_platform_relationships
from app.services.org_directory import (
    canonical_org_member_id_subquery,
    department_subtree_cte,
    permission_directory_departments,
    permission_directory_members,
)

router = APIRouter(prefix="/agents", tags=["agents"])
settings = get_settings()


async def check_agent_access(*args, **kwargs):
    root = sys.modules.get("app.api.agents")
    override = getattr(root, "check_agent_access", None) if root else None
    target = override if override is not None and override is not check_agent_access else _check_agent_access_impl
    return await target(*args, **kwargs)


def is_agent_creator(*args, **kwargs):
    root = sys.modules.get("app.api.agents")
    override = getattr(root, "is_agent_creator", None) if root else None
    target = override if override is not None and override is not is_agent_creator else _is_agent_creator_impl
    return target(*args, **kwargs)


async def _get_active_admin_users(db: AsyncSession, tenant_id: uuid.UUID | None) -> list[User]:
    if not tenant_id:
        return []
    result = await db.execute(
        select(User).where(
            User.tenant_id == tenant_id,
            User.is_active == True,  # noqa: E712
            User.role.in_(["platform_admin", "org_admin"]),
        )
    )
    return result.scalars().all()


def _serialize_dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _archive_agent_task_history(db: AsyncSession, agent_id: uuid.UUID, archive_dir: Path) -> Path | None:
    """Persist task and task-log history into the agent archive directory before DB cleanup."""
    from app.models.task import Task, TaskLog

    task_result = await db.execute(select(Task).where(Task.agent_id == agent_id).order_by(Task.created_at.asc()))
    tasks = task_result.scalars().all()
    if not tasks:
        return None

    archive_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "agent_id": str(agent_id),
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "tasks": [],
    }

    for task in tasks:
        log_result = await db.execute(select(TaskLog).where(TaskLog.task_id == task.id).order_by(TaskLog.created_at.asc()))
        logs = log_result.scalars().all()
        payload["tasks"].append(
            {
                "id": str(task.id),
                "title": task.title,
                "description": task.description,
                "type": task.type,
                "status": task.status,
                "priority": task.priority,
                "assignee": task.assignee,
                "created_by": str(task.created_by),
                "due_date": _serialize_dt(task.due_date),
                "supervision_target_user_id": (
                    str(task.supervision_target_user_id) if task.supervision_target_user_id else None
                ),
                "supervision_target_agent_id": (
                    str(task.supervision_target_agent_id)
                    if getattr(task, "supervision_target_agent_id", None)
                    else None
                ),
                "supervision_target_name": task.supervision_target_name,
                "supervision_channel": task.supervision_channel,
                "remind_schedule": task.remind_schedule,
                "created_at": _serialize_dt(task.created_at),
                "updated_at": _serialize_dt(task.updated_at),
                "completed_at": _serialize_dt(task.completed_at),
                "logs": [
                    {
                        "id": str(log.id),
                        "content": log.content,
                        "created_at": _serialize_dt(log.created_at),
                    }
                    for log in logs
                ],
            }
        )

    archive_path = archive_dir / "task_history.json"
    archive_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return archive_path


async def _lazy_reset_token_counters(agent: Agent, db: AsyncSession) -> bool:
    """Reset daily/monthly token counters if the day or month has changed.

    Returns True if any counter was reset (caller should commit/flush).
    """
    from datetime import datetime
    from datetime import timezone as tz
    now = datetime.now(tz.utc)
    from sqlalchemy import or_, update

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    daily = await db.execute(
        update(Agent)
        .where(
            Agent.id == agent.id,
            or_(Agent.last_daily_reset.is_(None), Agent.last_daily_reset < day_start),
        )
        .values(
            tokens_used_today=0,
            cache_read_tokens_today=0,
            cache_creation_tokens_today=0,
            last_daily_reset=now,
        )
    )
    monthly = await db.execute(
        update(Agent)
        .where(
            Agent.id == agent.id,
            or_(Agent.last_monthly_reset.is_(None), Agent.last_monthly_reset < month_start),
        )
        .values(
            tokens_used_month=0,
            cache_read_tokens_month=0,
            cache_creation_tokens_month=0,
            last_monthly_reset=now,
        )
    )
    changed = bool(daily.rowcount or monthly.rowcount)
    if changed:
        await db.refresh(agent)
    return changed


async def _build_unread_count_by_agent(
    db: AsyncSession,
    agents: list[Agent],
    current_user: User,
) -> dict[str, int]:
    """Return unread assistant/system/tool message counts for the current user per agent.

    The sidebar only needs user-facing unread state, so we scope strictly to sessions owned by
    the current platform user and ignore agent-to-agent / trigger-only threads.
    """

    return await _build_unread_count_by_agent_ids(
        db,
        [agent.id for agent in agents],
        current_user,
    )


async def _build_unread_count_by_agent_ids(
    db: AsyncSession,
    agent_ids: list[uuid.UUID],
    current_user: User,
) -> dict[str, int]:
    if not agent_ids:
        return {}

    result = await db.execute(
        select(ChatSession.agent_id, func.count(ChatMessage.id))
        .join(ChatMessage, ChatMessage.conversation_id == cast(ChatSession.id, String))
        .where(
            ChatSession.agent_id.in_(agent_ids),
            ChatSession.user_id == current_user.id,
            ChatSession.is_group.is_(False),
            ChatSession.source_channel.notin_(["agent", "trigger", "subagent"]),
            ChatMessage.role.in_(["assistant", "system", "tool_call"]),
            ChatMessage.created_at > func.coalesce(
                ChatSession.last_read_at_by_user,
                datetime(1970, 1, 1, tzinfo=timezone.utc),
            ),
        )
        .group_by(ChatSession.agent_id)
    )
    return {str(row[0]): int(row[1] or 0) for row in result.all()}


def _serialize_agent_out(
    agent: Agent,
    unread_count: int = 0,
    *,
    creator_username: str | None = None,
    creator_display_name: str | None = None,
) -> AgentOut:
    payload = AgentOut.model_validate(agent).model_dump()
    payload["unread_count"] = unread_count
    payload["creator_username"] = creator_username
    payload["creator_display_name"] = creator_display_name
    return AgentOut.model_validate(payload)


__all__ = [name for name in globals() if not name.startswith("__")]
