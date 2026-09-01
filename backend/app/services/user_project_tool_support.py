"""Parsing, access, and project lookup helpers for human project tools."""

import json
import uuid
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func, or_, select

from app.core.permissions import get_agent_access_level_for_user_id
from app.models.agent import Agent
from app.models.project import Project, ProjectAccessGrant, ProjectMemberSnapshot, ProjectWorkItem
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.activity_logger import log_activity
from app.services.execution_identity import is_human_interactive_turn
from app.services.project_agent_workspace import _is_sensitive_asset
from app.services.project_service import accessible_projects_clause, require_project
from app.services.user_project_tool_catalog import (
    _PRIVATE_CONNECTION_FILENAMES,
    _PROJECT_STATUSES,
)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _page_metadata(*, offset: int, limit: int, has_more: bool) -> dict[str, Any]:
    return {
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
    }


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a complete platform UUID") from exc


def _optional_uuid(value: Any, field: str) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    return _uuid(value, field)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _text(value: Any, field: str, *, maximum: int, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    if len(text) > maximum:
        raise ValueError(f"{field} exceeds its maximum length")
    return text


def _datetime(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _bounded_list(value: Any, field: str, maximum: int) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{field} must be an array")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds its item limit")
    return value


def _clip(value: Any, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    return f"{text[: max(0, limit - 1)]}…", True


def _safe_file_path(path: str) -> bool:
    try:
        pure = PurePosixPath(path)
    except (TypeError, ValueError):
        return False
    lowered_parts = tuple(part.casefold() for part in pure.parts)
    return bool(
        path
        and not pure.is_absolute()
        and ".." not in pure.parts
        and not _is_sensitive_asset(pure)
        and not any(part in {".mcp", ".mcp_servers"} for part in lowered_parts)
        and (not lowered_parts or lowered_parts[-1] not in _PRIVATE_CONNECTION_FILENAMES)
    )


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


async def record_user_project_tool_activity(
    *,
    agent_id: uuid.UUID,
    tool_name: str,
    outcome: str,
    user_id: uuid.UUID | None,
    session_id: str | None,
    turn_anchor_id: uuid.UUID | None,
    tool_call_id: str | None,
    project_id: Any = None,
) -> None:
    """Persist a bounded audit record without user content or credentials."""

    related_id: uuid.UUID | None = None
    try:
        related_id = uuid.UUID(str(project_id)) if project_id else None
    except (TypeError, ValueError, AttributeError):
        related_id = None
    summary = {
        "completed": "Project action completed",
        "rejected": "Project action rejected",
        "failed": "Project action failed",
    }.get(outcome, "Project action recorded")
    await log_activity(
        agent_id,
        "tool_call",
        summary,
        detail={
            "capability": "project_management",
            "tool": tool_name,
            "outcome": outcome,
            "actor_user_id": str(user_id or "") or None,
            "source_session_id": str(session_id or "") or None,
            "source_message_id": str(turn_anchor_id or "") or None,
            "tool_call_id": str(tool_call_id or "") or None,
            "project_id": str(related_id) if related_id else None,
        },
        related_id=related_id,
    )


async def _interactive_actor(
    db,
    *,
    tool_name: str,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> tuple[Agent, User]:
    agent = await db.get(Agent, agent_id)
    if agent is None or agent.is_deleted or agent.scope != "standard":
        raise ValueError("User project tools are available only to standard digital employees")

    assignment_id = await db.scalar(
        select(AgentTool.id)
        .join(Tool, Tool.id == AgentTool.tool_id)
        .where(
            AgentTool.agent_id == agent.id,
            AgentTool.enabled.is_(True),
            Tool.name == tool_name,
            Tool.enabled.is_(True),
        )
    )
    if assignment_id is None:
        raise ValueError("This project tool is not enabled for the current digital employee")

    actor_user_id = _optional_uuid(user_id, "user_id")
    actor_session_id = _optional_uuid(session_id, "session_id")
    actor_turn_anchor_id = _optional_uuid(turn_anchor_id, "turn_anchor_id")
    if actor_user_id is None or actor_session_id is None or actor_turn_anchor_id is None:
        raise ValueError("This capability requires an active user conversation")
    if not await is_human_interactive_turn(
        db,
        agent_id=agent.id,
        actor_user_id=actor_user_id,
        session_id=actor_session_id,
        turn_anchor_id=actor_turn_anchor_id,
    ):
        raise ValueError("This capability requires an active user conversation")

    actor = await db.get(User, actor_user_id)
    if (
        actor is None
        or not actor.is_active
        or actor.tenant_id is None
        or actor.tenant_id != agent.tenant_id
        or await get_agent_access_level_for_user_id(db, actor.id, agent) is None
    ):
        raise ValueError("This capability is unavailable for this conversation")
    return agent, actor


async def _project_access_role(db, project: Project, actor: User) -> str:
    if project.owner_user_id == actor.id:
        return "owner"
    role = await db.scalar(
        select(ProjectAccessGrant.role).where(
            ProjectAccessGrant.project_id == project.id,
            ProjectAccessGrant.tenant_id == project.tenant_id,
            ProjectAccessGrant.user_id == actor.id,
        )
    )
    return str(role or "view")


async def _search_projects(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    scope = str(arguments.get("scope") or "all")
    query = str(arguments.get("query") or "").strip()[:200]
    status = str(arguments.get("status") or "").strip()
    if scope not in {"all", "mine", "shared", "running", "archived"}:
        raise ValueError("Unknown project search scope")
    if status and status not in _PROJECT_STATUSES:
        raise ValueError("Unknown project status")
    offset = _bounded_int(arguments.get("offset"), default=0, minimum=0, maximum=5000)
    limit = _bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=50)
    stmt = select(Project).where(accessible_projects_clause(actor))
    if scope == "mine":
        stmt = stmt.where(Project.owner_user_id == actor.id)
    elif scope == "shared":
        stmt = stmt.where(Project.owner_user_id != actor.id)
    elif scope == "running":
        stmt = stmt.where(Project.status == "running")
    elif scope == "archived":
        stmt = stmt.where(Project.status == "archived")
    if status:
        stmt = stmt.where(Project.status == status)
    elif scope != "archived":
        stmt = stmt.where(Project.status != "archived")
    if query:
        stmt = stmt.where(or_(Project.name.ilike(f"%{query}%"), Project.goal.ilike(f"%{query}%")))
    projects = (
        (await db.execute(stmt.order_by(Project.updated_at.desc(), Project.id).offset(offset).limit(limit + 1)))
        .scalars()
        .all()
    )
    items = []
    for project in projects[:limit]:
        goal, goal_truncated = _clip(project.goal, 800)
        items.append(
            {
                "id": str(project.id),
                "name": project.name,
                "description": _clip(project.description, 500)[0],
                "goal": goal,
                "goal_truncated": goal_truncated,
                "status": project.status,
                "access_role": await _project_access_role(db, project, actor),
                "created_at": _iso(project.created_at),
                "updated_at": _iso(project.updated_at),
            }
        )
    return {"items": items, **_page_metadata(offset=offset, limit=limit, has_more=len(projects) > limit)}


async def _get_project(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(db, actor, _uuid(arguments.get("project_id"), "project_id"))
    work_counts = dict(
        (
            await db.execute(
                select(ProjectWorkItem.status, func.count(ProjectWorkItem.id))
                .where(
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.tenant_id == project.tenant_id,
                )
                .group_by(ProjectWorkItem.status)
            )
        ).all()
    )
    active_members = int(
        await db.scalar(
            select(func.count(ProjectMemberSnapshot.id)).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
        or 0
    )
    settings = dict(project.settings or {})
    git_settings = dict(settings.get("git") or {})
    planning = dict(settings.get("planning") or {})
    goal, goal_truncated = _clip(project.goal, 4000)
    return {
        "id": str(project.id),
        "name": project.name,
        "description": _clip(project.description, 2000)[0],
        "goal": goal,
        "goal_truncated": goal_truncated,
        "success_criteria": [_clip(item, 1000)[0] for item in list(project.success_criteria or [])[:30]],
        "status": project.status,
        "access_role": await _project_access_role(db, project, actor),
        "work_item_counts": work_counts,
        "active_member_count": active_members,
        "current_signal": _clip(settings.get("current_signal"), 1000)[0] or None,
        "next_action": _clip(settings.get("next_action"), 1000)[0] or None,
        "planning_state": str(planning.get("state") or "") or None,
        "git_head": str(git_settings.get("head") or "") or None,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
    }
