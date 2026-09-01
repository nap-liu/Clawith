from app.api.projects_shared import *  # noqa: F401,F403
from app.api.projects_work_items import _require_member_agent

@router.get("/{project_id}/runs", response_model=list[ProjectRunOut])
async def list_project_runs(
    project_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    project = await require_project(db, current_user, project_id)
    if await reconcile_project_runs(db, project.id, tenant_id=project.tenant_id):
        await db.commit()
    runs = (
        (
            await db.execute(
                select(ProjectRun)
                .where(ProjectRun.project_id == project.id, ProjectRun.tenant_id == project.tenant_id)
                .order_by(ProjectRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return await serialize_project_runs(db, project, list(runs))


@router.post("/{project_id}/runs", response_model=ProjectRunOut, status_code=201)
async def create_project_run(
    project_id: uuid.UUID,
    data: ProjectRunCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create one durable, single-Agent execution and dispatch it immediately.

    ``POST /runs`` is an execution command, not a passive history insert. The
    durable ProjectRun + group anchor are committed as one outbox transaction;
    the daemon can retry dispatch after a process exit.
    """
    from app.services.subagent_runtime import dispatch_project_run

    project = await require_project(db, current_user, project_id, edit=True)
    ensure_project_running(project)
    if data.trigger_type == "a2a":
        raise HTTPException(
            status_code=422,
            detail="请通过项目协作功能向数字员工发送消息。",
        )
    work_item = None
    if data.work_item_id:
        work_item = (
            await db.execute(
                select(ProjectWorkItem).where(
                    ProjectWorkItem.id == data.work_item_id,
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.tenant_id == project.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if work_item is None:
            raise HTTPException(status_code=422, detail="所选任务不属于当前项目。")

    requested_agent_id = data.agent_id or (work_item.assignee_agent_id if work_item else None)
    member_conditions = [
        ProjectMemberSnapshot.project_id == project.id,
        ProjectMemberSnapshot.tenant_id == project.tenant_id,
        ProjectMemberSnapshot.is_enabled.is_(True),
    ]
    if requested_agent_id is not None:
        member_conditions.append(ProjectMemberSnapshot.agent_id == requested_agent_id)
    else:
        member_conditions.append(ProjectMemberSnapshot.is_leader.is_(True))
    member = (
        await db.execute(
            select(ProjectMemberSnapshot)
            .where(*member_conditions)
            .order_by(ProjectMemberSnapshot.is_leader.desc(), ProjectMemberSnapshot.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if member is None:
        if requested_agent_id is not None:
            raise HTTPException(
                status_code=422,
                detail="数字员工必须是已启用的项目成员",
            )
        raise HTTPException(status_code=422, detail="项目需要一名可用的负责人。")

    supplied_input = dict(data.input or {})
    task = str(
        supplied_input.get("task") or supplied_input.get("objective") or supplied_input.get("message") or ""
    ).strip()
    if work_item is not None:
        criteria = "\n".join(f"- {item}" for item in (work_item.acceptance_criteria or [])) or "- None recorded"
        work_context = (
            f"Execute project work item {work_item.id}: {work_item.title}\n\n"
            f"Description:\n{work_item.description or '(none)'}\n\n"
            f"Acceptance criteria:\n{criteria}"
        )
        task = f"{work_context}\n\nAdditional instruction:\n{task}" if task else work_context
    if not task:
        raise HTTPException(
            status_code=422,
            detail="请选择任务或填写执行内容。",
        )
    run_title = str(supplied_input.get("title") or supplied_input.get("objective") or "").strip()
    if work_item is not None:
        run_title = work_item.title
    if not run_title:
        run_title = task.splitlines()[0].strip()
    run_title = run_title[:120]

    group_session = await ensure_project_group_session(db, project)
    anchor = ChatMessage(
        id=uuid.uuid4(),
        agent_id=group_session.agent_id,
        user_id=current_user.id,
        sender_user_id=current_user.id,
        role="user",
        content=task,
        conversation_id=str(group_session.id),
        message_meta={
            "kind": "project_run_request",
            "project_id": str(project.id),
            "visible_to_group": True,
            "mentions": [str(member.agent_id)],
            "awakened_agent_ids": [],
            "wake_policy": "single_explicit_or_default_leader",
            "initiator_user_id": str(current_user.id),
            "target_agent_id": str(member.agent_id),
            "attachments": [],
        },
    )
    run = ProjectRun(
        tenant_id=project.tenant_id,
        project_id=project.id,
        work_item_id=data.work_item_id,
        agent_id=member.agent_id,
        initiated_by_user_id=current_user.id,
        execution_user_id=project_execution_user_id(project),
        status="queued",
        trigger_type=data.trigger_type,
        input={
            **supplied_input,
            "title": run_title,
            "group_session_id": str(group_session.id),
            "dispatch": {
                "group_session_id": str(group_session.id),
                "project_member_id": str(member.id),
                "turn_anchor_id": str(anchor.id),
                "task": task,
            },
        },
        output={"group_session_id": str(group_session.id)},
    )
    db.add_all([anchor, run])
    group_session.last_message_at = func.now()
    await db.flush()
    from app.services.project_group_turn_lifecycle import reconcile_project_group_turn

    await reconcile_project_group_turn(
        db,
        project_id=project.id,
        session=group_session,
    )
    await freeze_run_members(db, project, run)
    add_event(
        db,
        project,
        "run.queued",
        "Queued project run and froze run snapshots",
        actor_user_id=current_user.id,
        actor_agent_id=run.agent_id,
        work_item_id=run.work_item_id,
        run_id=run.id,
        metadata={
            "group_session_id": str(group_session.id),
            "target_agent_id": str(member.agent_id),
            "dispatch_policy": "single_agent",
        },
    )
    # This explicit commit creates the durable outbox boundary before child
    # creation. A daemon retry owns recovery if the process exits afterward.
    await db.commit()
    try:
        await dispatch_project_run(run.id)
    except Exception as exc:  # noqa: BLE001 - queued outbox remains retryable
        logger.warning("Project run dispatch deferred run=%s error=%s", run.id, exc)
    await db.refresh(run)
    return (await serialize_project_runs(db, project, [run]))[0]


@router.patch("/{project_id}/runs/{run_id}", response_model=ProjectRunOut)
async def patch_project_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    data: ProjectRunUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    run = (
        await db.execute(
            select(ProjectRun).where(
                ProjectRun.id == run_id, ProjectRun.project_id == project.id, ProjectRun.tenant_id == project.tenant_id
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Project run not found")
    updates = data.model_dump(exclude_unset=True)
    if "status" in updates:
        apply_run_status(run, updates.pop("status"))
    for key, value in updates.items():
        setattr(run, key, value)
    add_event(
        db,
        project,
        f"run.{run.status}",
        "Execution status updated",
        actor_user_id=current_user.id,
        actor_agent_id=run.agent_id,
        work_item_id=run.work_item_id,
        run_id=run.id,
    )
    await db.flush()
    terminal_group_run_id = (
        run.id if run.status in {"succeeded", "failed", "cancelled"} else None
    )
    if terminal_group_run_id is not None:
        # The websocket projection may only describe committed cohort state.
        await db.commit()
        from app.services.project_group_turn_lifecycle import (
            reconcile_and_publish_project_run_group_turn,
        )

        await reconcile_and_publish_project_run_group_turn(terminal_group_run_id)
    await db.refresh(run)
    return (await serialize_project_runs(db, project, [run]))[0]


@router.get("/{project_id}/runs/{run_id}/member-snapshots", response_model=list[ProjectRunMemberSnapshotOut])
async def list_run_snapshots(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    snapshots = (
        (
            await db.execute(
                select(ProjectRunMemberSnapshot)
                .where(
                    ProjectRunMemberSnapshot.run_id == run_id,
                    ProjectRunMemberSnapshot.project_id == project.id,
                    ProjectRunMemberSnapshot.tenant_id == project.tenant_id,
                )
                .order_by(ProjectRunMemberSnapshot.created_at)
            )
        )
        .scalars()
        .all()
    )
    return await serialize_project_run_member_snapshots(db, project, list(snapshots))


@router.get("/{project_id}/events", response_model=list[ProjectEventOut])
async def list_project_events(
    project_id: uuid.UUID,
    event_type: str | None = None,
    actor_agent_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    stmt = select(ProjectEvent).where(
        ProjectEvent.project_id == project.id, ProjectEvent.tenant_id == project.tenant_id
    )
    if event_type:
        stmt = stmt.where(ProjectEvent.event_type == event_type)
    if actor_agent_id:
        stmt = stmt.where(ProjectEvent.actor_agent_id == actor_agent_id)
    events = (await db.execute(stmt.order_by(ProjectEvent.created_at.desc()).limit(limit))).scalars().all()
    return await serialize_project_events(db, project, list(events))


@router.post("/{project_id}/events", response_model=ProjectEventOut, status_code=201)
async def create_project_event(
    project_id: uuid.UUID,
    data: ProjectEventCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    await _require_member_agent(db, project, data.actor_agent_id)
    event = add_event(
        db,
        project,
        data.event_type,
        data.summary,
        actor_user_id=current_user.id,
        actor_agent_id=data.actor_agent_id,
        work_item_id=data.work_item_id,
        run_id=data.run_id,
        metadata=data.metadata,
    )
    await db.flush()
    return (await serialize_project_events(db, project, [event]))[0]
