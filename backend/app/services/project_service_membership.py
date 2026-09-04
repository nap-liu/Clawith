"""Project membership and capability orchestration."""

from app.services.project_service_shared import *  # noqa: F403
from app.services.project_service_access import *  # noqa: F403
from app.services.project_service_access import (
    _is_company_project_admin,
    _is_platform_project_admin,
)

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
    bounded_summary, overflow_metadata = bounded_project_event_summary(event_type, summary)
    event = ProjectEvent(
        tenant_id=project.tenant_id,
        project_id=project.id,
        event_type=event_type,
        summary=bounded_summary,
        actor_user_id=actor_user_id,
        actor_agent_id=actor_agent_id,
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        work_item_id=work_item_id,
        run_id=run_id,
        event_metadata={**overflow_metadata, **dict(metadata or {})},
    )
    db.add(event)
    return event


async def _get_project_agent(db: AsyncSession, project: Project, agent_id: uuid.UUID) -> Agent:
    agent = (
        await db.execute(
            select(Agent).where(
                Agent.id == agent_id,
                Agent.tenant_id == project.tenant_id,
                Agent.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=422, detail=f"数字员工 {agent_id} 在当前租户不可用")
    if agent.scope == "project" and agent.project_id != project.id:
        raise HTTPException(status_code=422, detail="项目专用数字员工不能加入其他项目")
    return agent


async def add_member(
    db: AsyncSession,
    project: Project,
    data: ProjectMemberCreate,
    *,
    actor_user_id: uuid.UUID,
) -> ProjectMemberSnapshot:
    agent = await _get_project_agent(db, project, data.agent_id)
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
        raise HTTPException(
            status_code=422, detail="New project members must start active; remove them explicitly later"
        )
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
            "temperature": agent.temperature,
            "reasoning_effort": agent.reasoning_effort,
            "autonomy_policy": dict(agent.autonomy_policy or {}),
            "max_tool_rounds": agent.max_tool_rounds,
            "project_instruction": "",
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
        raise HTTPException(
            status_code=422,
            detail="Transfer project responsibility before removing the project owner",
        )
    if not member.is_enabled:
        return []

    all_children = (
        (
            await db.execute(
                select(SubagentRun).where(
                    SubagentRun.project_id == project.id,
                    SubagentRun.project_member_id == member.id,
                )
            )
        )
        .scalars()
        .all()
    )
    live_children = [row for row in all_children if row.status in {"queued", "running"}]
    child_ids = [row.id for row in all_children]
    cancelled_child_ids = [row.id for row in live_children]
    now = datetime.now(timezone.utc)
    for child in live_children:
        child.status = "cancelled"
        child.lease_owner = None
        child.lease_expires_at = None

    if child_ids:
        sessions = (await db.execute(select(ChatSession).where(ChatSession.id.in_(child_ids)))).scalars().all()
        from app.services.conversation_turn_lifecycle import (
            cancel_current_conversation_turn,
        )

        live_child_ids = set(cancelled_child_ids)
        for session in sessions:
            if session.id in live_child_ids:
                await cancel_current_conversation_turn(
                    db,
                    agent_id=session.agent_id,
                    conversation_id=str(session.id),
                )
            session.im_config = {
                **dict(session.im_config or {}),
                "membership_revoked": True,
                "membership_revoked_at": now.isoformat(),
                "membership_revoked_reason": "project_member_departed",
            }
        inputs = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id.in_([str(value) for value in child_ids]),
                        ChatMessage.message_meta["kind"].as_string() == "subagent_input",
                        ChatMessage.message_meta["subagent_input_state"].as_string().in_(["pending", "processing"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in inputs:
            metadata = dict(row.message_meta or {})
            metadata["subagent_input_state"] = "cancelled"
            metadata["cancel_reason"] = "project_member_departed"
            row.message_meta = metadata

    project_runs = (
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                    ProjectRun.agent_id == member.agent_id,
                    ProjectRun.status.not_in(TERMINAL_PROJECT_RUN_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
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
    await _get_project_agent(db, project, member.agent_id)

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
    """Return ``read``/``edit`` for an auditable project session.

    Group sessions use the project ACL directly. Historical Subagent sessions
    remain readable after departure; writing them additionally requires the
    exact durable member snapshot to still be enabled.
    """

    if session.project_id is None:
        return None
    project = await db.get(Project, session.project_id)
    if project is None:
        return None

    if _is_platform_project_admin(user):
        human_role = "edit"
    elif user.tenant_id is None or project.tenant_id != user.tenant_id:
        return None
    elif _is_company_project_admin(user) or project.owner_user_id == user.id:
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

    if session.source_channel == "project" and session.is_group:
        return human_role
    if session.source_channel != "subagent":
        return None

    run = await db.get(SubagentRun, session.id)
    if (
        run is None
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
    member_is_writable = member.is_enabled and not bool(dict(session.im_config or {}).get("membership_revoked"))
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
    existing_roles = dict(
        (
            await db.execute(
                select(ProjectAccessGrant.user_id, ProjectAccessGrant.role).where(
                    ProjectAccessGrant.project_id == project.id,
                    ProjectAccessGrant.tenant_id == project.tenant_id,
                )
            )
        ).all()
    )
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
    await db.execute(
        delete(ProjectAccessGrant).where(
            ProjectAccessGrant.project_id == project.id,
            ProjectAccessGrant.tenant_id == project.tenant_id,
        )
    )
    for user_id in sorted(unique_ids, key=str):
        db.add(
            ProjectAccessGrant(
                tenant_id=project.tenant_id,
                project_id=project.id,
                user_id=user_id,
                role=existing_roles.get(user_id, "view"),
                created_by_user_id=actor_user_id,
            )
        )
    project.visibility = "shared" if unique_ids else "private"
