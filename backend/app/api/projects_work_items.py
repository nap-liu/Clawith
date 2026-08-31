from app.api.projects_shared import *  # noqa: F401,F403

async def _require_member_agent(db: AsyncSession, project: Project, agent_id: uuid.UUID | None) -> None:
    if agent_id is None:
        return
    found = (
        await db.execute(
            select(ProjectMemberSnapshot.id).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == agent_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(
            status_code=422,
            detail="数字员工必须是已启用的项目成员",
        )


async def _validate_work_item_links(
    db: AsyncSession,
    project: Project,
    parent_id: uuid.UUID | None,
    dependency_ids: list[uuid.UUID],
    *,
    item_id: uuid.UUID | None = None,
) -> None:
    linked_ids = set(dependency_ids)
    if parent_id:
        linked_ids.add(parent_id)
    if item_id and item_id in linked_ids:
        raise HTTPException(status_code=422, detail="A work item cannot depend on or parent itself")
    if not linked_ids:
        return
    found_ids = set(
        (
            await db.execute(
                select(ProjectWorkItem.id).where(
                    ProjectWorkItem.id.in_(linked_ids),
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.tenant_id == project.tenant_id,
                )
            )
        ).scalars()
    )
    if found_ids != linked_ids:
        raise HTTPException(status_code=422, detail="Every parent and dependency must belong to this project")


@router.get("/{project_id}/work-items", response_model=list[WorkItemOut])
async def list_work_items(
    project_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    project = await require_project(db, current_user, project_id)
    return (
        (
            await db.execute(
                select(ProjectWorkItem)
                .where(ProjectWorkItem.project_id == project.id, ProjectWorkItem.tenant_id == project.tenant_id)
                .order_by(ProjectWorkItem.created_at)
            )
        )
        .scalars()
        .all()
    )


@router.get("/{project_id}/work-items/{work_item_id}", response_model=WorkItemDetailOut)
async def get_work_item_detail(
    project_id: uuid.UUID,
    work_item_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return one trace object; clients do not reconstruct links heuristically."""
    project = await require_project(db, current_user, project_id)
    item = (
        await db.execute(
            select(ProjectWorkItem).where(
                ProjectWorkItem.id == work_item_id,
                ProjectWorkItem.project_id == project.id,
                ProjectWorkItem.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    project_runs = (
        (
            await db.execute(
                select(ProjectRun)
                .where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                )
                .order_by(ProjectRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    all_run_payloads = await serialize_project_runs(db, project, list(project_runs))
    project_events = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(
                    ProjectEvent.project_id == project.id,
                    ProjectEvent.tenant_id == project.tenant_id,
                )
                .order_by(ProjectEvent.created_at.desc(), ProjectEvent.id.desc())
            )
        )
        .scalars()
        .all()
    )
    item_events = [event for event in project_events if event.work_item_id == item.id]
    explicit_run_ids = {
        str(value)
        for event in item_events
        for value in (
            event.run_id,
            dict(event.event_metadata or {}).get("project_run_id"),
            *(dict(event.event_metadata or {}).get("related_run_ids") or []),
        )
        if value
    }
    run_payloads = [
        run for run in all_run_payloads if run.get("work_item_id") == item.id or str(run["id"]) in explicit_run_ids
    ]
    run_ids = {str(run["id"]) for run in run_payloads}
    events = []
    for event in project_events:
        metadata = dict(event.event_metadata or {})
        event_run_ids = {
            str(value)
            for value in (
                event.run_id,
                metadata.get("project_run_id"),
                *(metadata.get("related_run_ids") or []),
            )
            if value
        }
        if event.work_item_id == item.id or bool(run_ids & event_run_ids):
            events.append(event)
    run_by_id = {str(run["id"]): run for run in run_payloads}
    trace_session_ids = {
        str(run.get("subagent_session_id") or run.get("session_id"))
        for run in run_payloads
        if run.get("subagent_session_id") or run.get("session_id")
    }
    trace_run_ids = {str(run["id"]) for run in run_payloads}
    trace_anchor_rows = (
        (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id.in_(trace_session_ids),
                    ChatMessage.role == "user",
                    ChatMessage.message_meta["project_run_id"].as_string().in_(trace_run_ids),
                )
            )
        )
        .scalars()
        .all()
        if trace_session_ids and trace_run_ids
        else []
    )
    trace_anchor_by_run_id = {
        str(dict(row.message_meta or {}).get("project_run_id")): str(row.id)
        for row in trace_anchor_rows
        if dict(row.message_meta or {}).get("project_run_id")
    }
    sessions = []
    for run in run_payloads:
        if not (run.get("session_id") or run.get("subagent_session_id")):
            continue
        run_input = dict(run.get("input") or {})
        run_dispatch = dict(run_input.get("dispatch") or {})
        anchor_message_id = trace_anchor_by_run_id.get(str(run["id"])) or (
            str(run_dispatch.get("turn_anchor_id") or run_input.get("turn_anchor_id") or "").strip() or None
        )
        sessions.append(
            {
                "run_id": str(run["id"]),
                "project_run_id": str(run["id"]),
                "work_item_id": str(item.id),
                "agent_id": str(run["agent_id"]) if run.get("agent_id") else None,
                "agent_name": run.get("agent_name"),
                "status": run["status"],
                "source_channel": "agent" if run["trigger_type"] == "a2a" else "subagent",
                "session_intent": "a2a" if run["trigger_type"] == "a2a" else "execution",
                "session_id": str(run["session_id"]) if run.get("session_id") else None,
                "subagent_session_id": (str(run["subagent_session_id"]) if run.get("subagent_session_id") else None),
                "anchor_message_id": anchor_message_id,
            }
        )
    known_session_ids = {row["session_id"] for row in sessions if row.get("session_id")}
    session_actor_ids = {event.actor_agent_id for event in item_events if event.actor_agent_id is not None}
    session_members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id.in_(session_actor_ids),
                )
            )
        )
        .scalars()
        .all()
        if session_actor_ids
        else []
    )
    session_member_names = {str(member.agent_id): member.name_snapshot for member in session_members}
    for event in item_events:
        metadata = dict(event.event_metadata or {})
        event_session_id = str(metadata.get("session_id") or "").strip()
        if not event_session_id or event_session_id in known_session_ids:
            continue
        sessions.append(
            {
                "run_id": str(event.run_id) if event.run_id else metadata.get("project_run_id"),
                "project_run_id": str(event.run_id) if event.run_id else metadata.get("project_run_id"),
                "work_item_id": str(item.id),
                "agent_id": str(event.actor_agent_id) if event.actor_agent_id else None,
                "agent_name": session_member_names.get(str(event.actor_agent_id)),
                "status": None,
                "source_channel": "subagent",
                "session_intent": "evidence",
                "session_id": event_session_id,
                "subagent_session_id": metadata.get("subagent_session_id") or event_session_id,
                "anchor_message_id": metadata.get("anchor_message_id")
                or metadata.get("turn_anchor_id")
                or metadata.get("subagent_turn_anchor_id"),
            }
        )
        known_session_ids.add(event_session_id)
    commits_by_hash: dict[str, dict] = {}
    files_by_path: dict[str, dict] = {}
    evidence: list[dict] = []
    for event in events:
        metadata = dict(event.event_metadata or {})
        linked_run = run_by_id.get(str(event.run_id)) if event.run_id else None
        trace = {
            "event_id": str(event.id),
            "run_id": str(event.run_id) if event.run_id else None,
            "work_item_id": str(event.work_item_id or item.id),
            "session_id": metadata.get("session_id")
            or (str(linked_run["session_id"]) if linked_run and linked_run.get("session_id") else None),
            "subagent_session_id": metadata.get("subagent_session_id")
            or (
                str(linked_run["subagent_session_id"]) if linked_run and linked_run.get("subagent_session_id") else None
            ),
        }
        commit = str(metadata.get("commit") or metadata.get("commit_hash") or "").strip()
        if commit:
            commits_by_hash.setdefault(
                commit,
                {
                    **trace,
                    "commit": commit,
                    "short_commit": commit[:12],
                    "message": metadata.get("message") or event.summary,
                    "event_type": event.event_type,
                    "created_at": event.created_at,
                    # Populated from Git below. Event metadata describes the
                    # requested operation and is not authoritative evidence of
                    # what the commit actually changed (an empty milestone can
                    # legitimately carry a non-empty requested path list).
                    "paths": [],
                },
            )
        for value in metadata.get("evidence") or []:
            evidence.append({**trace, "kind": "evidence", "value": str(value), "created_at": event.created_at})
        if metadata.get("progress_note"):
            evidence.append(
                {
                    **trace,
                    "kind": "progress_note",
                    "value": str(metadata["progress_note"]),
                    "created_at": event.created_at,
                }
            )
    # Only commits reached through the explicit work-item/run relation above
    # are inspected. The file list is derived from the repository diff, never
    # from event ``path(s)`` hints, so unrelated repository files and requested
    # paths on an empty milestone cannot leak into this detail contract.
    for commit_hash, commit_record in commits_by_hash.items():
        try:
            diff = await project_commit_diff(project, commit_hash, max_patch_bytes=1)
        except (HTTPException, RuntimeError) as exc:
            logger.warning(
                "Unable to resolve work-item Git diff project={} work_item={} commit={}: {}",
                project.id,
                item.id,
                commit_hash,
                exc,
            )
            commit_record["diff_available"] = False
            continue
        changed_files = list(diff.get("files") or [])
        commit_record["paths"] = [str(file["path"]) for file in changed_files if file.get("path")]
        commit_record["diff_available"] = True
        commit_record["files_truncated"] = bool(diff.get("files_truncated"))
        for changed_file in changed_files:
            path = str(changed_file.get("path") or "").strip()
            if not path:
                continue
            # Events and commits are newest-first. Keep the newest explicit
            # commit for a path while the separate commit list preserves the
            # full history for the work item.
            files_by_path.setdefault(
                path,
                {
                    "event_id": commit_record.get("event_id"),
                    "run_id": commit_record.get("run_id"),
                    "work_item_id": commit_record.get("work_item_id"),
                    "session_id": commit_record.get("session_id"),
                    "subagent_session_id": commit_record.get("subagent_session_id"),
                    "path": path,
                    "commit": commit_hash,
                    "status": changed_file.get("status"),
                    "additions": changed_file.get("additions"),
                    "deletions": changed_file.get("deletions"),
                    "binary": bool(changed_file.get("binary")),
                    "created_at": commit_record.get("created_at"),
                },
            )
    for run in run_payloads:
        output = dict(run.get("output") or {})
        value = output.get("result") or run.get("error")
        if value:
            evidence.append(
                {
                    "kind": "run_result" if output.get("result") else "run_error",
                    "value": str(value),
                    "run_id": str(run["id"]),
                    "work_item_id": str(item.id),
                    "session_id": str(run["session_id"]) if run.get("session_id") else None,
                    "subagent_session_id": (
                        str(run["subagent_session_id"]) if run.get("subagent_session_id") else None
                    ),
                    "created_at": run.get("finished_at") or run["updated_at"],
                }
            )
    return {
        "work_item": item,
        "runs": run_payloads,
        "sessions": sessions,
        "events": await serialize_project_events(db, project, list(events)),
        "commits": list(commits_by_hash.values()),
        "files": list(files_by_path.values()),
        "evidence": evidence,
    }


@router.post("/{project_id}/work-items", response_model=WorkItemOut, status_code=201)
async def create_work_item(
    project_id: uuid.UUID,
    data: WorkItemCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    await _require_member_agent(db, project, data.assignee_agent_id)
    await _validate_work_item_links(db, project, data.parent_id, data.dependency_ids)
    values = data.model_dump()
    values["dependency_ids"] = [str(value) for value in data.dependency_ids]
    item = ProjectWorkItem(
        tenant_id=project.tenant_id,
        project_id=project.id,
        created_by_user_id=current_user.id,
        **values,
    )
    db.add(item)
    await db.flush()
    add_event(
        db,
        project,
        "work_item.created",
        f"Created work item {item.title}",
        actor_user_id=current_user.id,
        work_item_id=item.id,
        metadata={
            "status": item.status,
            "priority": item.priority,
            "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
        },
    )
    await db.flush()
    await db.refresh(item)
    return item


@router.patch("/{project_id}/work-items/{work_item_id}", response_model=WorkItemOut)
async def patch_work_item(
    project_id: uuid.UUID,
    work_item_id: uuid.UUID,
    data: WorkItemUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    item = (
        await db.execute(
            select(ProjectWorkItem).where(
                ProjectWorkItem.id == work_item_id,
                ProjectWorkItem.project_id == project.id,
                ProjectWorkItem.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    before = {
        "title": item.title,
        "status": item.status,
        "priority": item.priority,
        "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
    }
    updates = data.model_dump(exclude_unset=True)
    if data.dependency_ids is not None:
        updates["dependency_ids"] = [str(value) for value in data.dependency_ids]
    await _require_member_agent(db, project, updates.get("assignee_agent_id"))
    if data.dependency_ids is not None:
        await _validate_work_item_links(db, project, item.parent_id, data.dependency_ids, item_id=item.id)
    for key, value in updates.items():
        setattr(item, key, value)
    after = {
        "title": item.title,
        "status": item.status,
        "priority": item.priority,
        "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
    }
    changed_fields = sorted(key for key in after if before[key] != after[key])
    if data.dependency_ids is not None:
        changed_fields.append("dependency_ids")
    add_event(
        db,
        project,
        "work_item.updated",
        f"Updated work item {item.title}",
        actor_user_id=current_user.id,
        work_item_id=item.id,
        metadata={"before": before, "after": after, "changed_fields": changed_fields},
    )
    await db.flush()
    await db.refresh(item)
    return item
