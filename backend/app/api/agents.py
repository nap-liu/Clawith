"""Agent (Digital Employee) API routes."""

import hashlib
import json
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
    check_agent_access,
    is_agent_creator,
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


@router.get("/templates")
async def list_templates(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all available agent templates."""
    from app.models.agent import AgentTemplate
    result = await db.execute(
        select(AgentTemplate).order_by(AgentTemplate.is_builtin.desc(), AgentTemplate.created_at.asc())
    )
    templates = result.scalars().all()
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "description": t.description,
            "icon": t.icon,
            "category": t.category,
            "is_builtin": t.is_builtin,
            "soul_template": t.soul_template,
            "default_skills": t.default_skills,
            "default_autonomy_policy": t.default_autonomy_policy,
            "capability_bullets": t.capability_bullets or [],
            "has_bootstrap": bool(t.bootstrap_content),
        }
        for t in templates
    ]


async def _agent_to_out(
    db: AsyncSession,
    agent: Agent,
    viewer_id: uuid.UUID,
) -> AgentOut:
    """Serialize one agent with ``onboarded_for_me`` for the given viewer."""
    from app.services.onboarding import is_onboarded
    model = AgentOut.model_validate(agent)
    model.onboarded_for_me = await is_onboarded(db, agent.id, viewer_id)
    return model


async def _agents_to_out(
    db: AsyncSession,
    agents: list[Agent],
    viewer_id: uuid.UUID,
) -> list[AgentOut]:
    """List variant that fetches all junction rows in one query."""
    from app.services.onboarding import onboarded_agent_ids
    onboarded = await onboarded_agent_ids(db, viewer_id, [a.id for a in agents])
    out: list[AgentOut] = []
    for a in agents:
        model = AgentOut.model_validate(a)
        model.onboarded_for_me = a.id in onboarded
        out.append(model)
    return out


@router.get("/", response_model=list[AgentOut])
async def list_agents(
    tenant_id: uuid.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all agents the current user has access to."""
    if tenant_id and tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only list agents in your own company",
        )

    requested_tenant_id = current_user.tenant_id

    stmt = build_visible_agents_query(
        current_user,
        tenant_id=requested_tenant_id,
    ).order_by(Agent.created_at.desc())

    result = await db.execute(stmt)
    agents = result.scalars().all()
    # Lazy reset token counters
    needs_flush = False
    for a in agents:
        if await _lazy_reset_token_counters(a, db):
            needs_flush = True
    if needs_flush:
        await db.commit()
    unread_by_agent = await _build_unread_count_by_agent(db, agents, current_user)
    creator_ids = {a.creator_id for a in agents if a.creator_id}
    creators_by_id: dict[uuid.UUID, User] = {}
    if creator_ids:
        from sqlalchemy.orm import selectinload

        creator_rows = await db.execute(
            select(User)
            .where(User.id.in_(creator_ids))
            .options(selectinload(User.identity))
        )
        creators_by_id = {creator.id: creator for creator in creator_rows.scalars().all()}
    from app.services.onboarding import onboarded_agent_ids
    onboarded = await onboarded_agent_ids(db, current_user.id, [a.id for a in agents])
    out: list[AgentOut] = []
    for a in agents:
        creator = creators_by_id.get(a.creator_id)
        model = _serialize_agent_out(
            a,
            unread_by_agent.get(str(a.id), 0),
            creator_username=creator.username if creator else None,
            creator_display_name=creator.display_name if creator else None,
        )
        model.onboarded_for_me = a.id in onboarded
        out.append(model)
    return out


