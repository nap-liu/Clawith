"""Tenant-safe orchestration services for AI-native projects."""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.mcp_server import MCPServer
from app.models.project import (
    Project,
    ProjectAccessGrant,
    ProjectCapabilityBinding,
    ProjectEvent,
    ProjectMemberSnapshot,
    ProjectRun,
    ProjectRunMemberSnapshot,
    ProjectWorkItem,
)
from app.models.skill import Skill
from app.models.subagent_run import SubagentRun
from app.models.user import User
from app.schemas.project import ProjectCapabilityCreate, ProjectCreate, ProjectMemberCreate


def _tenant_id(user: User) -> uuid.UUID:
    if user.tenant_id is None:
        raise HTTPException(status_code=403, detail="A tenant membership is required")
    return user.tenant_id


def accessible_projects_clause(user: User, *, edit: bool = False):
    tenant_id = _tenant_id(user)
    grant = exists().where(
        ProjectAccessGrant.project_id == Project.id,
        ProjectAccessGrant.tenant_id == tenant_id,
        ProjectAccessGrant.user_id == user.id,
        *([ProjectAccessGrant.role == "edit"] if edit else []),
    )
    return and_(Project.tenant_id == tenant_id, or_(Project.owner_user_id == user.id, grant))


async def require_project(db: AsyncSession, user: User, project_id: uuid.UUID, *, edit: bool = False) -> Project:
    project = (
        await db.execute(select(Project).where(Project.id == project_id, accessible_projects_clause(user, edit=edit)))
    ).scalar_one_or_none()
    if project is None:
        # Deliberately hide existence across tenants and unauthorized users.
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def require_owner(db: AsyncSession, user: User, project_id: uuid.UUID) -> Project:
    project = (
        await db.execute(
            select(Project).where(
                Project.id == project_id,
                Project.tenant_id == _tenant_id(user),
                Project.owner_user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def ensure_project_group_session(db: AsyncSession, project: Project) -> ChatSession:
    """Return the project's single durable group root.

    The access Agent is fixed at first creation instead of following Leader
    changes. Project REST endpoints enforce ACL; this session is only the
    append-only conversation/root for durable child runs.
    """
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.project_id == project.id,
                ChatSession.source_channel == "project",
                ChatSession.is_group.is_(True),
            )
        )
    ).scalar_one_or_none()
    if session is not None:
        return session
    anchor = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            ).order_by(
                ProjectMemberSnapshot.is_leader.desc(),
                ProjectMemberSnapshot.created_at,
                ProjectMemberSnapshot.id,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if anchor is None:
        raise HTTPException(status_code=422, detail="Project needs an enabled Agent before group chat can start")
    session = ChatSession(
        project_id=project.id,
        agent_id=anchor.agent_id,
        title=f"{project.name} · Agent Group",
        source_channel="project",
        external_conv_id=f"project:{project.id}",
        is_group=True,
        group_name=project.name,
        is_primary=False,
        im_config={
            "project_id": str(project.id),
            "access_agent_id": str(anchor.agent_id),
            "append_only": True,
            "wake_policy": "structured_mentions_only",
        },
    )
    db.add(session)
    await db.flush()
    return session


async def ensure_project_leader_session(db: AsyncSession, project: Project) -> ChatSession:
    """Return the durable project-scoped planning conversation with its Leader.

    This is deliberately a normal, non-primary Web session so the existing Web
    Chat transport/history can be reused. The project owner is the canonical
    human participant; project REST remains the discovery and ACL boundary.
    """
    external_conv_id = f"project-leader:{project.id}"
    session = (
        await db.execute(
            select(ChatSession)
            .where(
                ChatSession.project_id == project.id,
                ChatSession.source_channel == "web",
                ChatSession.external_conv_id == external_conv_id,
                ChatSession.is_group.is_(False),
            )
            .order_by(ChatSession.created_at, ChatSession.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    leader = (
        await db.execute(
            select(ProjectMemberSnapshot)
            .where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.is_leader.is_(True),
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if leader is None:
        raise HTTPException(status_code=422, detail="Project needs an enabled Leader before planning can start")
    if session is not None:
        # Leader replacement before confirmation should continue the same
        # auditable project discussion rather than orphaning a second thread.
        if session.agent_id != leader.agent_id:
            session.agent_id = leader.agent_id
            session.title = f"{project.name} · Leader Planning"
            session.im_config = {
                **dict(session.im_config or {}),
                "leader_agent_id": str(leader.agent_id),
            }
        return session
    session = ChatSession(
        project_id=project.id,
        agent_id=leader.agent_id,
        user_id=project.owner_user_id,
        title=f"{project.name} · Leader Planning",
        source_channel="web",
        external_conv_id=external_conv_id,
        is_group=False,
        is_primary=False,
        im_config={
            "project_id": str(project.id),
            "project_member_id": str(leader.id),
            "leader_agent_id": str(leader.agent_id),
            "purpose": "project_kickoff_planning",
        },
    )
    db.add(session)
    await db.flush()
    return session


def add_event(
    db: AsyncSession,
    project: Project,
    event_type: str,
    summary: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_agent_id: uuid.UUID | None = None,
    from_agent_id: uuid.UUID | None = None,
    to_agent_id: uuid.UUID | None = None,
    work_item_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    metadata: dict | None = None,
) -> ProjectEvent:
    event = ProjectEvent(
        tenant_id=project.tenant_id,
        project_id=project.id,
        event_type=event_type,
        summary=summary,
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        work_item_id=work_item_id,
        run_id=run_id,
        event_metadata=metadata or {},
    )
    db.add(event)
    return event


async def _get_project_agent(db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID) -> Agent:
    agent = (
        await db.execute(
            select(Agent).where(
                Agent.id == agent_id,
                Agent.tenant_id == tenant_id,
                Agent.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=422, detail=f"Agent {agent_id} is unavailable in this tenant")
    return agent


async def add_member(
    db: AsyncSession,
    project: Project,
    data: ProjectMemberCreate,
    *,
    actor_user_id: uuid.UUID,
) -> ProjectMemberSnapshot:
    agent = await _get_project_agent(db, project.tenant_id, data.agent_id)
    existing = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.agent_id == agent.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        detail = (
            "Agent previously left this project; restore the existing member snapshot"
            if not existing.is_enabled
            else "Agent is already an active project member"
        )
        raise HTTPException(status_code=409, detail=detail)
    if not data.is_enabled:
        raise HTTPException(status_code=422, detail="New project members must start active; remove them explicitly later")
    if data.is_leader:
        await db.execute(
            ProjectMemberSnapshot.__table__.update()
            .where(ProjectMemberSnapshot.project_id == project.id)
            .values(is_leader=False)
        )
    member = ProjectMemberSnapshot(
        tenant_id=project.tenant_id,
        project_id=project.id,
        agent_id=agent.id,
        name_snapshot=agent.name,
        role_snapshot=agent.role_description or "",
        source_updated_at=agent.updated_at,
        is_leader=data.is_leader,
        is_enabled=data.is_enabled,
        config_snapshot={
            "primary_model_id": str(agent.primary_model_id) if agent.primary_model_id else None,
            "fallback_model_id": str(agent.fallback_model_id) if agent.fallback_model_id else None,
            "autonomy_policy": dict(agent.autonomy_policy or {}),
            "max_tool_rounds": agent.max_tool_rounds,
            "source_agent_status": agent.status,
            "enabled_inherited_capability_ids": [str(value) for value in data.enabled_inherited_capability_ids],
            "membership": {
                "state": "active",
                "generation": 1,
                "changed_at": datetime.now(timezone.utc).isoformat(),
                "changed_by_user_id": str(actor_user_id),
                "changed_by_agent_id": None,
                "reason": "project_member_added",
                "history": [],
            },
        },
    )
    db.add(member)
    await db.flush()
    add_event(
        db,
        project,
        "member.snapshot.created",
        f"Added project-local snapshot for {agent.name}",
        actor_user_id=actor_user_id,
        actor_agent_id=agent.id,
        metadata={"member_id": str(member.id), "is_leader": member.is_leader},
    )
    return member


def _membership_config(
    member: ProjectMemberSnapshot,
    *,
    state: str,
    actor_user_id: uuid.UUID | None,
    actor_agent_id: uuid.UUID | None,
    reason: str | None,
    revoked_subagent_ids: list[uuid.UUID] | None = None,
) -> dict:
    """Return a new immutable JSON value for one membership transition."""

    now = datetime.now(timezone.utc).isoformat()
    config = dict(member.config_snapshot or {})
    previous = dict(config.get("membership") or {})
    history = list(previous.get("history") or [])
    transition = {
        "state": state,
        "at": now,
        "actor_user_id": str(actor_user_id) if actor_user_id else None,
        "actor_agent_id": str(actor_agent_id) if actor_agent_id else None,
        "reason": (reason or "").strip() or None,
    }
    history.append(transition)
    membership = {
        **previous,
        "state": state,
        "changed_at": now,
        "changed_by_user_id": transition["actor_user_id"],
        "changed_by_agent_id": transition["actor_agent_id"],
        "reason": transition["reason"],
        "history": history,
    }
    if state == "departed":
        membership["departed_at"] = now
        membership["revoked_subagent_ids"] = [str(value) for value in (revoked_subagent_ids or [])]
    else:
        membership["restored_at"] = now
        membership["generation"] = max(1, int(previous.get("generation") or 1)) + 1
    config["membership"] = membership
    return config


async def deactivate_project_member(
    db: AsyncSession,
    project: Project,
    member: ProjectMemberSnapshot,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_agent_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> list[uuid.UUID]:
    """Soft-remove a member and revoke every live project execution.

    The snapshot, child sessions, messages, immutable run-member snapshots and
    audit history remain addressable. Only future authority is removed.
    """

    if member.project_id != project.id or member.tenant_id != project.tenant_id:
        raise HTTPException(status_code=404, detail="Project member not found")
    if member.is_leader:
        raise HTTPException(status_code=422, detail="Transfer project leadership before removing the Leader")
    if not member.is_enabled:
        return []

    all_children = (
        await db.execute(
            select(SubagentRun).where(
                SubagentRun.project_id == project.id,
                SubagentRun.project_member_id == member.id,
            )
        )
    ).scalars().all()
    live_children = [row for row in all_children if row.status in {"queued", "running"}]
    child_ids = [row.id for row in all_children]
    cancelled_child_ids = [row.id for row in live_children]
    now = datetime.now(timezone.utc)
    for child in live_children:
        child.status = "cancelled"
        child.lease_owner = None
        child.lease_expires_at = None

    if child_ids:
        sessions = (
            await db.execute(select(ChatSession).where(ChatSession.id.in_(child_ids)))
        ).scalars().all()
        for session in sessions:
            session.im_config = {
                **dict(session.im_config or {}),
                "membership_revoked": True,
                "membership_revoked_at": now.isoformat(),
                "membership_revoked_reason": "project_member_departed",
            }
        inputs = (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id.in_([str(value) for value in child_ids]),
                    ChatMessage.message_meta["kind"].as_string() == "subagent_input",
                    ChatMessage.message_meta["subagent_input_state"].as_string().in_(["pending", "processing"]),
                )
            )
        ).scalars().all()
        for row in inputs:
            metadata = dict(row.message_meta or {})
            metadata["subagent_input_state"] = "cancelled"
            metadata["turn_status"] = "cancelled"
            metadata["cancel_reason"] = "project_member_departed"
            row.message_meta = metadata

    project_runs = (
        await db.execute(
            select(ProjectRun).where(
                ProjectRun.project_id == project.id,
                ProjectRun.tenant_id == project.tenant_id,
                ProjectRun.agent_id == member.agent_id,
                ProjectRun.status.not_in(TERMINAL_PROJECT_RUN_STATUSES),
            )
        )
    ).scalars().all()
    for run in project_runs:
        run.status = "cancelled"
        run.finished_at = now
        run.error = "Project member departed before this run completed"
        run.output = {
            **dict(run.output or {}),
            "cancel_reason": "project_member_departed",
            "project_member_id": str(member.id),
        }

    member.is_enabled = False
    member.is_leader = False
    member.config_snapshot = _membership_config(
        member,
        state="departed",
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        reason=reason,
        revoked_subagent_ids=child_ids,
    )
    add_event(
        db,
        project,
        "member.departed",
        f"Removed {member.name_snapshot} from active project participation",
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        metadata={
            "member_id": str(member.id),
            "agent_id": str(member.agent_id),
            "reason": (reason or "").strip() or None,
            "revoked_subagent_ids": [str(value) for value in child_ids],
            "cancelled_subagent_ids": [str(value) for value in cancelled_child_ids],
            "cancelled_project_run_ids": [str(row.id) for row in project_runs],
            "snapshot_retained": True,
        },
    )
    await db.flush()
    return cancelled_child_ids


async def restore_project_member(
    db: AsyncSession,
    project: Project,
    member: ProjectMemberSnapshot,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_agent_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> list[uuid.UUID]:
    """Restore the same member snapshot without reopening revoked sessions."""

    if member.project_id != project.id or member.tenant_id != project.tenant_id:
        raise HTTPException(status_code=404, detail="Project member not found")
    if member.is_enabled:
        return []
    await _get_project_agent(db, project.tenant_id, member.agent_id)

    member.is_enabled = True
    member.config_snapshot = _membership_config(
        member,
        state="active",
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        reason=reason,
    )
    add_event(
        db,
        project,
        "member.restored",
        f"Restored {member.name_snapshot} to active project participation",
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        metadata={
            "member_id": str(member.id),
            "agent_id": str(member.agent_id),
            "reason": (reason or "").strip() or None,
            "restored_subagent_ids": [],
            "old_sessions_remain_read_only": True,
            "snapshot_reused": True,
        },
    )
    await db.flush()
    return []


async def project_session_access_mode(
    db: AsyncSession,
    user: User,
    session: ChatSession,
) -> str | None:
    """Return ``read``/``edit`` for an auditable project Subagent session.

    Historical sessions remain readable after departure. Writing additionally
    requires an owner/editor ACL and the exact durable member snapshot to still
    be enabled.
    """

    if session.source_channel != "subagent" or session.project_id is None or user.tenant_id is None:
        return None
    project = await db.get(Project, session.project_id)
    run = await db.get(SubagentRun, session.id)
    if (
        project is None
        or project.tenant_id != user.tenant_id
        or run is None
        or run.project_id != project.id
        or run.project_member_id is None
        or session.agent_id is None
    ):
        return None
    member = await db.get(ProjectMemberSnapshot, run.project_member_id)
    if (
        member is None
        or member.project_id != project.id
        or member.tenant_id != project.tenant_id
        or member.agent_id != session.agent_id
    ):
        return None
    member_is_writable = member.is_enabled and not bool(
        dict(session.im_config or {}).get("membership_revoked")
    )
    if project.owner_user_id == user.id:
        human_role = "edit"
    else:
        grant = (
            await db.execute(
                select(ProjectAccessGrant).where(
                    ProjectAccessGrant.project_id == project.id,
                    ProjectAccessGrant.tenant_id == project.tenant_id,
                    ProjectAccessGrant.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if grant is None:
            return None
        human_role = "edit" if grant.role == "edit" else "read"
    return "edit" if human_role == "edit" and member_is_writable else "read"


async def _resolve_capability(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    data: ProjectCapabilityCreate,
) -> str:
    if data.capability_id is None:
        if not data.capability_name:
            raise HTTPException(status_code=422, detail="capability_name is required without capability_id")
        return data.capability_name
    if data.capability_type == "skill":
        item = (
            await db.execute(
                select(Skill).where(
                    Skill.id == data.capability_id,
                    or_(Skill.tenant_id == tenant_id, Skill.tenant_id.is_(None)),
                )
            )
        ).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=422, detail="Skill is unavailable in this tenant")
        return item.name
    if data.capability_type == "mcp":
        item = (
            await db.execute(
                select(MCPServer).where(
                    MCPServer.id == data.capability_id,
                    or_(MCPServer.tenant_id == tenant_id, MCPServer.tenant_id.is_(None)),
                )
            )
        ).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=422, detail="MCP server is unavailable in this tenant")
        return item.display_name or item.name
    return data.capability_name or str(data.capability_id)


async def add_capability(
    db: AsyncSession,
    project: Project,
    data: ProjectCapabilityCreate,
    *,
    actor_user_id: uuid.UUID,
) -> ProjectCapabilityBinding:
    if data.source == "inherited":
        if data.inherited_from_agent_id is None:
            raise HTTPException(status_code=422, detail="inherited_from_agent_id is required")
        is_member = (
            await db.execute(
                select(ProjectMemberSnapshot.id).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id == data.inherited_from_agent_id,
                )
            )
        ).scalar_one_or_none()
        if is_member is None:
            raise HTTPException(status_code=422, detail="Inherited capability source must be a project member")
    name = await _resolve_capability(db, project.tenant_id, data)
    binding = ProjectCapabilityBinding(
        tenant_id=project.tenant_id,
        project_id=project.id,
        capability_type=data.capability_type,
        capability_id=data.capability_id,
        capability_name=name,
        source=data.source,
        inherited_from_agent_id=data.inherited_from_agent_id,
        is_enabled=data.is_enabled,
        scope=data.scope,
        config=data.config,
    )
    db.add(binding)
    await db.flush()
    add_event(
        db,
        project,
        "capability.bound",
        f"Bound {data.capability_type} capability {name}",
        actor_user_id=actor_user_id,
        metadata={"binding_id": str(binding.id), "source": binding.source},
    )
    return binding


async def replace_access_grants(
    db: AsyncSession,
    project: Project,
    user_ids: list[uuid.UUID],
    *,
    actor_user_id: uuid.UUID,
) -> None:
    unique_ids = set(user_ids)
    unique_ids.discard(project.owner_user_id)
    if unique_ids:
        valid_ids = set(
            (
                await db.execute(
                    select(User.id).where(
                        User.id.in_(unique_ids),
                        User.tenant_id == project.tenant_id,
                        User.is_active.is_(True),
                    )
                )
            ).scalars()
        )
        if valid_ids != unique_ids:
            raise HTTPException(status_code=422, detail="Every shared user must be active in the project tenant")
    await db.execute(delete(ProjectAccessGrant).where(ProjectAccessGrant.project_id == project.id))
    for user_id in sorted(unique_ids, key=str):
        db.add(
            ProjectAccessGrant(
                tenant_id=project.tenant_id,
                project_id=project.id,
                user_id=user_id,
                role="view",
                created_by_user_id=actor_user_id,
            )
        )
    project.visibility = "shared" if unique_ids else "private"


async def create_project(db: AsyncSession, user: User, data: ProjectCreate) -> Project:
    tenant_id = _tenant_id(user)
    project = Project(
        tenant_id=tenant_id,
        owner_user_id=user.id,
        template_id=data.template_id,
        name=data.name,
        description=data.description,
        goal=data.goal,
        success_criteria=data.success_criteria,
        visibility="private",
        status=data.status,
        settings=data.settings,
    )
    db.add(project)
    await db.flush()
    members = list(data.members)
    if members:
        leader_count = sum(member.is_leader for member in members)
        if leader_count > 1:
            raise HTTPException(status_code=422, detail="A project can have only one leader")
        if leader_count == 0:
            members[0].is_leader = True
    for member in members:
        await add_member(db, project, member, actor_user_id=user.id)

    capabilities = list(data.capabilities)
    explicit_capability_ids = {capability.capability_id for capability in capabilities}
    for capability_id in data.shared_capability_ids:
        if capability_id in explicit_capability_ids:
            continue
        skill = (
            await db.execute(
                select(Skill).where(
                    Skill.id == capability_id,
                    or_(Skill.tenant_id == tenant_id, Skill.tenant_id.is_(None)),
                )
            )
        ).scalar_one_or_none()
        capability_type = "skill"
        capability_name = skill.name if skill else None
        if skill is None:
            mcp = (
                await db.execute(
                    select(MCPServer).where(
                        MCPServer.id == capability_id,
                        or_(MCPServer.tenant_id == tenant_id, MCPServer.tenant_id.is_(None)),
                    )
                )
            ).scalar_one_or_none()
            if mcp is None:
                raise HTTPException(status_code=422, detail=f"Capability {capability_id} is unavailable")
            capability_type = "mcp"
            capability_name = mcp.display_name or mcp.name
        capabilities.append(
            ProjectCapabilityCreate(
                capability_type=capability_type,
                capability_id=capability_id,
                capability_name=capability_name,
                source="shared",
            )
        )
    for capability in capabilities:
        await add_capability(db, project, capability, actor_user_id=user.id)

    if data.visibility == "shared" and not data.shared_with_user_ids:
        raise HTTPException(status_code=422, detail="shared visibility requires shared_with_user_ids")
    await replace_access_grants(db, project, data.shared_with_user_ids, actor_user_id=user.id)
    # A project with Agents owns its canonical group root from creation time;
    # GET remains a safe fallback for projects created before this migration.
    if members:
        await ensure_project_group_session(db, project)
        await ensure_project_leader_session(db, project)
    from app.services.project_git_service import initialize_project_repo

    git_config = dict((project.settings or {}).get("git") or {})
    git_mode = git_config.get("mode") or git_config.get("repository_mode", "managed")
    if git_mode != "managed":
        raise HTTPException(status_code=501, detail="External Git repositories require a connector")
    git_state = await initialize_project_repo(project)
    project.settings = {
        **(project.settings or {}),
        "git": {**git_config, "mode": git_mode, **git_state},
    }
    add_event(db, project, "project.created", f"Created project {project.name}", actor_user_id=user.id)
    if project.status == "initializing":
        project.status = "planning"
    if project.status == "planning":
        add_event(
            db,
            project,
            "project.initialized",
            "Project snapshots and capability bindings are ready for Leader planning",
            actor_user_id=user.id,
        )
    await db.flush()
    # PostgreSQL server-side timestamps are expired after the final UPDATE.
    # Refresh while we are still inside the async greenlet so response
    # serialization never triggers implicit synchronous IO (MissingGreenlet).
    await db.refresh(project)
    return project


async def freeze_run_members(db: AsyncSession, project: Project, run: ProjectRun) -> list[ProjectRunMemberSnapshot]:
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.is_enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    capabilities = (
        (
            await db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                    ProjectCapabilityBinding.is_enabled.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    frozen: list[ProjectRunMemberSnapshot] = []
    for member in members:
        effective = [
            {
                "binding_id": str(binding.id),
                "type": binding.capability_type,
                "capability_id": str(binding.capability_id) if binding.capability_id else None,
                "name": binding.capability_name,
                "source": binding.source,
                "scope": binding.scope,
                "config": binding.config,
            }
            for binding in capabilities
            if binding.source == "shared" or binding.inherited_from_agent_id == member.agent_id
        ]
        snapshot = ProjectRunMemberSnapshot(
            tenant_id=project.tenant_id,
            project_id=project.id,
            run_id=run.id,
            project_member_id=member.id,
            agent_id=member.agent_id,
            is_leader=member.is_leader,
            member_config_snapshot=dict(member.config_snapshot or {}),
            capability_snapshot=effective,
        )
        db.add(snapshot)
        frozen.append(snapshot)
    await db.flush()
    return frozen


async def project_summary(db: AsyncSession, project: Project) -> dict:
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot)
                .where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                )
                .order_by(ProjectMemberSnapshot.is_leader.desc(), ProjectMemberSnapshot.created_at)
            )
        )
        .scalars()
        .all()
    )
    grants = (
        await db.execute(
            select(ProjectAccessGrant, User.display_name)
            .join(User, User.id == ProjectAccessGrant.user_id)
            .where(
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.tenant_id == project.tenant_id,
            )
        )
    ).all()
    work_counts = dict(
        (
            await db.execute(
                select(ProjectWorkItem.status, func.count(ProjectWorkItem.id))
                .where(ProjectWorkItem.project_id == project.id, ProjectWorkItem.tenant_id == project.tenant_id)
                .group_by(ProjectWorkItem.status)
            )
        ).all()
    )
    total = sum(work_counts.values())
    done = work_counts.get("done", 0)
    leader = next((member for member in members if member.is_leader), None)
    owner_name = (
        await db.execute(
            select(User.display_name).where(User.id == project.owner_user_id, User.tenant_id == project.tenant_id)
        )
    ).scalar_one_or_none()
    return {
        "id": str(project.id),
        "tenant_id": str(project.tenant_id),
        "owner_user_id": str(project.owner_user_id),
        "template_id": str(project.template_id) if project.template_id else None,
        "name": project.name,
        "description": project.description,
        "goal": project.goal,
        "objective": project.goal,
        "success_criteria": project.success_criteria,
        "visibility": project.visibility,
        "status": project.status,
        "settings": project.settings,
        "progress": round(done * 100 / total) if total else 0,
        "work_item_counts": work_counts,
        "members": [
            {
                "id": str(member.id),
                "agent_id": str(member.agent_id),
                "agent_name": member.name_snapshot,
                "name_snapshot": member.name_snapshot,
                "role_snapshot": member.role_snapshot,
                "is_leader": member.is_leader,
                "is_enabled": member.is_enabled,
                "status": "active" if member.is_enabled else "departed",
                "config_snapshot": member.config_snapshot,
            }
            for member in members
        ],
        "leader_name": leader.name_snapshot if leader else None,
        "active_agent_count": sum(member.is_enabled for member in members),
        "current_signal": project.settings.get("current_signal"),
        "next_action": project.settings.get("next_action"),
        "owner_name": owner_name,
        "shared_with_names": [display_name for _, display_name in grants],
        "shared_with": [
            {"user_id": str(grant.user_id), "display_name": display_name, "role": grant.role}
            for grant, display_name in grants
        ],
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
    }


def apply_run_status(run: ProjectRun, status: str) -> None:
    # A ProjectRun is an append-only execution fact. Retrying creates another
    # run; it must never move an already terminal row back to a live state.
    reconcile_project_run_terminal_state(run)
    if run.status in TERMINAL_PROJECT_RUN_STATUSES:
        return
    run.status = status
    now = datetime.now(timezone.utc)
    if status == "running" and run.started_at is None:
        run.started_at = now
    if status in TERMINAL_PROJECT_RUN_STATUSES:
        run.finished_at = now


TERMINAL_PROJECT_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def reconcile_project_run_terminal_state(run: ProjectRun) -> bool:
    """Repair the durable invariant ``finished_at => terminal status``.

    A previous dispatch race could commit ``running`` after the child turn had
    already written ``finished_at``. The timestamp is the stronger completion
    fact, so recovery promotes the row to a terminal status and never clears
    evidence/output. The operation is idempotent and safe in request/daemon
    recovery paths.
    """

    if run.finished_at is None or run.status in TERMINAL_PROJECT_RUN_STATUSES:
        return False
    run.status = "failed" if (run.error or "").strip() else "succeeded"
    if run.started_at is None:
        run.started_at = run.created_at or run.finished_at
    return True


async def reconcile_project_runs(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """Repair stale non-terminal ProjectRuns for one tenant-scoped project."""

    conditions = [
        ProjectRun.project_id == project_id,
        ProjectRun.finished_at.is_not(None),
        ProjectRun.status.not_in(TERMINAL_PROJECT_RUN_STATUSES),
    ]
    if tenant_id is not None:
        conditions.append(ProjectRun.tenant_id == tenant_id)
    runs = (await db.execute(select(ProjectRun).where(*conditions))).scalars().all()
    return sum(reconcile_project_run_terminal_state(run) for run in runs)


async def deliver_project_a2a(run_id: uuid.UUID) -> None:
    """Consume one persisted project A2A run through the existing sender.

    This runs after the request transaction. Delivery status and its audit
    event are persisted in a fresh transaction, including truthful failures.
    """

    from app.services.agent_tools import _send_message_to_agent

    async with async_session() as db:
        run = await db.get(ProjectRun, run_id)
        if run is None or run.status != "queued" or run.trigger_type != "a2a":
            return
        project = await db.get(Project, run.project_id)
        if project is None:
            return
        payload = dict(run.input or {})
        project_id = run.project_id
        initiated_by_user_id = run.initiated_by_user_id
        from_agent_id = uuid.UUID(payload["from_agent_id"])
        to_agent_id = uuid.UUID(payload["to_agent_id"])
        active_member_ids = set(
            (
                await db.execute(
                    select(ProjectMemberSnapshot.agent_id).where(
                        ProjectMemberSnapshot.project_id == project.id,
                        ProjectMemberSnapshot.tenant_id == project.tenant_id,
                        ProjectMemberSnapshot.agent_id.in_([from_agent_id, to_agent_id]),
                        ProjectMemberSnapshot.is_enabled.is_(True),
                    )
                )
            ).scalars()
        )
        if active_member_ids != {from_agent_id, to_agent_id}:
            apply_run_status(run, "cancelled")
            run.error = "Project A2A sender or recipient is no longer an active member"
            add_event(
                db,
                project,
                "a2a.cancelled",
                "Cancelled project A2A because a participant left the project",
                actor_user_id=initiated_by_user_id,
                from_agent_id=from_agent_id,
                to_agent_id=to_agent_id,
                run_id=run.id,
                metadata={"reason": "project_member_departed"},
            )
            await db.commit()
            return
        mode_map = {"delegate": "task_delegate", "review": "consult"}
        msg_type = mode_map.get(payload.get("mode"), payload.get("mode", "notify"))
        apply_run_status(run, "running")
        await db.commit()

    try:
        result = await _send_message_to_agent(
            from_agent_id,
            {
                "agent_id": payload["to_agent_id"],
                "message": payload["message"],
                "msg_type": msg_type,
                "force_async": True,
                "new_conversation": bool(payload.get("new_conversation")),
                "_project_id": str(project_id),
            },
            user_id=initiated_by_user_id,
        )
    except Exception as exc:  # delivery failures must become durable run state
        result = f"❌ Project A2A delivery raised {type(exc).__name__}: {exc!s}"
    failed = result.startswith("❌") or '"status": "error"' in result
    session_info: dict = {}
    async with async_session() as session_db:
        from app.models.chat_session import ChatSession

        source_id = uuid.UUID(payload["from_agent_id"])
        target_id = uuid.UUID(payload["to_agent_id"])
        session_agent_id = min(source_id, target_id, key=str)
        session_peer_id = max(source_id, target_id, key=str)
        session = (
            await session_db.execute(
                select(ChatSession).where(
                    ChatSession.project_id == project_id,
                    ChatSession.source_channel == "agent",
                    ChatSession.agent_id == session_agent_id,
                    ChatSession.peer_agent_id == session_peer_id,
                ).order_by(
                    ChatSession.last_message_at.desc().nulls_last(),
                    ChatSession.created_at.desc(),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if session is not None:
            session_info = {
                "session_id": str(session.id),
                "session_agent_id": str(session.agent_id),
                "session_access_agent_id": str(session.agent_id),
                "session_title": session.title,
            }
    async with async_session() as db:
        run = await db.get(ProjectRun, run_id)
        project = await db.get(Project, run.project_id) if run else None
        if run is None or project is None:
            return
        apply_run_status(run, "failed" if failed else "succeeded")
        delivery_metadata = {
            "delivery_result": result,
            "group_session_id": payload.get("group_session_id"),
            **session_info,
        }
        run.output = delivery_metadata
        if failed:
            run.error = result
        add_event(
            db,
            project,
            "a2a.delivery_failed" if failed else "a2a.delivered",
            "Project A2A delivery failed" if failed else "Project A2A message delivered and target wake requested",
            actor_user_id=run.initiated_by_user_id,
            actor_agent_id=uuid.UUID(payload["from_agent_id"]),
            from_agent_id=uuid.UUID(payload["from_agent_id"]),
            to_agent_id=uuid.UUID(payload["to_agent_id"]),
            work_item_id=run.work_item_id,
            run_id=run.id,
            metadata=delivery_metadata,
        )
        await db.commit()
