from app.api.projects_shared import *  # noqa: F401,F403

@router.get("/{project_id}/git/remotes")
async def get_git_remotes(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    return {"items": await list_git_remotes(project)}


@router.put("/{project_id}/git/remotes/{name}")
async def put_project_git_remote(
    project_id: uuid.UUID,
    name: str,
    data: GitRemoteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await put_git_remote(project, name, data.url)
    remotes = await list_git_remotes(project)
    _record_git_repository_settings(project, remotes=remotes)
    event = add_event(
        db,
        project,
        "git.remote.configured",
        f"Configured Git remote: {result['name']}",
        actor_user_id=current_user.id,
        metadata=_git_remote_audit_metadata(result, history_changed=False),
    )
    await db.flush()
    return {**result, "event_id": str(event.id)}


@router.delete("/{project_id}/git/remotes/{name}")
async def delete_project_git_remote(
    project_id: uuid.UUID,
    name: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await delete_git_remote(project, name)
    remotes = await list_git_remotes(project)
    _record_git_repository_settings(project, remotes=remotes)
    event = add_event(
        db,
        project,
        "git.remote.deleted",
        f"Deleted Git remote: {result['name']}",
        actor_user_id=current_user.id,
        metadata=_git_remote_audit_metadata(result, history_changed=False),
    )
    await db.flush()
    return {**result, "event_id": str(event.id)}


@router.post("/{project_id}/git/clone")
async def clone_project_git_repository(
    project_id: uuid.UUID,
    data: GitCloneRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    operation = await begin_project_repository_clone(project, data.url, data.branch)
    result = operation.result
    journal = ProjectRepositoryOperation(
        id=operation.id,
        tenant_id=project.tenant_id,
        project_id=project.id,
        operation_type="clone",
        state="prepared",
        old_head=operation.old_head,
        new_head=operation.new_head,
        backup_name=operation.backup.name,
        staging_name=operation.staging_root.name,
    )
    db.add(journal)
    try:
        # The journal must win its own transaction before the filesystem can
        # move. A process exit after this point is repaired on next Git access.
        await db.commit()
        await apply_project_repository_clone(operation)
        _record_git_repository_settings(
            project,
            source="cloned",
            head=str(result["head"]),
            default_branch=str(result["default_branch"]),
            remotes=list(result["remotes"]),
        )
        event = add_event(
            db,
            project,
            "git.repository.cloned",
            f"Cloned project repository at {str(result['head'])[:12]}",
            actor_user_id=current_user.id,
            metadata={
                **_git_remote_audit_metadata(
                    {"name": "origin", "url": result["url"]},
                    history_changed=True,
                ),
                "operation": "clone",
                "head": result["head"],
                "default_branch": result["default_branch"],
            },
        )
        journal.state = "committed"
        await db.flush()
        # The filesystem backup cannot be finalized by the dependency's
        # post-response commit: a commit failure there would be too late to
        # compensate. Commit the settings and audit event inside this unit.
        await db.commit()
    except BaseException as exc:  # noqa: BLE001 - cancellation must preserve the durable state machine
        persisted: ProjectRepositoryOperation | None = None
        state_known = False
        try:
            await db.rollback()
            persisted = await db.get(ProjectRepositoryOperation, operation.id)
            state_known = True
        except BaseException as state_exc:  # noqa: BLE001 - do not guess an ambiguous commit result
            logger.warning("Could not read project clone journal operation={}: {}", operation.id, state_exc)
        if persisted is not None and persisted.state == "committed":
            # The metadata transaction won even though the client observed an
            # exception (for example a disconnect after server-side COMMIT).
            # Keep the new repository and let this or the next access finish
            # cleanup; rolling it back would contradict durable project state.
            try:
                await finalize_project_repository_clone(operation)
            except BaseException as cleanup_exc:  # noqa: BLE001 - committed journal owns deferred cleanup
                logger.warning("Deferred ambiguous project clone cleanup operation={}: {}", operation.id, cleanup_exc)
        elif state_known:
            try:
                await rollback_project_repository_clone(operation)
            finally:
                try:
                    if persisted is not None:
                        await db.delete(persisted)
                        await db.commit()
                except BaseException:  # noqa: BLE001 - preserve the original operation failure
                    await db.rollback()
        else:
            # A DB outage leaves the commit result genuinely unknown. Release
            # only the lock: the durable journal decides recovery on access.
            await release_project_repository_clone_lock(operation)
        if isinstance(exc, IntegrityError):
            raise HTTPException(status_code=409, detail="A repository operation is already in progress") from exc
        raise
    finalized = False
    try:
        await finalize_project_repository_clone(operation)
        finalized = True
    except Exception as exc:  # noqa: BLE001 - committed journal owns deferred cleanup
        # DB state is authoritative after ``committed``. Keep the journal so a
        # later repository access can retry backup cleanup.
        logger.warning("Deferred committed project clone cleanup operation={}: {}", operation.id, exc)
    if finalized:
        try:
            persisted = await db.get(ProjectRepositoryOperation, operation.id)
            if persisted is not None:
                await db.delete(persisted)
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - committed journal remains recoverable
            await db.rollback()
            logger.warning("Deferred project clone journal deletion operation={}: {}", operation.id, exc)
    return {**result, "event_id": str(event.id)}


@router.post("/{project_id}/git/restore")
async def create_git_restore(
    project_id: uuid.UUID,
    data: GitRestoreRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Restoring an arbitrary tree may replace project-owned Agent identity
    # files, so this repository-wide mutation is owner-only.
    project = await require_owner(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await restore_as_new_commit(
        project,
        data.commit,
        data.message,
        author_name=current_user.display_name,
        author_email=project_user_git_email(current_user.id),
    )
    _record_git_head(project, result["commit"])
    event = add_event(
        db,
        project,
        "git.restore_commit.created",
        "项目版本已恢复",
        actor_user_id=current_user.id,
        metadata={**result, "forbidden_operations": ["reset", "force_push"]},
    )
    await db.flush()
    return {**result, "event_id": str(event.id)}


@router.get("/{project_id}/files")
async def get_project_files(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    return await list_project_files(project)


@router.get("/{project_id}/files/content")
async def get_project_file_content(
    project_id: uuid.UUID,
    path: str = Query(min_length=1, max_length=1024),
    max_chars: int = Query(default=200_000, ge=1, le=1024 * 1024),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return HEAD metadata and a bounded text preview plus signed media URLs."""

    project = await require_project(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await read_project_file_content(project, path, max_chars=max_chars)
    ticket = _create_project_file_ticket(current_user, project, result)
    encoded_path = quote(result["path"], safe="")
    encoded_ticket = quote(ticket, safe="")
    raw_url = f"/api/projects/{project.id}/files/raw?path={encoded_path}&ticket={encoded_ticket}"
    html_preview_url = None
    if result["is_text"] and result["mime_type"] == "text/html":
        preview_ticket = _create_project_snapshot_ticket(
            current_user,
            project,
            purpose="project_html_preview",
            path=result["path"],
            head=result["head"],
        )
        html_preview_url = (
            f"/api/projects/{project.id}/files/preview/"
            f"{quote(preview_ticket, safe='')}/{quote(result['path'], safe='/')}"
        )
    return {
        **result,
        "raw_url": raw_url,
        "download_url": f"{raw_url}&download=true",
        "html_preview_url": html_preview_url,
        "ticket_expires_in": _PROJECT_FILE_TICKET_TTL_SECONDS,
    }


@router.api_route("/{project_id}/files/raw", methods=["GET", "HEAD"])
async def get_project_file_raw(
    project_id: uuid.UUID,
    request: Request,
    path: str = Query(min_length=1, max_length=1024),
    ticket: str = Query(min_length=1),
    download: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Stream one immutable HEAD blob, including RFC single-range requests."""

    _user, project, metadata = await _authorize_project_file_ticket(db, project_id, path, ticket)
    size = int(metadata["size"])
    etag = f'"{metadata["object_id"]}"'
    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if range_header and if_range and if_range.strip() != etag:
        range_header = None
    try:
        start, end, partial = _parse_project_file_range(range_header, size)
    except (TypeError, ValueError):
        return Response(
            status_code=416,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "Content-Range": f"bytes */{size}",
                "Cross-Origin-Resource-Policy": "same-origin",
                "ETag": etag,
                "X-Content-Type-Options": "nosniff",
            },
        )

    content_length = max(0, end - start + 1)
    disposition = "attachment" if download else "inline"
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(metadata['name'], safe='')}",
        "Content-Length": str(content_length),
        "Cross-Origin-Resource-Policy": "same-origin",
        "ETag": etag,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    status_code = 206 if partial else 200
    if request.method == "HEAD" or content_length == 0:
        return Response(status_code=status_code, media_type=metadata["mime_type"], headers=headers)
    return StreamingResponse(
        iter_project_file_blob(project, metadata["object_id"], start=start, end=end),
        status_code=status_code,
        media_type=metadata["mime_type"],
        headers=headers,
    )


@router.get("/{project_id}/files/archive")
async def get_project_directory_archive_ticket(
    project_id: uuid.UUID,
    path: str = Query(default="", max_length=1024),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a short-lived download URL for one immutable HEAD directory."""

    project = await require_project(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    snapshot = await inspect_project_directory(project, path)
    ticket = _create_project_snapshot_ticket(
        current_user,
        project,
        purpose="project_directory_archive",
        path=snapshot["path"],
        head=snapshot["head"],
    )
    return {
        **snapshot,
        "download_url": (
            f"/api/projects/{project.id}/files/archive/raw?"
            f"ticket={quote(ticket, safe='')}&path={quote(snapshot['path'], safe='')}"
        ),
        "ticket_expires_in": _PROJECT_FILE_TICKET_TTL_SECONDS,
    }


@router.api_route("/{project_id}/files/archive/raw", methods=["GET", "HEAD"])
async def get_project_directory_archive(
    project_id: uuid.UUID,
    request: Request,
    ticket: str = Query(min_length=1),
    path: str = Query(default="", max_length=1024),
    db: AsyncSession = Depends(get_db),
):
    """Stream a prevalidated ZIP from the exact HEAD captured by its ticket."""

    _user, project, payload = await _authorize_project_snapshot_ticket(
        db,
        project_id,
        ticket,
        purpose="project_directory_archive",
    )
    if payload["path"] != path:
        raise HTTPException(status_code=401, detail="Project archive ticket does not match this directory")
    snapshot = await inspect_project_directory(project, path, revision=payload["head"])
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(snapshot['name'], safe='')}",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Project-Git-Head": snapshot["head"],
    }
    if request.method == "HEAD":
        return Response(status_code=200, media_type="application/zip", headers=headers)
    return StreamingResponse(
        iter_project_directory_archive(project, snapshot["head"], snapshot["path"]),
        media_type="application/zip",
        headers=headers,
    )


@router.api_route("/{project_id}/files/preview/{ticket}/{path:path}", methods=["GET", "HEAD"])
async def get_project_html_preview_resource(
    project_id: uuid.UUID,
    ticket: str,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Serve one sandbox-preview asset from a ticket's immutable Git HEAD."""

    _user, project, payload = await _authorize_project_snapshot_ticket(
        db,
        project_id,
        ticket,
        purpose="project_html_preview",
    )
    metadata = await inspect_project_file_at(project, path, payload["head"])
    preview_prefix = f"{str(request.base_url).rstrip('/')}/api/projects/{project.id}/files/preview/"
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "private, no-store",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(metadata['name'], safe='')}",
        "Content-Length": str(metadata["size"]),
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Project-Git-Head": metadata["head"],
    }
    if metadata["mime_type"] == "text/html":
        headers["Content-Security-Policy"] = (
            "sandbox allow-scripts; default-src 'none'; "
            f"script-src 'unsafe-inline' {preview_prefix}; "
            f"style-src 'unsafe-inline' {preview_prefix}; "
            f"img-src data: blob: {preview_prefix}; "
            f"media-src data: blob: {preview_prefix}; "
            f"font-src data: {preview_prefix}; "
            "connect-src 'none'; frame-src 'none'; object-src 'none'; "
            "worker-src 'none'; base-uri 'none'; form-action 'none'; navigate-to 'none'"
        )
    if request.method == "HEAD" or metadata["size"] == 0:
        return Response(status_code=200, media_type=metadata["mime_type"], headers=headers)
    return StreamingResponse(
        iter_project_file_blob(project, metadata["object_id"], start=0, end=metadata["size"] - 1),
        media_type=metadata["mime_type"],
        headers=headers,
    )


@router.put("/{project_id}/files")
async def put_project_file(
    project_id: uuid.UUID,
    data: ProjectFileWriteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Atomically write one project file and immediately create its Git commit."""

    project = await require_project(db, current_user, project_id, edit=True)
    await _guard_project_agent_identity_paths(db, current_user, project, data.path)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await write_project_file(
        project,
        data.path,
        data.content,
        author_name=current_user.display_name,
        author_email=project_user_git_email(current_user.id),
    )
    _record_git_head(project, result["commit"])
    event = add_event(
        db,
        project,
        "project.file.committed",
        f"项目文件已保存：{result['path']}",
        actor_user_id=current_user.id,
        metadata={
            "path": result["path"],
            "commit": result["commit"],
            "operation": "write_and_commit",
        },
    )
    await db.flush()
    return {**result, "event_id": str(event.id)}


@router.post("/{project_id}/git/commit")
async def create_git_commit(
    project_id: uuid.UUID,
    data: GitCommitRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await commit_project_changes(
        project,
        data.message,
        data.paths,
        milestone=data.milestone,
        author_name=current_user.display_name,
        author_email=project_user_git_email(current_user.id),
    )
    _record_git_head(project, result["commit"])
    commit_event = add_event(
        db,
        project,
        "git.commit.created",
        "项目版本已创建",
        actor_user_id=current_user.id,
        metadata=result,
    )
    milestone_event = None
    if data.milestone:
        milestone_event = add_event(
            db,
            project,
            "git.milestone.created",
            "交付里程碑已创建",
            actor_user_id=current_user.id,
            metadata={
                **result,
                "milestone_message": data.message,
                "description": data.message,
            },
        )
    await db.flush()
    return {
        **result,
        "event_id": str(commit_event.id),
        "milestone_event_id": str(milestone_event.id) if milestone_event else None,
    }


@router.post("/{project_id}/git/branches")
async def create_git_branch(
    project_id: uuid.UUID,
    data: GitBranchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await create_branch(project, data.name, data.from_commit)
    event = add_event(
        db,
        project,
        "git.branch.created",
        f"Created branch: {data.name}",
        actor_user_id=current_user.id,
        metadata={**result, "forbidden_operations": ["reset", "force_push"]},
    )
    await db.flush()
    return {**result, "event_id": str(event.id)}


@router.get("/{project_id}/milestones", response_model=list[ProjectMilestoneOut])
async def list_project_milestones(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate durable milestone audit rows with verified Git commits."""
    project = await require_project(db, current_user, project_id)
    milestone_events = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(
                    ProjectEvent.project_id == project.id,
                    ProjectEvent.tenant_id == project.tenant_id,
                    ProjectEvent.event_type == "git.milestone.created",
                )
                .order_by(ProjectEvent.created_at.desc(), ProjectEvent.id.desc())
            )
        )
        .scalars()
        .all()
    )
    git_state = await repository_state(project, max(500, len(milestone_events) * 20))
    commit_by_hash = {str(commit["commit"]): commit for commit in git_state.get("commits", [])}
    run_ids = {event.run_id for event in milestone_events if event.run_id is not None}
    runs = (
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                    ProjectRun.id.in_(run_ids),
                )
            )
        )
        .scalars()
        .all()
        if run_ids
        else []
    )
    run_by_id = {str(payload["id"]): payload for payload in await serialize_project_runs(db, project, list(runs))}
    actor_agent_ids = {event.actor_agent_id for event in milestone_events if event.actor_agent_id is not None}
    actor_members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id.in_(actor_agent_ids),
                )
            )
        )
        .scalars()
        .all()
        if actor_agent_ids
        else []
    )
    member_name_by_agent = {str(member.agent_id): member.name_snapshot for member in actor_members}
    records = []
    for event in milestone_events:
        metadata = dict(event.event_metadata or {})
        commit_hash = str(metadata.get("commit") or "").strip()
        commit = commit_by_hash.get(commit_hash)
        # An audit event alone is not a milestone: expose only records whose
        # immutable Git commit still exists in the repository history.
        if commit is None:
            continue
        run = run_by_id.get(str(event.run_id)) if event.run_id else None
        records.append(
            {
                "id": event.id,
                "event_id": event.id,
                "project_id": project.id,
                "commit": commit_hash,
                "short_commit": commit.get("short_commit") or commit_hash[:12],
                "message": metadata.get("milestone_message")
                or metadata.get("description")
                or commit.get("message")
                or metadata.get("message")
                or event.summary,
                "author": commit.get("author"),
                "created_at": event.created_at,
                "commit_created_at": commit.get("created_at"),
                "run_id": event.run_id,
                "work_item_id": event.work_item_id or (run.get("work_item_id") if run else None),
                "session_id": metadata.get("session_id") or (run.get("session_id") if run else None),
                "subagent_session_id": metadata.get("subagent_session_id")
                or (run.get("subagent_session_id") if run else None),
                "agent_id": event.actor_agent_id or (run.get("agent_id") if run else None),
                "agent_name": (run.get("agent_name") if run else None)
                or member_name_by_agent.get(str(event.actor_agent_id)),
                "paths": list(metadata.get("paths") or []),
                "changed": metadata.get("changed"),
                "related_run_ids": list(metadata.get("related_run_ids") or []),
                "related_work_item_ids": list(metadata.get("related_work_item_ids") or []),
            }
        )
    return records