@router.get("/explore", response_model=AgentExplorePageOut)
async def explore_agents(
    tenant_id: uuid.UUID | None = None,
    search: str | None = Query(default=None, max_length=120),
    agent_status: str | None = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the lightweight, paginated Agent directory used by Explore."""
    if tenant_id and tenant_id != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only list agents in your own company",
        )
    if agent_status not in (None, "running", "idle", "stopped", "creating", "error"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid agent status",
        )

    visible = build_visible_agents_query(
        current_user,
        tenant_id=current_user.tenant_id,
    ).with_only_columns(
        Agent.id,
        Agent.name,
        Agent.avatar_url,
        Agent.role_description,
        Agent.bio,
        Agent.status,
        Agent.creator_id,
        Agent.agent_type,
        Agent.openclaw_last_seen,
        Agent.created_at,
        Agent.last_active_at,
    ).subquery("explore_visible_agents")

    counts_result = await db.execute(
        select(
            func.count(visible.c.id),
            func.coalesce(func.sum(case((visible.c.status == "running", 1), else_=0)), 0),
            func.coalesce(func.sum(case((visible.c.status == "idle", 1), else_=0)), 0),
            func.coalesce(func.sum(case((visible.c.status == "stopped", 1), else_=0)), 0),
        ).select_from(visible)
    )
    all_count, running_count, idle_count, stopped_count = counts_result.one()

    filters = []
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                visible.c.name.ilike(pattern),
                visible.c.role_description.ilike(pattern),
                visible.c.bio.ilike(pattern),
            )
        )
    if agent_status:
        filters.append(visible.c.status == agent_status)

    filtered = select(visible).where(*filters).subquery("explore_filtered_agents")
    total_result = await db.execute(select(func.count()).select_from(filtered))
    total = int(total_result.scalar_one() or 0)

    creator = aliased(User)
    creator_identity = aliased(Identity)
    status_order = case(
        (filtered.c.status == "running", 0),
        (filtered.c.status == "idle", 1),
        (filtered.c.status == "creating", 2),
        (filtered.c.status == "stopped", 3),
        (filtered.c.status == "error", 4),
        else_=5,
    )
    rows_result = await db.execute(
        select(
            filtered.c.id,
            filtered.c.name,
            filtered.c.avatar_url,
            filtered.c.role_description,
            filtered.c.bio,
            filtered.c.status,
            filtered.c.creator_id,
            creator.display_name.label("creator_display_name"),
            creator_identity.username.label("creator_username"),
            filtered.c.agent_type,
            filtered.c.openclaw_last_seen,
            filtered.c.created_at,
            filtered.c.last_active_at,
        )
        .select_from(filtered)
        .outerjoin(creator, creator.id == filtered.c.creator_id)
        .outerjoin(creator_identity, creator_identity.id == creator.identity_id)
        .order_by(status_order.asc(), filtered.c.last_active_at.desc().nullslast(), filtered.c.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    rows = [dict(row) for row in rows_result.mappings().all()]
    unread_by_agent = await _build_unread_count_by_agent_ids(
        db,
        [row["id"] for row in rows],
        current_user,
    )
    for row in rows:
        row["unread_count"] = unread_by_agent.get(str(row["id"]), 0)

    return {
        "items": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
        "counts": {
            "all": int(all_count or 0),
            "running": int(running_count or 0),
            "idle": int(idle_count or 0),
            "stopped": int(stopped_count or 0),
        },
    }


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_agent(
    data: AgentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new digital employee (any authenticated user)."""
    from app.services.agent_provisioning import AgentProvisionInput, provision_agent
    from app.services.quota_guard import QuotaExceeded

    # Admins may target another tenant; everyone else uses their own.
    target_tenant_id = current_user.tenant_id
    if current_user.role in ("platform_admin", "org_admin") and data.tenant_id:
        target_tenant_id = data.tenant_id

    inp = AgentProvisionInput(
        name=data.name,
        agent_type=data.agent_type or "native",
        role_description=data.role_description,
        bio=data.bio,
        avatar_url=data.avatar_url,
        personality=data.personality,
        boundaries=data.boundaries,
        primary_model_id=data.primary_model_id,
        fallback_model_id=data.fallback_model_id,
        permission_scope_type=data.permission_scope_type,
        permission_scope_ids=data.permission_scope_ids,
        permission_access_level=data.permission_access_level,
        autonomy_policy=data.autonomy_policy,
        max_tokens_per_day=data.max_tokens_per_day,
        max_tokens_per_month=data.max_tokens_per_month,
        template_id=data.template_id,
        skill_ids=data.skill_ids,
    )
    try:
        agent, raw_key = await provision_agent(
            db, creator=current_user, tenant_id=target_tenant_id, data=inp
        )
    except QuotaExceeded as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=e.message)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    if agent.agent_type == "openclaw":
        out = (await _agent_to_out(db, agent, current_user.id)).model_dump()
        out["api_key"] = raw_key
        return out
    return await _agent_to_out(db, agent, current_user.id)


