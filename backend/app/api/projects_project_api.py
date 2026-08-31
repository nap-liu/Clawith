from app.api.projects_shared import *  # noqa: F401,F403

@router.get("/{project_id}")
async def get_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    return await project_summary(db, project, actor_user_id=current_user.id)


@router.get("/{project_id}/directory/departments")
async def get_project_directory_departments(
    project_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return organization departments available to the project owner."""
    project = await require_owner(db, current_user, project_id)
    return await permission_directory_departments(
        db,
        tenant_id=project.tenant_id,
        current_user_id=current_user.id,
        parent_id=parent_id,
        search=search,
        limit=limit,
    )


@router.get("/{project_id}/directory/members")
async def get_project_directory_members(
    project_id: uuid.UUID,
    department_id: uuid.UUID | None = None,
    include_descendants: bool = False,
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return canonical share candidates available to the project owner."""
    project = await require_owner(db, current_user, project_id)
    return await permission_directory_members(
        db,
        tenant_id=project.tenant_id,
        department_id=department_id,
        include_descendants=include_descendants,
        search=search,
        page=page,
        page_size=page_size,
        excluded_user_ids={project.owner_user_id},
    )


@router.get("/{project_id}/dashboard")
async def get_project_dashboard(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    # Repair ProjectRuns written by older dispatchers before exposing the
    # dashboard. This is an idempotent data invariant, not presentation logic.
    if await reconcile_project_runs(db, project.id, tenant_id=project.tenant_id):
        await db.commit()
    summary = await project_summary(db, project, actor_user_id=current_user.id)
    work_items = (
        (
            await db.execute(
                select(ProjectWorkItem)
                .where(
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.tenant_id == project.tenant_id,
                )
                .order_by(ProjectWorkItem.updated_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    runs = (
        (
            await db.execute(
                select(ProjectRun)
                .where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                )
                .order_by(ProjectRun.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    events = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(
                    ProjectEvent.project_id == project.id,
                    ProjectEvent.tenant_id == project.tenant_id,
                )
                .order_by(ProjectEvent.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
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
                )
            )
        )
        .scalars()
        .all()
    )
    await reconcile_project_repository_operations(project.id, db=db)
    git_state = await repository_state(project, 20)
    files = await list_project_files(project)
    return {
        "project": summary,
        "members": members,
        "capabilities": capabilities,
        "work_items": work_items,
        "runs": await serialize_project_runs(db, project, list(runs)),
        "events": await serialize_project_events(db, project, list(events)),
        "git": git_state,
        "commits": git_state["commits"],
        "files": files,
        "branches": git_state["branches"],
        "settings": project.settings or {},
        "policies": (project.settings or {}).get("policies", {}),
        "runtime": (project.settings or {}).get("runtime", {}),
        "work_item_counts": summary["work_item_counts"],
        "progress": summary["progress"],
    }


@router.patch("/{project_id}")
async def patch_project(
    project_id: uuid.UUID,
    data: ProjectUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    updates = data.model_dump(exclude_unset=True)
    acl_change = bool({"visibility", "shared_with_user_ids", "execution_user_id"} & data.model_fields_set)
    runtime_change = "status" in data.model_fields_set
    project = (
        await require_owner(db, current_user, project_id, lock=True)
        if acl_change or runtime_change
        else await require_project(db, current_user, project_id, edit=True)
    )
    runtime_event_type = None
    target_status = updates.get("status")
    runtime_previous_status = project.status if runtime_change else None
    if runtime_change:
        if project.status not in {"running", "paused", "waiting"} or target_status not in {"running", "paused"}:
            raise HTTPException(
                status_code=409,
                detail="The project runtime switch is unavailable in the current project state",
            )
        if target_status != project.status:
            runtime_event_type = "project.paused" if target_status == "paused" else "project.resumed"
    shared_ids = updates.pop("shared_with_user_ids", None)
    requested_visibility = updates.pop("visibility", None)
    requested_execution_user_id = updates.pop("execution_user_id", None)
    normalized_shared_ids = (
        {user_id for user_id in shared_ids if user_id != project.owner_user_id} if shared_ids is not None else None
    )
    if requested_visibility == "shared" and normalized_shared_ids is not None and not normalized_shared_ids:
        raise HTTPException(status_code=422, detail="A shared project requires at least one shared user")
    if requested_visibility == "private" and normalized_shared_ids:
        raise HTTPException(status_code=422, detail="A private project cannot include shared users")
    current_shared_ids = set(
        (
            await db.execute(
                select(ProjectAccessGrant.user_id).where(
                    ProjectAccessGrant.project_id == project.id,
                    ProjectAccessGrant.tenant_id == project.tenant_id,
                )
            )
        ).scalars()
    )
    effective_shared_ids = normalized_shared_ids if normalized_shared_ids is not None else current_shared_ids
    if normalized_shared_ids is not None:
        effective_visibility = "shared" if effective_shared_ids else "private"
    elif requested_visibility == "private":
        effective_visibility = "private"
        effective_shared_ids = set()
    else:
        effective_visibility = requested_visibility or project.visibility
    if effective_visibility == "shared" and not effective_shared_ids:
        raise HTTPException(status_code=422, detail="A shared project requires at least one shared user")

    execution_change = "execution_user_id" in data.model_fields_set
    if execution_change and not can_manage_project_execution_user(current_user, project):
        raise HTTPException(
            status_code=403,
            detail="Only platform or company administrators can change the project execution user",
        )
    previous_execution_user_id = project_execution_user_id(project)
    automatic_execution_fallback = False
    if effective_visibility == "private":
        automatic_execution_fallback = previous_execution_user_id != project.owner_user_id
        project.execution_user_id = None
    else:
        if execution_change:
            selected_execution_user_id = requested_execution_user_id
            # NULL and the owner UUID are the same canonical owner fallback.
            project.execution_user_id = (
                None if selected_execution_user_id in {None, project.owner_user_id} else selected_execution_user_id
            )
        elif project.visibility != "shared":
            # A newly shared project always starts under its owner unless an
            # administrator atomically chooses another active shared user.
            project.execution_user_id = None
        selected_execution_user_id = project_execution_user_id(project)
        valid_execution_user_ids = {project.owner_user_id, *effective_shared_ids}
        if selected_execution_user_id not in valid_execution_user_ids:
            if execution_change:
                raise HTTPException(
                    status_code=422,
                    detail="Project execution user must be the owner or an active shared project user",
                )
            if project.execution_user_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail="The current project execution user must be changed before removing their access",
                )
            selected_execution_user_id = project.owner_user_id
    for key, value in updates.items():
        setattr(project, key, value)
    if shared_ids is not None:
        await replace_access_grants(db, project, shared_ids, actor_user_id=current_user.id)
    elif requested_visibility == "private":
        await replace_access_grants(db, project, [], actor_user_id=current_user.id)
    elif requested_visibility == "shared" and project.visibility != "shared":
        raise HTTPException(status_code=422, detail="Set shared_with_user_ids when sharing a project")
    if project.execution_user_id is not None:
        await resolve_project_execution_user(db, project, project.execution_user_id)
    current_execution_user_id = project_execution_user_id(project)
    if current_execution_user_id != previous_execution_user_id:
        add_event(
            db,
            project,
            "project.execution_user.changed",
            "Changed the project execution user",
            actor_user_id=current_user.id,
            metadata={
                "previous_execution_user_id": str(previous_execution_user_id),
                "execution_user_id": str(current_execution_user_id),
                "automatic_fallback": automatic_execution_fallback,
            },
        )
    if runtime_event_type is not None:
        add_event(
            db,
            project,
            runtime_event_type,
            "Paused all new project work" if target_status == "paused" else "Resumed project work",
            actor_user_id=current_user.id,
            metadata={"previous_status": runtime_previous_status},
        )
    if set(updates) - {"status"} or acl_change:
        add_event(db, project, "project.updated", "Project settings updated", actor_user_id=current_user.id)
    await db.flush()
    await db.refresh(project)
    return await project_summary(db, project, actor_user_id=current_user.id)


@router.get("/{project_id}/settings")
async def get_project_settings(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    return project.settings or {}


@router.patch("/{project_id}/settings")
async def patch_project_settings(
    project_id: uuid.UUID,
    data: ProjectSettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    patch = data.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=422, detail="At least one project setting is required")
    if "git" in patch:
        raise HTTPException(status_code=422, detail="Git repository configuration cannot be changed through settings")
    project.settings = _merge_settings(project.settings or {}, patch)
    add_event(
        db,
        project,
        "project.settings.updated",
        "Updated project runtime or governance settings",
        actor_user_id=current_user.id,
        metadata={"changed_keys": sorted(patch)},
    )
    await db.flush()
    return project.settings


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id, lock=True)
    if project.status in {"initializing", "running"}:
        raise HTTPException(
            status_code=409,
            detail="Pause the project before deleting it",
        )
    cleanup_target = SimpleNamespace(id=project.id, tenant_id=project.tenant_id)

    # Project-scoped Agents and conversations are owned by the project and are
    # removed by the project's database cascades.  Chat messages predate that
    # ownership model and intentionally have no session/project foreign key, so
    # delete the exact project conversation rows before the cascades reach the
    # project Agents they reference.
    project_agent_ids = list(
        (
            await db.execute(
                select(Agent.id).where(
                    Agent.project_id == project.id,
                    Agent.tenant_id == project.tenant_id,
                    Agent.scope == "project",
                )
            )
        ).scalars()
    )
    session_scope = [ChatSession.project_id == project.id]
    if project_agent_ids:
        session_scope.extend(
            (
                ChatSession.agent_id.in_(project_agent_ids),
                ChatSession.peer_agent_id.in_(project_agent_ids),
            )
        )
    related_session_ids = list(
        (
            await db.execute(select(ChatSession.id).where(or_(*session_scope)))
        ).scalars()
    )
    active_project_run_id = await db.scalar(
        select(ProjectRun.id)
        .where(
            ProjectRun.project_id == project.id,
            ProjectRun.status.not_in({"succeeded", "failed", "cancelled"}),
        )
        .limit(1)
    )
    subagent_scope = [SubagentRun.project_id == project.id]
    if related_session_ids:
        subagent_scope.extend(
            (
                SubagentRun.id.in_(related_session_ids),
                SubagentRun.parent_session_id.in_(related_session_ids),
            )
        )
    active_subagent_run_id = await db.scalar(
        select(SubagentRun.id)
        .where(
            or_(*subagent_scope),
            SubagentRun.status.not_in({"completed", "failed", "cancelled"}),
        )
        .limit(1)
    )
    if active_project_run_id is not None or active_subagent_run_id is not None:
        raise HTTPException(
            status_code=409,
            detail="The project still has active work; wait for it to finish or cancel it before deleting",
        )

    # Child runs must be removed before their parent ChatSessions because the
    # durable parent edge intentionally uses RESTRICT.
    await db.execute(delete(SubagentRun).where(or_(*subagent_scope)))

    project_session_ids = [str(session_id) for session_id in related_session_ids]
    message_scope = []
    if project_session_ids:
        message_scope.append(ChatMessage.conversation_id.in_(project_session_ids))
    if project_agent_ids:
        message_scope.extend(
            (
                ChatMessage.agent_id.in_(project_agent_ids),
                ChatMessage.sender_agent_id.in_(project_agent_ids),
            )
        )
    if message_scope:
        await db.execute(delete(ChatMessage).where(or_(*message_scope)))
    if related_session_ids:
        await db.execute(delete(ChatSession).where(ChatSession.id.in_(related_session_ids)))

    # Project digital employees are deleted by the project FK cascade, but a
    # small set of older Agent-owned tables intentionally has no cascade. Clear
    # those exact project-owned rows first so a used project remains deletable.
    # The table names are fixed application schema identifiers; values remain
    # bound parameters.
    if project_agent_ids:
        cleanup_tables = (
            "agent_activity_logs",
            "audit_logs",
            "approval_requests",
            "channel_configs",
            "daily_token_usage",
            "dingtalk_channel_provisioning_sessions",
            "published_pages",
            "notifications",
            "agent_permissions",
        )
        for agent_id in project_agent_ids:
            await db.execute(
                text(
                    "DELETE FROM gateway_messages "
                    "WHERE agent_id = :agent_id OR sender_agent_id = :agent_id"
                ),
                {"agent_id": agent_id},
            )
            await db.execute(
                text(
                    "DELETE FROM task_logs WHERE task_id IN "
                    "(SELECT id FROM tasks WHERE agent_id = :agent_id)"
                ),
                {"agent_id": agent_id},
            )
            await db.execute(
                text("DELETE FROM tasks WHERE agent_id = :agent_id"),
                {"agent_id": agent_id},
            )
            for table_name in cleanup_tables:
                await db.execute(
                    text(f"DELETE FROM {table_name} WHERE agent_id = :agent_id"),
                    {"agent_id": agent_id},
                )

    await db.delete(project)
    await db.commit()
    try:
        await remove_project_repository(cleanup_target)
    except Exception:
        logger.exception("Project {} was deleted but managed storage cleanup failed", project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