@router.get("/{project_id}/git")
async def get_git_state(
    project_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    state = await repository_state(project, limit)
    commits = list(state.get("commits") or [])
    commit_hashes = [str(commit.get("commit") or "") for commit in commits if commit.get("commit")]
    if not commit_hashes:
        return state

    events = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(
                    ProjectEvent.project_id == project.id,
                    ProjectEvent.tenant_id == project.tenant_id,
                    ProjectEvent.event_metadata["commit"].as_string().in_(commit_hashes),
                )
                .order_by(ProjectEvent.created_at.desc(), ProjectEvent.id.desc())
            )
        )
        .scalars()
        .all()
    )
    run_ids = {event.run_id for event in events if event.run_id is not None}
    work_item_ids = {event.work_item_id for event in events if event.work_item_id is not None}
    runs = (
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                    ProjectRun.id.in_(run_ids),
                )
            )
        )
        .scalars()
        .all()
        if run_ids
        else []
    )
    work_items = (
        (
            await db.execute(
                select(ProjectWorkItem).where(
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.tenant_id == project.tenant_id,
                    ProjectWorkItem.id.in_(work_item_ids),
                )
            )
        )
        .scalars()
        .all()
        if work_item_ids
        else []
    )
    run_agents = {run.id: run.agent_id for run in runs if run.agent_id is not None}
    work_item_agents = {item.id: item.assignee_agent_id for item in work_items if item.assignee_agent_id is not None}
    event_agent_by_commit: dict[str, uuid.UUID] = {}
    for event in events:
        commit_hash = str((event.event_metadata or {}).get("commit") or "")
        if not commit_hash or commit_hash in event_agent_by_commit:
            continue
        agent_id = run_agents.get(event.run_id) or work_item_agents.get(event.work_item_id) or event.actor_agent_id
        if agent_id is not None:
            event_agent_by_commit[commit_hash] = agent_id
    agent_ids = set(event_agent_by_commit.values())
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id.in_(agent_ids),
                )
            )
        )
        .scalars()
        .all()
        if agent_ids
        else []
    )
    member_names = {member.agent_id: member.name_snapshot for member in members}
    for commit in commits:
        agent_id = event_agent_by_commit.get(str(commit.get("commit") or ""))
        if agent_id is not None and member_names.get(agent_id):
            commit["author"] = member_names[agent_id]
            commit["author_agent_id"] = str(agent_id)
    return {**state, "commits": commits}


@router.get("/{project_id}/git/diff")
async def get_git_diff(
    project_id: uuid.UUID,
    commit: str = Query(..., min_length=7, max_length=64),
    parent: str | None = Query(None, min_length=7, max_length=64),
    path: str | None = Query(None, min_length=1, max_length=4096),
    max_patch_bytes: int = Query(256 * 1024, ge=1, le=1024 * 1024),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    return await project_commit_diff(project, commit, parent, path, max_patch_bytes)