@router.get("/{agent_id}")
async def get_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get agent details."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    # Lazy reset token counters
    if await _lazy_reset_token_counters(agent, db):
        await db.commit()
    out_model = await _agent_to_out(db, agent, current_user.id)
    out = out_model.model_dump()
    out["access_level"] = access_level
    from app.services.scene_service import scene_tool_enabled

    out["scene_config_enabled"] = await scene_tool_enabled(db, agent_id)

    # Resolve creator username (one extra query, only on detail page).
    # IMPORTANT: User.username is an association_proxy to User.identity.username.
    # We must eagerly load the identity relationship (selectinload) to avoid
    # async lazy-loading errors (SQLAlchemy raises MissingGreenlet in async context).
    if agent.creator_id:
        from sqlalchemy.orm import selectinload

        from app.models.user import Identity  # noqa: F401
        creator_result = await db.execute(
            select(User)
            .where(User.id == agent.creator_id)
            .options(selectinload(User.identity))
        )
        creator = creator_result.scalar_one_or_none()
        out["creator_username"] = creator.username if creator else None
        out["creator_display_name"] = creator.display_name if creator else None

    # Resolve effective timezone (agent → tenant → UTC)
    effective_tz = agent.timezone
    if not effective_tz and agent.tenant_id:
        from app.models.tenant import Tenant
        t_result = await db.execute(select(Tenant).where(Tenant.id == agent.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant:
            effective_tz = tenant.timezone or "UTC"
    out["effective_timezone"] = effective_tz or "UTC"

    return out


@router.get("/{agent_id}/permissions")
async def get_agent_permissions(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get agent permission scope."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    result = await db.execute(select(AgentPermission).where(AgentPermission.agent_id == agent_id))
    perms = result.scalars().all()
    can_manage = access_level == "manage"
    is_owner = is_agent_creator(current_user, agent)
    access_mode = getattr(agent, "access_mode", None) or "company"

    if not perms:
        return {
            "scope_type": access_mode,
            "scope_ids": [],
            "user_access": [],
            "department_access": [],
            "access_level": "manage" if is_owner else "use",
            "effective_access_level": access_level,
            "can_manage": can_manage,
            "is_owner": is_owner,
            "creator_id": str(agent.creator_id) if agent.creator_id else None,
        }

    scope_type = access_mode
    scope_ids = [str(p.scope_id) for p in perms if p.scope_type == "user" and p.scope_id]
    department_perms = [
        p for p in perms if p.scope_type == "department" and p.scope_id
    ]
    perm_access_level = getattr(agent, "company_access_level", None) or next(
        (p.access_level for p in perms if p.scope_type == "company"),
        "use",
    )

    # Resolve names for display
    scope_names = []
    user_access = []
    department_access = []
    if department_perms:
        departments_result = await db.execute(
            select(OrgDepartment)
            .where(
                OrgDepartment.id.in_([p.scope_id for p in department_perms]),
                OrgDepartment.tenant_id == agent.tenant_id,
                OrgDepartment.status == "active",
            )
            .order_by(OrgDepartment.path.asc())
        )
        departments_by_id = {
            department.id: department
            for department in departments_result.scalars().all()
        }
        for perm in department_perms:
            department = departments_by_id.get(perm.scope_id)
            if department:
                department_access.append(
                    {
                        "id": str(department.id),
                        "name": department.name,
                        "path": department.path,
                        "access_level": perm.access_level or "use",
                        "include_descendants": True,
                    }
                )
    display_user_ids = {uuid.UUID(sid) for sid in scope_ids}
    if access_mode == "custom":
        if agent.creator_id:
            display_user_ids.add(agent.creator_id)
        display_user_ids.update(admin.id for admin in await _get_active_admin_users(db, agent.tenant_id))

    if display_user_ids:
        users_result = await db.execute(
            select(User).where(
                User.id.in_(display_user_ids),
                User.tenant_id == agent.tenant_id,
            )
        )
        users_by_id = {str(u.id): u for u in users_result.scalars().all()}
        canonical_members = canonical_org_member_id_subquery(
            tenant_id=agent.tenant_id,
            prefer_directory_profile=True,
        )
        members_result = await db.execute(
            select(OrgMember)
            .join(
                canonical_members,
                and_(OrgMember.id == canonical_members.c.om_id, canonical_members.c.rn == 1),
            )
            .where(OrgMember.user_id.in_(display_user_ids))
        )
        members_by_user_id = {
            str(member.user_id): member
            for member in members_result.scalars().all()
            if member.user_id
        }
        access_by_user_id = {
            str(perm.scope_id): (perm.access_level or "use")
            for perm in perms
            if perm.scope_type == "user" and perm.scope_id
        }
        ordered_user_ids = [str(uid) for uid in display_user_ids]
        ordered_user_ids.sort(key=lambda sid: (users_by_id.get(sid).display_name or users_by_id.get(sid).username or "") if users_by_id.get(sid) else "")
        for perm in perms:
            if perm.scope_type != "user" or not perm.scope_id:
                continue
            sid = str(perm.scope_id)
            if sid not in ordered_user_ids:
                ordered_user_ids.append(sid)

        for sid in ordered_user_ids:
            u = users_by_id.get(sid)
            if not u:
                continue
            member = members_by_user_id.get(sid)
            is_creator = agent.creator_id == u.id
            is_admin = u.role in ("platform_admin", "org_admin")
            is_required = access_mode == "custom" and (is_creator or is_admin)
            item = {
                "id": sid,
                "name": u.display_name or u.username,
                "username": u.username,
                "email": u.email,
                "title": member.title if member else u.title,
                "avatar_url": member.avatar_url if member else u.avatar_url,
                "department_path": member.department_path if member else "",
                "role": u.role,
                "access_level": "manage" if is_required else access_by_user_id.get(sid, "use"),
                "is_required": is_required,
                "required_reason": "creator" if is_creator else "company_admin" if is_admin else None,
            }
            scope_names.append({"id": sid, "name": item["name"]})
            user_access.append(item)

    return {
        "scope_type": scope_type,
        "scope_ids": scope_ids,
        "scope_names": scope_names,
        "user_access": user_access,
        "department_access": department_access,
        "access_level": perm_access_level,
        "effective_access_level": access_level,
        "can_manage": can_manage,
        "is_owner": is_owner,
        "creator_id": str(agent.creator_id) if agent.creator_id else None,
    }


@router.put("/{agent_id}/permissions")
async def update_agent_permissions(
    agent_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update agent permission scope (owner or platform_admin only)."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can change permissions")

    scope_type = data.get("scope_type", "company")
    scope_ids = data.get("scope_ids", [])
    user_access = data.get("user_access", [])
    department_access = data.get("department_access", [])
    access_level = data.get("access_level", "use")
    if access_level not in ("use", "manage"):
        access_level = "use"
    if scope_type not in ("company", "user", "private", "custom"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported scope_type")
    if scope_type == "user":
        scope_type = "private"

    valid_user_ids: set[uuid.UUID] = set()
    if scope_type == "custom":
        try:
            requested_user_ids = {
                uuid.UUID(str(item.get("id") or item.get("user_id")))
                for item in user_access
                if item.get("id") or item.get("user_id")
            }
            requested_user_ids.update(uuid.UUID(str(scope_id)) for scope_id in scope_ids)
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid user id",
            ) from exc
        if requested_user_ids:
            users_result = await db.execute(
                select(User.id).where(
                    User.id.in_(requested_user_ids),
                    User.tenant_id == agent.tenant_id,
                    User.is_active == True,  # noqa: E712
                )
            )
            valid_user_ids = set(users_result.scalars().all())
            if valid_user_ids != requested_user_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="User not found in this organization",
                )

    valid_departments: dict[uuid.UUID, OrgDepartment] = {}
    if scope_type == "custom" and department_access:
        try:
            requested_department_ids = {
                uuid.UUID(str(item.get("id") or item.get("department_id")))
                for item in department_access
                if item.get("id") or item.get("department_id")
            }
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid department id",
            ) from exc
        departments_result = await db.execute(
            select(OrgDepartment).where(
                OrgDepartment.id.in_(requested_department_ids),
                OrgDepartment.tenant_id == agent.tenant_id,
                OrgDepartment.status == "active",
            )
        )
        valid_departments = {
            department.id: department
            for department in departments_result.scalars().all()
        }
        if set(valid_departments) != requested_department_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Department not found in this organization",
            )

    # Delete existing permissions
    from sqlalchemy import delete as sql_delete
    await db.execute(sql_delete(AgentPermission).where(AgentPermission.agent_id == agent_id))

    # Insert new permissions
    if scope_type == "company":
        agent.access_mode = "company"
        agent.company_access_level = access_level
        db.add(AgentPermission(agent_id=agent_id, scope_type="company", access_level=access_level))
    elif scope_type == "private":
        agent.access_mode = "private"
        agent.company_access_level = access_level
        # "Only me" means private to the agent creator, even when an org admin
        # is managing a company-visible agent created by someone else.
        db.add(AgentPermission(agent_id=agent_id, scope_type="user", scope_id=agent.creator_id or current_user.id, access_level="manage"))
    elif scope_type == "custom":
        agent.access_mode = "custom"
        agent.company_access_level = access_level
        seen_user_ids: set[uuid.UUID] = set()
        creator_id = agent.creator_id or current_user.id
        required_manager_ids = {creator_id}
        required_manager_ids.update(admin.id for admin in await _get_active_admin_users(db, agent.tenant_id))
        for item in user_access:
            sid = item.get("id") or item.get("user_id")
            if not sid:
                continue
            uid = uuid.UUID(str(sid))
            if uid in seen_user_ids:
                continue
            lvl = item.get("access_level", "use")
            if lvl not in ("use", "manage"):
                lvl = "use"
            if uid in required_manager_ids:
                lvl = "manage"
            seen_user_ids.add(uid)
            db.add(AgentPermission(agent_id=agent_id, scope_type="user", scope_id=uid, access_level=lvl))
        for sid in scope_ids:
            uid = uuid.UUID(str(sid))
            if uid not in seen_user_ids:
                seen_user_ids.add(uid)
                db.add(AgentPermission(
                    agent_id=agent_id,
                    scope_type="user",
                    scope_id=uid,
                    access_level="manage" if uid in required_manager_ids else access_level,
                ))
        seen_department_ids: set[uuid.UUID] = set()
        for item in department_access:
            sid = item.get("id") or item.get("department_id")
            if not sid:
                continue
            department_id = uuid.UUID(str(sid))
            if department_id in seen_department_ids or department_id not in valid_departments:
                continue
            level = item.get("access_level", "use")
            if level not in ("use", "manage"):
                level = "use"
            seen_department_ids.add(department_id)
            db.add(
                AgentPermission(
                    agent_id=agent_id,
                    scope_type="department",
                    scope_id=department_id,
                    access_level=level,
                )
            )
        for uid in required_manager_ids:
            if uid not in seen_user_ids:
                db.add(AgentPermission(agent_id=agent_id, scope_type="user", scope_id=uid, access_level="manage"))

    await db.flush()
    relationships_changed = await ensure_access_granted_platform_relationships(
        db,
        agent,
        created_by_user_id=current_user.id,
    )
    if relationships_changed:
        from app.api.relationships import _regenerate_relationships_file
        await _regenerate_relationships_file(db, agent_id)

    await db.commit()
    return {"status": "ok"}


