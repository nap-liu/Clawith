"""Project access, runtime, and leader-session orchestration."""

from app.services.project_service_shared import *  # noqa: F403

def ensure_project_running(project: Project) -> None:
    """Reject a new project wake unless the authoritative project is running."""

    if project.status != PROJECT_RUNTIME_STATUS_RUNNING:
        raise HTTPException(
            status_code=409,
            detail="Project runtime is paused or unavailable; resume the project before starting new work",
        )


def ensure_project_accepts_group_message(project: Project) -> None:
    """Allow project conversation independently from execution scheduling.

    Planning, waiting, paused, and completed projects remain conversational. All new
    execution, A2A, scheduling, and specialist wakes still require ``running``.
    """

    if project.status not in PROJECT_CONVERSATION_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Project conversation is unavailable; resume the project before sending new messages",
        )


async def project_runtime_allows_agent(db: AsyncSession, agent: Agent) -> bool:
    """Return whether an Agent may accept a new turn under its project switch.

    Standard Agents are not governed by a project. Project Agents always defer
    to their owning Project status, which keeps schedules and triggers aligned
    with the same switch used by Project Runs and collaboration.
    """

    if getattr(agent, "scope", "standard") != "project":
        return True
    if getattr(agent, "project_id", None) is None:
        return False
    status_value = await db.scalar(select(Project.status).where(Project.id == agent.project_id))
    return status_value == PROJECT_RUNTIME_STATUS_RUNNING


def bounded_project_event_summary(event_type: str, summary: str) -> tuple[str, dict[str, Any]]:
    """Return a database-safe one-line summary without silently losing detail.

    Project event detail belongs in ``event_metadata``.  For an unexpectedly
    large summary, use a stable, searchable event-type marker instead of
    slicing user text at an arbitrary Unicode/code-point boundary.  The caller
    receives the complete original summary and its digest for the audit JSON.
    """

    original = str(summary or "")
    normalized = " ".join(original.split())
    if len(normalized) <= PROJECT_EVENT_SUMMARY_MAX_LENGTH:
        return normalized, {}
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    return (
        f"{event_type} · full details stored in event metadata · sha256:{digest}",
        {
            "full_summary": original,
            "summary_sha256": digest,
            "summary_compacted": True,
        },
    )


def _tenant_id(user: User) -> uuid.UUID:
    if user.tenant_id is None:
        raise HTTPException(status_code=403, detail="A tenant membership is required")
    return user.tenant_id


def project_execution_user_id(project: Project) -> uuid.UUID:
    """Return the configured project principal, with owner fallback for legacy rows."""

    return project.execution_user_id or project.owner_user_id


