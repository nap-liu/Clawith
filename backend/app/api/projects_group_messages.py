from app.api.projects_shared import *  # noqa: F401,F403
from app.api.projects_work_items import _require_member_agent

@router.post("/{project_id}/group-sessions/{session_id}/messages", status_code=201)
async def create_project_group_message(
    project_id: uuid.UUID,
    session_id: uuid.UUID,
    data: ProjectGroupMessageCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Append a group message, route Human input to the project owner, and wake mentions."""
    from app.services.subagent_runtime import dispatch_project_run

    project = await require_project(db, current_user, project_id, edit=True)
    ensure_project_accepts_group_message(project)
    if data.sender_agent_id is not None:
        raise HTTPException(
            status_code=422,
            detail="当前请求不能以数字员工身份发送消息。",
        )
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.project_id == project.id,
                ChatSession.source_channel == "project",
                ChatSession.is_group.is_(True),
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Project group session not found")

    mention_ids = list(dict.fromkeys(data.mentions))
    if project.status in {"planning", "paused", "waiting", "completed"} and mention_ids:
        raise HTTPException(
            status_code=422,
            detail=(
                "Discussion outside active execution is handled by the project owner"
            ),
        )
    leader = await ensure_enabled_project_leader(db, project)
    default_leader_agent_id = leader.agent_id
    policies = dict((project.settings or {}).get("policies") or {})
    mention_limit = min(8, max(1, int(policies.get("max_group_mentions_per_message", 4))))
    configured_wake_budget = max(0, int(policies.get("max_a2a_wakes", mention_limit)))
    wake_budget = max(1, configured_wake_budget)
    if len(mention_ids) > mention_limit:
        raise HTTPException(
            status_code=422,
            detail=f"Structured mentions exceed this project's per-message limit ({mention_limit})",
        )
    wake_agent_ids = list(dict.fromkeys([default_leader_agent_id, *mention_ids]))
    if len(wake_agent_ids) > wake_budget:
        raise HTTPException(
            status_code=422,
            detail=f"Project owner and mentions exceed this project's per-message wake budget ({wake_budget})",
        )
    required_agent_ids = set(wake_agent_ids)
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id.in_(required_agent_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    member_by_agent = {member.agent_id: member for member in members}
    if set(member_by_agent) != required_agent_ids:
        raise HTTPException(status_code=422, detail="Every sender and mention must be a project member")
    disabled = [agent_id for agent_id in mention_ids if not member_by_agent[agent_id].is_enabled]
    if disabled:
        raise HTTPException(status_code=422, detail="Disabled project members cannot be awakened")

    event_key = f"project-group:{project.id}:{data.client_message_id}" if data.client_message_id else None
    existing = None
    if event_key:
        existing = (
            await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == event_key))
        ).scalar_one_or_none()
    if existing is not None:
        existing_work_item_id = dict(existing.message_meta or {}).get("work_item_id")
        if str(existing_work_item_id or "") != str(data.work_item_id or ""):
            raise HTTPException(status_code=409, detail="client_message_id already refers to another work item")
        pending_runs = (
            (
                await db.execute(
                    select(ProjectRun).where(
                        ProjectRun.project_id == project.id,
                        ProjectRun.input["group_message_id"].as_string() == str(existing.id),
                        ProjectRun.trigger_type.in_(["group_leader_message", "group_mention"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        from app.services.project_group_turn_lifecycle import reconcile_project_group_turn

        turn_projection = await reconcile_project_group_turn(
            db,
            project_id=project.id,
            session=session,
        )
        await db.commit()
        for pending_run in pending_runs:
            if not dict(pending_run.output or {}).get("subagent_run_id"):
                await dispatch_project_run(pending_run.id)
        await db.refresh(existing)
        turn_projection = await reconcile_project_group_turn(
            db,
            project_id=project.id,
            session=session,
        )
        await db.commit()
        meta = dict(existing.message_meta or {})
        return {
            "message": _group_message_payload(existing),
            "awakened_agent_ids": meta.get("awakened_agent_ids", []),
            "default_leader_agent_id": meta.get("default_leader_agent_id"),
            "subagent_runs": meta.get("subagent_runs", []),
            "turn": turn_projection.to_client_dict(),
            "idempotent_replay": True,
        }

    from app.services.project_group_turn_lifecycle import (
        find_project_group_blocking_confirmation,
    )

    blocking_confirmation = await find_project_group_blocking_confirmation(
        db,
        project_id=project.id,
        session_id=session.id,
    )
    if blocking_confirmation is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "project_group_confirmation_pending",
                "message": "请先完成当前待确认操作。",
                "call_id": str(blocking_confirmation.row_id),
                "client_message_id": data.client_message_id,
            },
        )

    message = ChatMessage(
        agent_id=session.agent_id,
        user_id=current_user.id,
        sender_user_id=current_user.id,
        sender_agent_id=None,
        role="user",
        content=data.content.strip(),
        conversation_id=str(session.id),
        external_event_key=event_key,
        message_meta={
            "kind": "project_group_message",
            "project_id": str(project.id),
            "visible_to_group": True,
            "mentions": [str(agent_id) for agent_id in mention_ids],
            "default_leader_agent_id": str(default_leader_agent_id),
            "attachments": data.attachments,
            "work_item_id": str(data.work_item_id) if data.work_item_id else None,
            "awakened_agent_ids": [],
            "subagent_runs": [],
            "wake_policy": "default_leader_plus_structured_mentions",
            "initiator_user_id": str(current_user.id),
        },
    )
    db.add(message)
    session.last_message_at = func.now()
    await db.flush()
    execution_content = (data.llm_content or data.content).strip()
    if not execution_content:
        execution_content = "处理项目群聊中附带的文件，并把结论回复到项目群。附件：" + str(data.attachments)
    from app.services.project_collaboration_prompt import (
        build_project_group_task,
        build_project_planning_task,
        build_project_read_only_conversation_task,
    )

    message_title = execution_content.splitlines()[0].strip()[:96] or "处理项目群聊消息"
    project_runs: list[ProjectRun] = []
    for agent_id in wake_agent_ids:
        member = member_by_agent[agent_id]
        project_run = ProjectRun(
            tenant_id=project.tenant_id,
            project_id=project.id,
            work_item_id=data.work_item_id,
            agent_id=agent_id,
            initiated_by_user_id=current_user.id,
            execution_user_id=project_execution_user_id(project),
            status="queued",
            trigger_type="group_leader_message" if agent_id == default_leader_agent_id else "group_mention",
            input={
                "title": (
                    f"处理群聊：{message_title}"
                    if agent_id == default_leader_agent_id
                    else f"响应提及：{message_title}"
                )[:120],
                "group_session_id": str(session.id),
                "group_message_id": str(message.id),
                "mentioned_agent_id": str(agent_id),
                "wake_reason": "default_leader" if agent_id == default_leader_agent_id else "structured_mention",
                "initiator_user_id": str(current_user.id),
                "work_item_id": str(data.work_item_id) if data.work_item_id else None,
                "dispatch": {
                    "group_session_id": str(session.id),
                    "project_member_id": str(member.id),
                    "turn_anchor_id": str(message.id),
                    "execution_tools_enabled": project.status == "running",
                    "read_only_conversation": project.status in {"paused", "waiting", "completed"},
                    "task": (
                        build_project_planning_task(execution_content)
                        if project.status == "planning"
                        else build_project_read_only_conversation_task(
                            execution_content,
                            status=project.status,
                        )
                        if project.status in {"paused", "waiting", "completed"}
                        else build_project_group_task(
                            execution_content,
                            is_owner=agent_id == default_leader_agent_id,
                        )
                    ),
                },
            },
            output={"group_session_id": str(session.id)},
        )
        db.add(project_run)
        await db.flush()
        await freeze_run_members(db, project, project_run)
        project_runs.append(project_run)
    from app.services.project_group_turn_lifecycle import (
        publish_project_group_turn_event,
        reconcile_project_group_turn,
    )

    admitted_turn = await reconcile_project_group_turn(
        db,
        project_id=project.id,
        session=session,
    )
    # Message and every target run form one durable outbox transaction. The
    # Subagent daemon can recover all rows after a process exit.
    await db.commit()
    await publish_project_group_turn_event(
        session=session,
        projection=admitted_turn,
        payload={
            **_group_message_payload(message),
            "type": "user_message_committed",
            "client_message_id": data.client_message_id,
            "message_id": str(message.id),
        },
        event_kind="turn_user_committed",
    )

    awakened: list[str] = []
    subagent_rows: list[dict] = []
    for project_run in project_runs:
        agent_id = project_run.agent_id
        try:
            result = await dispatch_project_run(project_run.id)
            run_id = result.get("subagent_run_id")
            run_status = result.get("status", "queued")
            if run_id:
                awakened.append(str(agent_id))
            subagent_rows.append(
                {
                    "project_run_id": str(project_run.id),
                    "run_id": str(run_id) if run_id else None,
                    "session_id": str(run_id) if run_id else None,
                    "agent_id": str(agent_id),
                    "status": run_status,
                }
            )
        except Exception as exc:
            # Keep the durable queued run retryable; the daemon owns recovery.
            logger.exception(
                "Project member dispatch is waiting for retry: project={} run={} agent={}",
                project.id,
                project_run.id,
                agent_id,
            )
            subagent_rows.append(
                {
                    "project_run_id": str(project_run.id),
                    "run_id": None,
                    "session_id": None,
                    "agent_id": str(agent_id),
                    "status": "queued",
                    "error": "任务正在等待处理。",
                }
            )

    message = await db.get(ChatMessage, message.id, with_for_update=True)
    if message is None:
        raise HTTPException(status_code=500, detail="Group message disappeared during wake dispatch")
    # Dispatch executes in its own transaction and may defer the owner when
    # the Human addressed specialists explicitly. Refresh the cached row so
    # the API response exposes that durable routing decision immediately.
    await db.refresh(message)
    recovered_meta = dict(message.message_meta or {})
    awakened = list(dict.fromkeys([*recovered_meta.get("awakened_agent_ids", []), *awakened]))
    recovered_rows = list(recovered_meta.get("subagent_runs", []))
    for row in subagent_rows:
        if not any(str(existing_row.get("project_run_id")) == row["project_run_id"] for existing_row in recovered_rows):
            recovered_rows.append(row)
    message.message_meta = {**recovered_meta, "awakened_agent_ids": awakened, "subagent_runs": recovered_rows}
    subagent_rows = recovered_rows
    event = (
        await db.execute(
            select(ProjectEvent)
            .where(
                ProjectEvent.project_id == project.id,
                ProjectEvent.event_type == "group.message.created",
                ProjectEvent.event_metadata["group_message_id"].as_string() == str(message.id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    event_metadata = {
        "group_session_id": str(session.id),
        "group_message_id": str(message.id),
        "work_item_id": str(data.work_item_id) if data.work_item_id else None,
        "initiator_user_id": str(current_user.id),
        "visible_to_group": True,
        "mentioned_agent_ids": [str(agent_id) for agent_id in mention_ids],
        "default_leader_agent_id": str(default_leader_agent_id),
        "awakened_agent_ids": awakened,
        "subagent_runs": subagent_rows,
        "zero_wake_default": False,
    }
    if event is None:
        event = add_event(
            db,
            project,
            "group.message.created",
            "Appended project group message and dispatched its selected project members",
            actor_user_id=current_user.id,
            actor_agent_id=None,
            work_item_id=data.work_item_id,
            metadata=event_metadata,
        )
    else:
        event.event_metadata = event_metadata
    from app.services.project_group_turn_lifecycle import reconcile_project_group_turn

    turn_projection = await reconcile_project_group_turn(
        db,
        project_id=project.id,
        session=session,
    )
    await db.flush()
    await db.commit()
    await db.refresh(message)
    return {
        "message": _group_message_payload(message),
        "event_id": str(event.id),
        "awakened_agent_ids": awakened,
        "default_leader_agent_id": str(default_leader_agent_id),
        "subagent_runs": subagent_rows,
        "turn": turn_projection.to_client_dict(),
    }


@router.post("/{project_id}/a2a", status_code=202)
async def wake_project_agent(
    project_id: uuid.UUID,
    data: A2AWakeRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    ensure_project_running(project)
    if data.from_agent_id == data.to_agent_id:
        raise HTTPException(status_code=422, detail="from_agent_id and to_agent_id must differ")
    await _require_member_agent(db, project, data.from_agent_id)
    await _require_member_agent(db, project, data.to_agent_id)
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
        raise HTTPException(status_code=422, detail="Work item is not in this project")
    dependency_ids = {uuid.UUID(str(value)) for value in (work_item.dependency_ids or [])}
    if dependency_ids:
        dependency_rows = (
            (
                await db.execute(
                    select(ProjectWorkItem).where(
                        ProjectWorkItem.project_id == project.id,
                        ProjectWorkItem.tenant_id == project.tenant_id,
                        ProjectWorkItem.id.in_(dependency_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        unfinished = [item.title for item in dependency_rows if item.status != "done"]
        missing = len(dependency_rows) != len(dependency_ids)
        if unfinished or missing:
            labels = ", ".join(unfinished) or "missing dependency records"
            raise HTTPException(
                status_code=409,
                detail=f"该任务的前置任务尚未完成，暂不能发起成员协作：{labels}",
            )
    run_title = data.title.strip()[:120]
    expected_output = data.expected_output.strip()
    message = data.message.strip()
    if not run_title or not message or not expected_output:
        raise HTTPException(
            status_code=422,
            detail="title, message and expected_output must contain non-whitespace text",
        )
    actionable_message = f"{message}\n\nExpected output: {expected_output}"
    group_session = await ensure_project_group_session(db, project)
    run = ProjectRun(
        tenant_id=project.tenant_id,
        project_id=project.id,
        work_item_id=data.work_item_id,
        agent_id=data.to_agent_id,
        initiated_by_user_id=current_user.id,
        execution_user_id=project_execution_user_id(project),
        status="queued",
        trigger_type="a2a",
        input={
            "title": run_title,
            "from_agent_id": str(data.from_agent_id),
            "to_agent_id": str(data.to_agent_id),
            "message": actionable_message,
            "mode": data.mode,
            "expected_output": expected_output,
            "new_conversation": data.new_conversation,
            "connector": "agent_tools.send_message_to_agent",
            "group_session_id": str(group_session.id),
        },
    )
    db.add(run)
    await db.flush()
    await freeze_run_members(db, project, run)
    event = add_event(
        db,
        project,
        "a2a.queued",
        f"Queued project collaboration action: {run_title}",
        actor_user_id=current_user.id,
        actor_agent_id=data.from_agent_id,
        from_agent_id=data.from_agent_id,
        to_agent_id=data.to_agent_id,
        work_item_id=data.work_item_id,
        run_id=run.id,
        metadata={
            "mode": data.mode,
            "title": run_title,
            "message": message,
            "expected_output": expected_output,
            "delivery": "queued",
            "connector": "agent_tools.send_message_to_agent",
            "group_session_id": str(group_session.id),
        },
    )
    await db.flush()
    await db.commit()
    background_tasks.add_task(deliver_project_a2a, run.id)
    return {
        "status": "queued",
        "run_id": str(run.id),
        "event_id": str(event.id),
        "from_agent_id": str(data.from_agent_id),
        "to_agent_id": str(data.to_agent_id),
        "group_session_id": str(group_session.id),
        "delivery_contract": "The existing A2A sender can consume this queued run without leader relay.",
    }