@router.get("/{agent_id}/permissions/directory/departments")
async def get_agent_permission_departments(
    agent_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return a lazy-loadable, tenant-scoped department directory."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can change permissions")
    return await permission_directory_departments(
        db,
        tenant_id=agent.tenant_id,
        current_user_id=current_user.id,
        parent_id=parent_id,
        search=search,
        limit=limit,
    )


@router.get("/{agent_id}/permissions/directory/members")
async def get_agent_permission_members(
    agent_id: uuid.UUID,
    department_id: uuid.UUID | None = None,
    include_descendants: bool = False,
    execution_assignable: bool = False,
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return canonical platform users for the permission picker."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can change permissions")
    if not agent.tenant_id:
        return {"items": [], "page": page, "page_size": page_size, "total": 0, "has_more": False}

    if execution_assignable:
        accessible_user_ids = build_agent_accessible_user_ids_query(agent).subquery()
        candidate_filters = [
            User.tenant_id == agent.tenant_id,
            User.is_active == True,  # noqa: E712
            or_(
                User.id.in_(select(accessible_user_ids.c.id)),
                User.role == "platform_admin",
                Identity.is_platform_admin == True,  # noqa: E712
            ),
        ]
        normalized_search = (search or "").strip()
        if normalized_search:
            pattern = f"%{normalized_search}%"
            candidate_filters.append(
                or_(
                    User.display_name.ilike(pattern),
                    User.identity.has(
                        or_(Identity.email.ilike(pattern), Identity.username.ilike(pattern))
                    ),
                )
            )
        elif department_id:
            department_result = await db.execute(
                select(OrgDepartment).where(
                    OrgDepartment.id == department_id,
                    OrgDepartment.tenant_id == agent.tenant_id,
                    OrgDepartment.status == "active",
                )
            )
            department = department_result.scalar_one_or_none()
            if not department:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")
            department_ids = [department.id]
            if include_descendants:
                subtree = department_subtree_cte(
                    tenant_id=agent.tenant_id,
                    department_id=department.id,
                    name="execution_picker_department_subtree",
                )
                department_ids = select(subtree.c.department_id)
            department_user_ids = select(OrgMember.user_id).where(
                OrgMember.tenant_id == agent.tenant_id,
                OrgMember.status == "active",
                OrgMember.department_id.in_(department_ids),
                OrgMember.user_id.is_not(None),
            )
            candidate_filters.append(User.id.in_(department_user_ids))

        candidate_query = select(User).outerjoin(Identity, Identity.id == User.identity_id).where(*candidate_filters)
        count_result = await db.execute(
            select(func.count()).select_from(candidate_query.subquery())
        )
        total = int(count_result.scalar_one() or 0)
        candidates_result = await db.execute(
            candidate_query
            .options(selectinload(User.identity))
            .order_by(User.display_name.asc(), User.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        candidates = candidates_result.scalars().all()

        profile_map: dict[uuid.UUID, OrgMember] = {}
        if candidates:
            profile_result = await db.execute(
                select(OrgMember)
                .where(
                    OrgMember.tenant_id == agent.tenant_id,
                    OrgMember.status == "active",
                    OrgMember.user_id.in_([candidate.id for candidate in candidates]),
                )
                .order_by(OrgMember.user_id.asc(), OrgMember.id.asc())
            )
            for profile in profile_result.scalars().all():
                if profile.user_id and profile.user_id not in profile_map:
                    profile_map[profile.user_id] = profile

        return {
            "items": [
                {
                    "id": str(candidate.id),
                    "member_id": str(profile.id) if (profile := profile_map.get(candidate.id)) else None,
                    "name": candidate.display_name,
                    "nickname": profile.nickname if profile else None,
                    "department_id": str(profile.department_id) if profile and profile.department_id else None,
                    "department_path": (profile.department_path or "") if profile else "",
                    "title": (profile.title or candidate.title or "") if profile else (candidate.title or ""),
                    "avatar_url": (profile.avatar_url if profile else None) or candidate.avatar_url,
                    "email": candidate.email,
                }
                for candidate in candidates
            ],
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": page * page_size < total,
        }

    return await permission_directory_members(
        db,
        tenant_id=agent.tenant_id,
        department_id=department_id,
        include_descendants=include_descendants,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.get("/{agent_id}/permissions/candidates")
async def get_agent_permission_candidates(
    agent_id: uuid.UUID,
    search: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return org members that can be granted custom access.

    For members without a linked platform account (user_id is None), we call
    get_platform_user_by_org_member which will find-or-create a User using the
    member's email/phone, then link it back to the OrgMember row.
    """
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can change permissions")

    member_query = select(OrgMember).where(
        OrgMember.tenant_id == agent.tenant_id,
        OrgMember.status == "active",
    )
    if search:
        pattern = f"%{search}%"
        member_query = member_query.where(
            OrgMember.name.ilike(pattern) |
            OrgMember.nickname.ilike(pattern) |
            OrgMember.email.ilike(pattern) |
            OrgMember.name_translit_full.ilike(pattern) |
            OrgMember.name_translit_initial.ilike(pattern)
        )

    members_result = await db.execute(member_query.order_by(OrgMember.name.asc()).limit(50))
    members = members_result.scalars().all()

    # For members already linked, batch-load User rows for display info.
    linked_user_ids = [m.user_id for m in members if m.user_id]
    users_by_id: dict[uuid.UUID, User] = {}
    if linked_user_ids:
        users_result = await db.execute(
            select(User)
            .where(User.id.in_(linked_user_ids), User.tenant_id == agent.tenant_id)
            .options(selectinload(User.identity))
        )
        users_by_id = {u.id: u for u in users_result.scalars().all()}

    from app.services.channel_user_service import get_platform_user_by_org_member

    candidates = []
    for m in members:
        if m.user_id:
            u = users_by_id.get(m.user_id)
        else:
            # No platform account yet — find-or-create one from OrgMember info
            # and link it back so future lookups hit Case 1.
            try:
                u = await get_platform_user_by_org_member(
                    db, m, agent_tenant_id=agent.tenant_id
                )
            except Exception:
                # If user creation fails for any reason, skip this member
                continue

        if u is None:
            continue

        candidates.append({
            "id": str(u.id),  # always a valid User.id
            "name": m.name,
            "nickname": m.nickname,
            "username": u.username if u else None,
            "email": m.email or (u.email if u else None),
            "title": m.title or None,
            "avatar_url": m.avatar_url or None,
        })

    await db.commit()

    return {
        "users": candidates,
        "agents": [],
    }


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: uuid.UUID,
    data: AgentUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update agent settings (creator or admin)."""
    agent, _access = await check_agent_access(db, current_user, agent_id)

    is_admin = current_user.role in ("platform_admin", "org_admin")

    if not is_agent_creator(current_user, agent) and not is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can update agent settings")

    update_data = data.model_dump(exclude_unset=True)

    # expires_at: admin only
    if "expires_at" in update_data:
        if not is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admin can modify agent expiry time")
        from datetime import datetime
        from datetime import timezone as tz
        new_expires = update_data["expires_at"]
        # Allow any value: extend, shorten, or null (permanent).
        # Re-activate the agent if new expiry is in the future or cleared.
        if new_expires is None or new_expires > datetime.now(tz.utc):
            if agent.is_expired:
                agent.is_expired = False
                agent.status = "idle"

    # Enforce heartbeat floor from tenant
    clamped_fields = []  # track fields adjusted by tenant floor
    if "heartbeat_interval_minutes" in update_data and current_user.tenant_id:
        from app.models.tenant import Tenant
        t_result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant and update_data["heartbeat_interval_minutes"] < tenant.min_heartbeat_interval_minutes:
            update_data["heartbeat_interval_minutes"] = tenant.min_heartbeat_interval_minutes
            clamped_fields.append({
                "field": "heartbeat_interval_minutes",
                "requested": update_data["heartbeat_interval_minutes"],
                "applied": tenant.min_heartbeat_interval_minutes,
                "reason": "company_floor",
            })

    # Enforce trigger limit floors from tenant
    trigger_fields = {"min_poll_interval_min", "webhook_rate_limit", "max_triggers"}
    if trigger_fields & set(update_data.keys()) and current_user.tenant_id:
        from app.models.tenant import Tenant
        t_result = await db.execute(select(Tenant).where(Tenant.id == current_user.tenant_id))
        tenant = t_result.scalar_one_or_none()
        if tenant:
            if "min_poll_interval_min" in update_data:
                original = update_data["min_poll_interval_min"]
                update_data["min_poll_interval_min"] = max(original, tenant.min_poll_interval_floor)
                if update_data["min_poll_interval_min"] != original:
                    clamped_fields.append({
                        "field": "min_poll_interval_min",
                        "requested": original,
                        "applied": update_data["min_poll_interval_min"],
                        "reason": "company_floor",
                    })
            if "webhook_rate_limit" in update_data:
                original = update_data["webhook_rate_limit"]
                update_data["webhook_rate_limit"] = min(original, tenant.max_webhook_rate_ceiling)
                if update_data["webhook_rate_limit"] != original:
                    clamped_fields.append({
                        "field": "webhook_rate_limit",
                        "requested": original,
                        "applied": update_data["webhook_rate_limit"],
                        "reason": "company_ceiling",
                    })

    for field, value in update_data.items():
        setattr(agent, field, value)
    await db.flush()

    # Sync Participant display_name / avatar if changed
    if "name" in update_data or "avatar_url" in update_data:
        from app.models.participant import Participant
        p_r = await db.execute(select(Participant).where(Participant.type == "agent", Participant.ref_id == agent_id))
        p = p_r.scalar_one_or_none()
        if p:
            if "name" in update_data:
                p.display_name = agent.name
            if "avatar_url" in update_data:
                p.avatar_url = agent.avatar_url
            await db.flush()

    out_model = await _agent_to_out(db, agent, current_user.id)
    out = out_model.model_dump()
    if clamped_fields:
        out["_clamped_fields"] = clamped_fields
    return out


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a digital employee (creator only)."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent) and current_user.role not in ("super_admin", "org_admin", "platform_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can delete agent")

    # System agents (OKR Agent, etc.) cannot be deleted — they are seeded by the
    # platform and required for core features. Disable them via settings instead.
    if agent.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="System agents cannot be deleted. Disable the related feature (e.g. OKR) in Company Settings instead.",
        )

    parent_session = aliased(ChatSession)
    child_session = aliased(ChatSession)
    subagent_audit = (
        await db.execute(
            select(SubagentRun.id)
            .join(parent_session, parent_session.id == SubagentRun.parent_session_id)
            .join(child_session, child_session.id == SubagentRun.id)
            .where(
                or_(
                    parent_session.agent_id == agent_id,
                    parent_session.peer_agent_id == agent_id,
                    child_session.agent_id == agent_id,
                    child_session.peer_agent_id == agent_id,
                )
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if subagent_audit is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="被 Subagent 审计记录引用的数字员工不能删除。",
        )

    # Stop container and archive files (best effort)
    from app.services.agent_manager import agent_manager
    archive_dir: Path | None = None
    try:
        await agent_manager.remove_container(agent)
    except Exception:
        pass
    try:
        archive_dir = await agent_manager.archive_agent_files(agent.id)
    except Exception:
        pass
    if archive_dir is not None:
        try:
            await _archive_agent_task_history(db, agent.id, archive_dir)
        except Exception:
            pass

    # Delete related records that reference this agent
    # Use savepoints so a failure in one table doesn't poison the whole transaction
    from sqlalchemy import text

    cleanup_tables = [
        "agent_activity_logs",
        "audit_logs",
        "approval_requests",
        "chat_messages",
        "chat_sessions",
        "agent_schedules",
        "agent_triggers",
        "dingtalk_channel_provisioning_sessions",
        "channel_configs",
        "agent_permissions",
        "agent_tools",
        "agent_relationships",
        "gateway_messages",
        "published_pages",
        "notifications",
        "daily_token_usage",
    ]

    for table in cleanup_tables:
        try:
            async with db.begin_nested():
                await db.execute(text(f"DELETE FROM {table} WHERE agent_id = :aid"), {"aid": agent_id})
        except Exception:
            pass

    # Clean up secondary FK columns that also reference agents table
    secondary_fk_cleanups = [
        "DELETE FROM task_logs WHERE task_id IN (SELECT id FROM tasks WHERE agent_id = :aid)",
        "DELETE FROM tasks WHERE agent_id = :aid",
        "DELETE FROM chat_sessions WHERE peer_agent_id = :aid",
        "DELETE FROM gateway_messages WHERE sender_agent_id = :aid",
        "UPDATE chat_messages SET sender_agent_id = NULL WHERE sender_agent_id = :aid",
    ]
    for sql in secondary_fk_cleanups:
        try:
            async with db.begin_nested():
                await db.execute(text(sql), {"aid": agent_id})
        except Exception:
            pass

    # Also clean agent_agent_relationships (has both agent_id and target_agent_id)
    try:
        async with db.begin_nested():
            await db.execute(
                text("DELETE FROM agent_agent_relationships WHERE agent_id = :aid OR target_agent_id = :aid"),
                {"aid": agent_id},
            )
    except Exception:
        pass

    # Also clear plaza posts by this agent
    try:
        async with db.begin_nested():
            await db.execute(text("DELETE FROM plaza_posts WHERE author_id = :aid"), {"aid": str(agent_id)})
    except Exception:
        pass

    # Clean up Participant identity
    try:
        async with db.begin_nested():
            await db.execute(
                text("DELETE FROM participants WHERE type = 'agent' AND ref_id = :aid"),
                {"aid": agent_id},
            )
    except Exception:
        pass

    await db.delete(agent)
    await db.commit()


@router.post("/{agent_id}/start", response_model=AgentOut)
async def start_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Start an agent's container."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can start agent")

    from app.services.agent_manager import agent_manager
    await agent_manager.start_container(db, agent)
    await db.flush()
    return await _agent_to_out(db, agent, current_user.id)


@router.post("/{agent_id}/stop", response_model=AgentOut)
async def stop_agent(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop an agent's container."""
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only manager can stop agent")

    from app.services.agent_manager import agent_manager
    await agent_manager.stop_container(agent)
    await db.flush()
    return await _agent_to_out(db, agent, current_user.id)


# ─── Agent-Level Approvals ──────────────────────────────


@router.get("/{agent_id}/approvals")
async def list_agent_approvals(
    agent_id: uuid.UUID,
    status_filter: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List approval requests for a specific agent. Only creator or admin can view."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent) and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only agent creator or admin can view approvals")

    from app.models.audit import ApprovalRequest
    query = select(ApprovalRequest).where(ApprovalRequest.agent_id == agent_id)
    if status_filter:
        query = query.where(ApprovalRequest.status == status_filter)
    query = query.order_by(ApprovalRequest.created_at.desc())
    result = await db.execute(query)
    approvals = result.scalars().all()

    return [
        {
            "id": str(a.id),
            "agent_id": str(a.agent_id),
            "action_type": a.action_type,
            "details": a.details,
            "status": a.status,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
            "resolved_by": str(a.resolved_by) if a.resolved_by else None,
        }
        for a in approvals
    ]


@router.post("/{agent_id}/approvals/{approval_id}/resolve")
async def resolve_agent_approval(
    agent_id: uuid.UUID,
    approval_id: uuid.UUID,
    data: dict,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Approve or reject a pending approval for a specific agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)

    from app.services.autonomy_service import autonomy_service
    action = data.get("action", "reject")
    try:
        approval = await autonomy_service.resolve_approval(db, approval_id, current_user, action)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await db.commit()
    return {
        "id": str(approval.id),
        "status": approval.status,
        "resolved_at": approval.resolved_at.isoformat() if approval.resolved_at else None,
    }


# ─── OpenClaw API Key Management ────────────────────────


@router.post("/{agent_id}/api-key")
async def generate_or_reset_api_key(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate or regenerate API key for an OpenClaw agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)
    if not is_agent_creator(current_user, agent) and current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only creator or admin can manage API keys")
    if getattr(agent, "agent_type", "native") != "openclaw":
        raise HTTPException(status_code=400, detail="API keys are only available for OpenClaw agents")

    raw_key = f"oc-{secrets.token_urlsafe(32)}"
    agent.api_key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    await db.commit()

    return {"api_key": raw_key, "message": "Key configured successfully."}


@router.get("/{agent_id}/gateway-messages")
async def list_gateway_messages(
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List recent gateway messages for an OpenClaw agent."""
    agent, _access = await check_agent_access(db, current_user, agent_id)

    from app.models.gateway_message import GatewayMessage
    result = await db.execute(
        select(GatewayMessage)
        .where(GatewayMessage.agent_id == agent_id)
        .order_by(GatewayMessage.created_at.desc())
        .limit(50)
    )
    messages = result.scalars().all()

    out = []
    for m in messages:
        sender_name = None
        if m.sender_agent_id:
            r = await db.execute(select(Agent.name).where(Agent.id == m.sender_agent_id))
            sender_name = r.scalar_one_or_none()
        out.append({
            "id": str(m.id),
            "sender_agent_name": sender_name,
            "content": m.content,
            "status": m.status,
            "result": m.result,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "delivered_at": m.delivered_at.isoformat() if m.delivered_at else None,
            "completed_at": m.completed_at.isoformat() if m.completed_at else None,
        })
    return out