async def resolve_project_execution_user(
    db: AsyncSession,
    project: Project,
    execution_user_id: uuid.UUID | None = None,
) -> User:
    """Resolve an active principal that still belongs to the project's human ACL."""

    user_id = execution_user_id or project_execution_user_id(project)
    user = (
        await db.execute(
            select(User).where(
                User.id == user_id,
                User.tenant_id == project.tenant_id,
                User.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=422, detail="Project execution user must be active in the project tenant")
    if user.id != project.owner_user_id:
        grant_id = await db.scalar(
            select(ProjectAccessGrant.id).where(
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.tenant_id == project.tenant_id,
                ProjectAccessGrant.user_id == user.id,
            )
        )
        if project.visibility != "shared" or grant_id is None:
            raise HTTPException(status_code=422, detail="Project execution user must be a shared project user")
    return user


def _is_platform_project_admin(user: User) -> bool:
    from app.core.permissions import is_platform_admin_user

    return is_platform_admin_user(user)


def _is_company_project_admin(user: User) -> bool:
    return user.role == "org_admin" and user.tenant_id is not None


def can_manage_project_execution_user(user: User, project: Project) -> bool:
    """Return whether the current tenant administrator may choose the runtime principal."""

    return user.tenant_id == project.tenant_id and (
        _is_platform_project_admin(user) or _is_company_project_admin(user)
    )


def can_manage_project_as_owner(user: User, project: Project) -> bool:
    """Return the compatibility owner-management capability for one project."""

    if user.tenant_id != project.tenant_id:
        return False
    return (
        user.id == project.owner_user_id
        or _is_platform_project_admin(user)
        or _is_company_project_admin(user)
    )


def accessible_projects_clause(user: User, *, edit: bool = False):
    tenant_id = _tenant_id(user)
    if _is_platform_project_admin(user) or _is_company_project_admin(user):
        return Project.tenant_id == tenant_id
    grant = exists().where(
        ProjectAccessGrant.project_id == Project.id,
        ProjectAccessGrant.tenant_id == tenant_id,
        ProjectAccessGrant.user_id == user.id,
        *([ProjectAccessGrant.role == "edit"] if edit else []),
    )
    return and_(Project.tenant_id == tenant_id, or_(Project.owner_user_id == user.id, grant))


async def require_project(
    db: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    *,
    edit: bool = False,
    lock: bool = False,
) -> Project:
    statement = select(Project).where(
        Project.id == project_id,
        accessible_projects_clause(user, edit=edit),
    )
    if lock:
        statement = statement.with_for_update()
    project = (await db.execute(statement)).scalar_one_or_none()
    if project is None:
        # Deliberately hide existence across tenants and unauthorized users.
        raise HTTPException(status_code=404, detail="Project not found")
    if (
        lock
        and project.owner_user_id != user.id
        and not _is_platform_project_admin(user)
        and not _is_company_project_admin(user)
    ):
        grant_role = await db.scalar(
            select(ProjectAccessGrant.role)
            .where(
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.tenant_id == project.tenant_id,
                ProjectAccessGrant.user_id == user.id,
                *([ProjectAccessGrant.role == "edit"] if edit else []),
            )
            .with_for_update()
        )
        if grant_role is None:
            raise HTTPException(status_code=404, detail="Project not found")
    return project


async def require_owner(
    db: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    *,
    lock: bool = False,
) -> Project:
    statement = select(Project).where(
        Project.id == project_id,
        Project.tenant_id == _tenant_id(user),
    )
    if not (_is_platform_project_admin(user) or _is_company_project_admin(user)):
        statement = statement.where(
            Project.owner_user_id == user.id,
        )
    if lock:
        statement = statement.with_for_update()
    project = (await db.execute(statement)).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


async def ensure_project_group_session(db: AsyncSession, project: Project) -> ChatSession:
    """Return the project's single durable group root.

    The access Agent is fixed at first creation instead of following project-owner
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
            select(ProjectMemberSnapshot)
            .where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
            .order_by(
                ProjectMemberSnapshot.is_leader.desc(),
                ProjectMemberSnapshot.created_at,
                ProjectMemberSnapshot.id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if anchor is None:
        raise HTTPException(status_code=422, detail="项目需要至少一名已启用的数字员工才能开始群聊")
    session = ChatSession(
        project_id=project.id,
        agent_id=anchor.agent_id,
        title=f"{project.name} · 项目协作",
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
async def ensure_enabled_project_leader(
    db: AsyncSession,
    project: Project,
) -> ProjectMemberSnapshot:
    """Return the enabled project owner and repair legacy projects without one."""

    leader = (
        await db.execute(
            select(ProjectMemberSnapshot)
            .join(Agent, Agent.id == ProjectMemberSnapshot.agent_id)
            .where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.is_leader.is_(True),
                ProjectMemberSnapshot.is_enabled.is_(True),
                Agent.project_id == project.id,
                Agent.tenant_id == project.tenant_id,
                Agent.scope == "project",
                Agent.is_deleted.is_(False),
                Agent.status.in_(["running", "idle"]),
            )
            .order_by(ProjectMemberSnapshot.created_at, ProjectMemberSnapshot.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if leader is not None:
        await _restore_blocked_project_replies(db, project, leader)
        return leader

    leader = (
        await db.execute(
            select(ProjectMemberSnapshot)
            .join(Agent, Agent.id == ProjectMemberSnapshot.agent_id)
            .where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
                Agent.project_id == project.id,
                Agent.tenant_id == project.tenant_id,
                Agent.scope == "project",
                Agent.is_deleted.is_(False),
                Agent.status.in_(["running", "idle"]),
            )
            .order_by(ProjectMemberSnapshot.created_at, ProjectMemberSnapshot.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if leader is None:
        raise HTTPException(
            status_code=422,
            detail="Project needs at least one enabled digital employee",
        )
    await db.execute(
        ProjectMemberSnapshot.__table__.update()
        .where(ProjectMemberSnapshot.project_id == project.id)
        .values(is_leader=False)
    )
    leader.is_leader = True
    await _restore_blocked_project_replies(db, project, leader)
    add_event(
        db,
        project,
        "leader.repaired",
        f"Assigned {leader.name_snapshot} as project owner",
        actor_agent_id=leader.agent_id,
        metadata={"member_id": str(leader.id), "reason": "missing_enabled_leader"},
    )
    await db.flush()
    return leader


async def _restore_blocked_project_replies(
    db: AsyncSession,
    project: Project,
    leader: ProjectMemberSnapshot,
) -> None:
    """Return replies blocked by a missing owner to the normal durable queue."""

    group_ids = [
        str(value)
        for value in (
            await db.execute(
                select(ChatSession.id).where(
                    ChatSession.project_id == project.id,
                    ChatSession.source_channel == "project",
                )
            )
        ).scalars()
    ]
    if not group_ids:
        return
    rows = (
        (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id.in_(group_ids),
                    ChatMessage.message_meta["kind"].as_string() == "project_subagent_reply",
                    ChatMessage.message_meta["leader_batch_state"].as_string() == "blocked_no_leader",
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        metadata = dict(row.message_meta or {})
        metadata.update(
            {
                "leader_batch_state": "pending",
                "wake_policy": "leader_batch_pending",
                "default_leader_agent_id": str(leader.agent_id),
            }
        )
        row.message_meta = metadata


async def ensure_project_leader_session(db: AsyncSession, project: Project) -> ChatSession:
    """Return the legacy project-owner Web session for history compatibility.

    New projects plan in the canonical project group. Their matching Web session
    remains readable so historical links continue to resolve, but it is marked
    read-only and is never selected as the kickoff discussion source.
    """
    external_conv_id = f"project-leader:{project.id}"
    planning_mode = str(dict((project.settings or {}).get("planning") or {}).get("conversation_mode") or "")
    planning_transport = "project_group" if planning_mode == "project_group" else "leader_session"
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
    leader = await ensure_enabled_project_leader(db, project)
    if session is not None:
        # Keep historical links attached to the current project owner without
        # creating a second compatibility thread.
        if session.agent_id != leader.agent_id:
            session.agent_id = leader.agent_id
            session.title = f"{project.name} · 项目规划"
        session.im_config = {
            **dict(session.im_config or {}),
            "leader_agent_id": str(leader.agent_id),
            "planning_transport": planning_transport,
            "read_only": True,
        }
        return session
    session = ChatSession(
        project_id=project.id,
        agent_id=leader.agent_id,
        user_id=project.owner_user_id,
        title=f"{project.name} · 项目规划",
        source_channel="web",
        external_conv_id=external_conv_id,
        is_group=False,
        is_primary=False,
        im_config={
            "project_id": str(project.id),
            "project_member_id": str(leader.id),
            "leader_agent_id": str(leader.agent_id),
            "purpose": "project_kickoff_planning",
            "planning_transport": planning_transport,
            "read_only": True,
        },
    )
    db.add(session)
    await db.flush()
    return session
