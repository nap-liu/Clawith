"""Tenant-safe orchestration services for AI-native projects."""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.agent import Agent
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
        raise HTTPException(status_code=409, detail="Agent is already a project member")
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
        project.status = "running"
        add_event(
            db,
            project,
            "project.initialized",
            "Project snapshots and capability bindings are ready",
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
                "status": "active" if member.is_enabled else "disabled",
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
    run.status = status
    now = datetime.now(timezone.utc)
    if status == "running" and run.started_at is None:
        run.started_at = now
    if status in {"succeeded", "failed", "cancelled"}:
        run.finished_at = now


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
        from_agent_id = uuid.UUID(payload["from_agent_id"])
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
                "_project_id": str(project.id),
            },
            user_id=run.initiated_by_user_id,
        )
    except Exception as exc:  # delivery failures must become durable run state
        result = f"❌ Project A2A delivery raised {type(exc).__name__}: {exc!s}"
    failed = result.startswith("❌") or '"status": "error"' in result
    async with async_session() as db:
        run = await db.get(ProjectRun, run_id)
        project = await db.get(Project, run.project_id) if run else None
        if run is None or project is None:
            return
        apply_run_status(run, "failed" if failed else "succeeded")
        run.output = {"delivery_result": result}
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
            metadata={"delivery_result": result},
        )
        await db.commit()
