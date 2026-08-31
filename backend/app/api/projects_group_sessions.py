from app.api.projects_shared import *  # noqa: F401,F403

@router.get("/{project_id}/leader-session")
async def get_project_leader_session(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    session = await ensure_project_leader_session(db, project)
    discussion_count = int(
        (
            await db.execute(select(func.count(ChatMessage.id)).where(ChatMessage.conversation_id == str(session.id)))
        ).scalar_one()
    )
    await db.commit()
    await db.refresh(session)
    return _leader_session_payload(session, discussion_count)


@router.post("/{project_id}/kickoff/confirm", status_code=202)
async def confirm_project_kickoff(
    project_id: uuid.UUID,
    data: ProjectKickoffConfirm,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Freeze project-owner planning evidence and start one durable child.

    Confirmation is the only implicit wake in this flow and targets only the
    enabled project owner. Group messages remain append-only and structured mentions
    keep their zero-broadcast default.
    """
    from app.services.subagent_runtime import dispatch_project_run

    authorized_project = await require_project(db, current_user, project_id, edit=True)
    project = (
        await db.execute(select(Project).where(Project.id == authorized_project.id).with_for_update())
    ).scalar_one()
    if project.status == "initializing":
        pending_run = (
            await db.execute(
                select(ProjectRun)
                .where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.trigger_type == "leader_kickoff",
                    ProjectRun.status.in_(["queued", "running"]),
                )
                .order_by(ProjectRun.created_at.desc(), ProjectRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if pending_run is not None and dict((pending_run.input or {}).get("dispatch") or {}):
            await db.commit()
            dispatch_result = await dispatch_project_run(pending_run.id)
            refreshed = await db.get(ProjectRun, pending_run.id)
            event_id = (
                await db.execute(
                    select(ProjectEvent.id)
                    .where(
                        ProjectEvent.run_id == pending_run.id,
                        ProjectEvent.event_type == "project.kickoff.confirmed",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            payload = {
                **dict(pending_run.input or {}),
                **(dict(refreshed.output or {}) if refreshed else {}),
            }
            return {
                "status": "running" if dispatch_result.get("subagent_run_id") else "initializing",
                "project_id": str(project.id),
                "run_id": str(pending_run.id),
                "event_id": str(event_id) if event_id else None,
                "leader_session_id": payload.get("leader_session_id"),
                "group_session_id": payload.get("group_session_id"),
                "leader_agent_id": payload.get("leader_agent_id"),
                "discussion_source": payload.get("discussion_source", "leader_session"),
                "discussion_session_id": payload.get("discussion_session_id", payload.get("leader_session_id")),
                "awakened_agent_ids": [payload.get("leader_agent_id")]
                if dispatch_result.get("subagent_run_id")
                else [],
                "subagent_run_id": dispatch_result.get("subagent_run_id"),
                "subagent_session_id": dispatch_result.get("subagent_session_id"),
                "git_start_commit": payload.get("git_start_commit"),
                "transcript_path": payload.get("transcript_path"),
                "transcript_commit": payload.get("transcript_commit"),
                "recovered": True,
            }
    if project.status != "planning":
        raise HTTPException(status_code=409, detail="Only a planning project can be confirmed")

    leader = await ensure_enabled_project_leader(db, project)
    leader_session = await ensure_project_leader_session(db, project)
    group_session = await ensure_project_group_session(db, project)
    group_planning = _uses_project_group_planning(project)
    if group_planning:
        if await _has_active_project_planning_run(db, project):
            raise HTTPException(
                status_code=409,
                detail="Project planning is still being processed; confirm kickoff after the current turn completes",
            )
        discussion = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(group_session.id),
                        or_(
                            and_(ChatMessage.role == "user", ChatMessage.sender_user_id.is_not(None)),
                            and_(
                                ChatMessage.role == "assistant",
                                ChatMessage.sender_agent_id == leader.agent_id,
                            ),
                        ),
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        roles = {message.role for message in discussion}
        discussion_source = "project_group"
        discussion_session_id = group_session.id
    else:
        discussion = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(leader_session.id))
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        if _has_unfinished_direct_planning_turn(discussion):
            raise HTTPException(
                status_code=409,
                detail="Project planning is still being processed; confirm kickoff after the current turn completes",
            )
        roles = {message.role for message in discussion}
        if "user" not in roles or "assistant" not in roles:
            # Older projects may have moved planning into the canonical group
            # before the transport marker existed. Keep that migration path.
            discussion = (
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id == str(group_session.id),
                            or_(
                                and_(ChatMessage.role == "user", ChatMessage.sender_user_id.is_not(None)),
                                and_(
                                    ChatMessage.role == "assistant",
                                    ChatMessage.sender_agent_id == leader.agent_id,
                                ),
                            ),
                        )
                        .order_by(ChatMessage.created_at, ChatMessage.id)
                    )
                )
                .scalars()
                .all()
            )
            roles = {message.role for message in discussion}
            discussion_source = "project_group"
            discussion_session_id = group_session.id
        else:
            discussion_source = "leader_session"
            discussion_session_id = leader_session.id
    if _has_unfinished_direct_planning_turn(discussion):
        raise HTTPException(
            status_code=409,
            detail="Project planning has an unanswered Human message; wait for the project owner response",
        )
    if "user" not in roles or "assistant" not in roles:
        raise HTTPException(
            status_code=422,
            detail=(
                "Kickoff requires at least one Human message and one project owner response in planning or project chat"
            ),
        )

    confirmation = (
        data.confirmation or "确认当前方案并开始执行。"
    ).strip()
    confirmed_at = datetime.now(UTC)
    transcript = _kickoff_transcript(project, discussion, confirmation, confirmed_at)
    transcript_sha256 = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    await reconcile_project_repository_operations(project.id, db=db)
    git_start = await repository_state(project, limit=1)
    transcript_commit = await write_project_file(
        project,
        "docs/kickoff-transcript.md",
        transcript,
        author_name=current_user.display_name,
        author_email=project_user_git_email(current_user.id),
    )
    now = confirmed_at
    kickoff_message = ChatMessage(
        id=uuid.uuid4(),
        agent_id=group_session.agent_id,
        user_id=current_user.id,
        sender_user_id=current_user.id,
        role="user",
        content=f"项目方案已确认，负责人 @{leader.name_snapshot} 可以开始推进。",
        conversation_id=str(group_session.id),
        external_event_key=f"project-kickoff:{project.id}:{transcript_sha256}",
        message_meta={
            "kind": "project_kickoff_confirmation",
            "project_id": str(project.id),
            "visible_to_group": True,
            "mentions": [],
            "awakened_agent_ids": [],
            "wake_policy": "kickoff_leader_only",
            "initiator_user_id": str(current_user.id),
            "leader_agent_id": str(leader.agent_id),
            "transcript_path": "docs/kickoff-transcript.md",
            "transcript_commit": transcript_commit["commit"],
        },
        created_at=now,
    )
    task = build_project_kickoff_task(transcript)
    kickoff_snapshot = {
        "confirmed_at": confirmed_at.isoformat(),
        "confirmed_by_user_id": str(current_user.id),
        "leader_session_id": str(leader_session.id),
        "group_session_id": str(group_session.id),
        "leader_agent_id": str(leader.agent_id),
        "git_start_commit": git_start["head"],
        "transcript_path": "docs/kickoff-transcript.md",
        "transcript_commit": transcript_commit["commit"],
        "transcript_sha256": transcript_sha256,
        "discussion_source": discussion_source,
        "discussion_session_id": str(discussion_session_id),
    }
    run = ProjectRun(
        tenant_id=project.tenant_id,
        project_id=project.id,
        agent_id=leader.agent_id,
        initiated_by_user_id=current_user.id,
        execution_user_id=project_execution_user_id(project),
        status="queued",
        trigger_type="leader_kickoff",
        input={
            "title": f"启动项目：{project.name}"[:120],
            "leader_session_id": str(leader_session.id),
            "group_session_id": str(group_session.id),
            "leader_agent_id": str(leader.agent_id),
            "discussion_source": discussion_source,
            "discussion_session_id": str(discussion_session_id),
            "confirmation": confirmation,
            "conversation_snapshot": {
                "message_count": len(discussion),
                "message_ids": [str(message.id) for message in discussion],
                "last_message_at": discussion[-1].created_at.isoformat() if discussion[-1].created_at else None,
                "source": discussion_source,
                "session_id": str(discussion_session_id),
                "transcript_path": "docs/kickoff-transcript.md",
                "transcript_sha256": transcript_sha256,
            },
            "git_start_commit": git_start["head"],
            "transcript_path": "docs/kickoff-transcript.md",
            "transcript_commit": transcript_commit["commit"],
            "dispatch": {
                "group_session_id": str(group_session.id),
                "project_member_id": str(leader.id),
                "turn_anchor_id": str(kickoff_message.id),
                "task": task,
                "execution_tools_enabled": True,
                "kickoff": kickoff_snapshot,
            },
        },
        output={
            "group_session_id": str(group_session.id),
            "leader_session_id": str(leader_session.id),
            "transcript_path": "docs/kickoff-transcript.md",
            "transcript_commit": transcript_commit["commit"],
        },
    )
    db.add_all([kickoff_message, run])
    group_session.last_message_at = now
    project.status = "initializing"
    project.settings = {
        **dict(project.settings or {}),
        "planning": {
            **dict(dict(project.settings or {}).get("planning") or {}),
            "state": "confirmed",
            "launch_confirmed": True,
            "confirmed_at": confirmed_at.isoformat(),
            "confirmed_by_user_id": str(current_user.id),
        },
    }
    await db.flush()
    from app.services.conversation_turn_lifecycle import (
        transition_conversation_turn,
    )

    await transition_conversation_turn(
        db,
        agent_id=group_session.agent_id,
        conversation_id=str(group_session.id),
        turn_anchor_id=kickoff_message.id,
        status="running",
    )
    await freeze_run_members(db, project, run)
    await db.commit()
    try:
        dispatch_result = await dispatch_project_run(run.id)
    except Exception as exc:
        # The committed ProjectRun is the durable outbox. A daemon retry owns
        # recovery, so this response never rolls the project back to planning.
        logger.exception("Project kickoff dispatch is waiting for retry: project={} run={}", project.id, run.id)
        dispatch_result = {"status": "initializing", "error": "项目正在等待处理。"}
    event_id = (
        await db.execute(
            select(ProjectEvent.id)
            .where(
                ProjectEvent.run_id == run.id,
                ProjectEvent.event_type == "project.kickoff.confirmed",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    child_id = dispatch_result.get("subagent_run_id")
    return {
        "status": "running" if child_id else "initializing",
        "project_id": str(project.id),
        "run_id": str(run.id),
        "event_id": str(event_id) if event_id else None,
        "leader_session_id": str(leader_session.id),
        "group_session_id": str(group_session.id),
        "leader_agent_id": str(leader.agent_id),
        "discussion_source": discussion_source,
        "discussion_session_id": str(discussion_session_id),
        "awakened_agent_ids": [str(leader.agent_id)] if child_id else [],
        "subagent_run_id": str(child_id) if child_id else None,
        "subagent_session_id": str(child_id) if child_id else None,
        "git_start_commit": git_start["head"],
        "transcript_path": "docs/kickoff-transcript.md",
        "transcript_commit": transcript_commit["commit"],
    }


@router.get("/{project_id}/group-session")
async def get_project_group_session(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    session = await ensure_project_group_session(db, project)
    await db.commit()
    await db.refresh(session)
    return _group_session_payload(session, project)


@router.get("/{project_id}/group-sessions/{session_id}/messages")
async def list_project_group_messages(
    project_id: uuid.UUID,
    session_id: uuid.UUID,
    limit: int = Query(500, ge=1, le=500),
    before: str | None = Query(
        None,
        description="Cursor: ISO timestamp, optionally followed by |message UUID",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.project_id == project.id,
                ChatSession.source_channel == "project",
                ChatSession.is_group.is_(True),
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Project group session not found")
    messages_query = (
        select(ChatMessage)
        .where(
            ChatMessage.conversation_id == str(session.id),
            ChatMessage.message_meta["kind"].as_string().is_distinct_from(
                "project_subagent_external_continuation"
            ),
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )
    if before:
        try:
            before_timestamp, separator, before_message_id = before.partition("|")
            before_dt = datetime.fromisoformat(before_timestamp.replace("Z", "+00:00"))
            if separator:
                cursor_id = uuid.UUID(before_message_id)
                messages_query = messages_query.where(
                    or_(
                        ChatMessage.created_at < before_dt,
                        and_(
                            ChatMessage.created_at == before_dt,
                            ChatMessage.id < cursor_id,
                        ),
                    )
                )
            else:
                messages_query = messages_query.where(ChatMessage.created_at < before_dt)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="分页位置无效，请刷新后重试。",
            )
    newest_first = (await db.execute(messages_query.limit(limit + 1))).scalars().all()
    has_more = len(newest_first) > limit
    messages = list(reversed(newest_first[:limit]))
    oldest_message = messages[0] if messages else None
    from app.services.project_group_turn_lifecycle import reconcile_project_group_turn

    turn_projection = await reconcile_project_group_turn(
        db,
        project_id=project.id,
        session=session,
    )
    timeline = await build_project_group_timeline(
        db,
        project_id=project.id,
        group_messages=messages,
    )
    await db.commit()
    return {
        "session": _group_session_payload(session, project),
        "items": timeline,
        "turn": turn_projection.to_client_dict(),
        "has_more": has_more,
        "next_cursor": (
            f"{oldest_message.created_at.isoformat()}|{oldest_message.id}" if oldest_message is not None else None
        ),
    }
