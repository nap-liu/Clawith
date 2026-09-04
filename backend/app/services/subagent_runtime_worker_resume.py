"""Subagent confirmation resume and turn finalization helpers."""

from __future__ import annotations

from app.services.subagent_runtime_shared import *  # noqa: F401,F403
from app.services.subagent_runtime_worker_claim import _claim_subagent

async def resume_subagent_after_confirmation(
    run_id: uuid.UUID,
    *,
    resolved_tool_payload: dict | None = None,
    child_turn_anchor_id: uuid.UUID | None = None,
) -> bool:
    """Persist a resolved child confirmation and run it when its project allows."""
    already_running = False
    project_id = None
    parent_session_id = None
    group_timeline_anchor_id = None
    child_agent_id = None
    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if run is None or run.status in TERMINAL_STATUSES:
            return False
        project_id = run.project_id
        parent_session_id = run.parent_session_id
        child = await db.get(ChatSession, run_id)
        child_agent_id = child.agent_id if child is not None else None
        if project_id is not None and child_turn_anchor_id is not None:
            from app.services.project_group_turn_lifecycle import (
                project_group_timeline_anchor_for_child_turn,
            )

            group_timeline_anchor_id = await project_group_timeline_anchor_for_child_turn(
                db,
                project_id=project_id,
                child_session_id=run_id,
                child_turn_anchor_id=child_turn_anchor_id,
            )
        if run.status == RUN_WAITING:
            run.status = RUN_QUEUED
            run.lease_owner = None
            run.lease_expires_at = None
            await db.commit()
        elif run.status == RUN_RUNNING:
            # The original worker will observe the resolved tool row and queue
            # restart recovery after the caller returns.
            already_running = True
    if project_id is not None and parent_session_id is not None:
        from app.services.project_group_turn_lifecycle import (
            reconcile_and_publish_project_group_turn,
        )

        group_payload = {"type": "turn_state"}
        event_kind = "turn_lifecycle"
        if resolved_tool_payload is not None and group_timeline_anchor_id is not None:
            group_payload = {
                **resolved_tool_payload,
                "timeline_anchor_id": str(group_timeline_anchor_id),
                "producer_scope": f"project:{run_id}:{child_turn_anchor_id}",
                **(
                    {"sender_agent_id": str(child_agent_id)}
                    if child_agent_id is not None
                    else {}
                ),
            }
            event_kind = "turn_tool"
        await reconcile_and_publish_project_group_turn(
            project_id=project_id,
            session_id=parent_session_id,
            payload=group_payload,
            event_kind=event_kind,
        )
    if already_running:
        return True
    # Confirmation resolution is durable even while the project is paused.
    # The canonical claim boundary leaves it queued until runtime resumes.
    claimed = await _claim_subagent(run_id, with_token=True)
    if claimed is not None:
        claimed_run_id, lease_owner = claimed
        await execute_claimed_subagent(claimed_run_id, lease_owner=lease_owner)
    return True


async def _finish_subagent_turn(
    *,
    run_id: uuid.UUID,
    anchor_id: uuid.UUID,
    reply: str,
    failed: bool,
    failure_code: str | None = None,
    thinking: str | None = None,
    reply_quality: dict | None = None,
) -> bool:
    """Persist the reply and lifecycle transition behind the same Run lock."""
    from app.services.active_turns import wait_for_current_turn_stop_resolution
    from app.services.chat_history import persist_assistant_reply_row

    # Resolve a concurrent stop before opening a transaction. Waiting after the
    # input rows have been mutated can autoflush the anchor and deadlock the
    # stop transaction that must mark that same row as cancelled.
    await wait_for_current_turn_stop_resolution()

    async with async_session() as db:
        run = await db.get(SubagentRun, run_id, with_for_update=True)
        if not _owns_subagent_lease(run):
            return False
        child = await db.get(ChatSession, run_id)
        if child is None:
            return False
        processed = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PROCESSING,
                        ChatMessage.message_meta["subagent_turn_anchor_id"].as_string() == str(anchor_id),
                    )
                )
            )
            .scalars()
            .all()
        )
        pending_exists = bool(
            (
                await db.execute(
                    select(ChatMessage.id)
                    .where(
                        ChatMessage.conversation_id == str(run_id),
                        ChatMessage.message_meta["kind"].as_string() == SUBAGENT_INPUT,
                        ChatMessage.message_meta["subagent_input_state"].as_string() == INPUT_PENDING,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
        )
        # Pending parent input always wins over the current turn's outcome.  A
        # failed provider/tool round may finish its already-dispatched inputs,
        # but it cannot strand later pending messages behind a terminal Run.
        terminal = not pending_exists
        kind = (
            SUBAGENT_FAILURE
            if failed and terminal
            else "subagent_turn_failure"
            if failed
            else SUBAGENT_COMPLETION
            if terminal
            else "subagent_turn_result"
        )
        content = (reply or "").strip() or (
            "本次执行未完成，请稍后重试或查看项目状态。" if failed else "协作任务已完成。"
        )
        # One durable child Session may process many separately auditable
        # project mentions. Complete every ProjectRun whose exact input was
        # consumed by this turn; do not leave the Runs UI permanently queued.
        from app.models.project import Project, ProjectEvent, ProjectRun, ProjectWorkItem
        from app.services.project_service import add_event

        now = datetime.now(UTC)
        project_run_ids: set[uuid.UUID] = set()
        for row in processed:
            raw_project_run_id = _message_meta(row).get("project_run_id")
            if raw_project_run_id:
                try:
                    project_run_ids.add(uuid.UUID(str(raw_project_run_id)))
                except (TypeError, ValueError):
                    logger.warning(
                        "[subagent] ignoring invalid project_run_id on input=%s",
                        row.id,
                    )
        if project_run_ids:
            project_runs = (
                (
                    await db.execute(
                        select(ProjectRun).where(
                            ProjectRun.id.in_(project_run_ids),
                            ProjectRun.project_id == run.project_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for project_run in project_runs:
                project_run.status = "failed" if failed else "succeeded"
                project_run.finished_at = now
                project_run.output = {
                    **dict(project_run.output or {}),
                    "subagent_run_id": str(run.id),
                    "subagent_session_id": str(run.id),
                    "result": content,
                    **({"reply_quality": reply_quality} if reply_quality else {}),
                }
                project_run.error = content if failed else None
                terminal_event_type = "run.failed" if failed else "run.succeeded"
                terminal_event_exists = (
                    await db.execute(
                        select(ProjectEvent.id).where(
                            ProjectEvent.run_id == project_run.id,
                            ProjectEvent.event_type == terminal_event_type,
                        )
                    )
                ).scalar_one_or_none()
                project = await db.get(Project, project_run.project_id)
                if project_run.trigger_type == "a2a" and project_run.work_item_id is not None:
                    work_item = await db.get(
                        ProjectWorkItem,
                        project_run.work_item_id,
                        with_for_update=True,
                    )
                    if work_item is not None and work_item.project_id == project_run.project_id:
                        before_status = work_item.status
                        terminal_work_item_status = "blocked" if failed else "review"
                        if before_status in {"backlog", "todo", "in_progress"}:
                            work_item.status = terminal_work_item_status
                            if project is not None:
                                add_event(
                                    db,
                                    project,
                                    "work_item.updated",
                                    (
                                        f"{child.title} blocked work item {work_item.title}"
                                        if failed
                                        else f"{child.title} submitted work item {work_item.title} for review"
                                    ),
                                    actor_agent_id=child.agent_id,
                                    work_item_id=work_item.id,
                                    run_id=project_run.id,
                                    metadata={
                                        "before": {"status": before_status},
                                        "after": {"status": work_item.status},
                                        "reason": "a2a_run_failed" if failed else "a2a_run_succeeded",
                                        "progress_note": content[:600],
                                        "project_run_id": str(project_run.id),
                                        "subagent_run_id": str(run.id),
                                    },
                                )
                if project is not None and terminal_event_exists is None:
                    add_event(
                        db,
                        project,
                        terminal_event_type,
                        "本次执行未完成" if failed else "本次执行已完成",
                        actor_user_id=project_run.initiated_by_user_id,
                        actor_agent_id=child.agent_id,
                        work_item_id=project_run.work_item_id,
                        run_id=project_run.id,
                        metadata={
                            "project_run_id": str(project_run.id),
                            "subagent_run_id": str(run.id),
                            "subagent_session_id": str(run.id),
                            "session_id": str(dict(project_run.output or {}).get("session_id") or run.id),
                            "status": project_run.status,
                        },
                    )
            owner_run = next(
                (
                    project_run
                    for project_run in project_runs
                    if project_run.trigger_type
                    in {"leader_kickoff", "leader_reply_batch", "group_leader_message"}
                ),
                None,
            )
            if owner_run is not None:
                owner_project = await db.get(Project, owner_run.project_id, with_for_update=True)
                if owner_project is not None and owner_project.status == "running":
                    active_successor = (
                        await db.execute(
                            select(ProjectRun.id)
                            .where(
                                ProjectRun.project_id == owner_project.id,
                                ProjectRun.id.notin_(project_run_ids),
                                ProjectRun.status.in_(["queued", "running"]),
                                ProjectRun.finished_at.is_(None),
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    unresolved_items = (
                        (
                            await db.execute(
                                select(ProjectWorkItem)
                                .where(
                                    ProjectWorkItem.project_id == owner_project.id,
                                    ProjectWorkItem.status != "done",
                                )
                                .order_by(ProjectWorkItem.updated_at.desc(), ProjectWorkItem.id)
                                .limit(50)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    has_any_work_item = bool(
                        await db.scalar(
                            select(ProjectWorkItem.id)
                            .where(ProjectWorkItem.project_id == owner_project.id)
                            .limit(1)
                        )
                    )
                    if active_successor is None:
                        owner_project.status = "waiting"
                        project_settings = dict(owner_project.settings or {})
                        if failed:
                            project_settings["current_signal"] = "负责人本轮执行未完成，项目正在等待处理。"
                            project_settings["next_action"] = "查看执行记录并重新推进项目。"
                        elif has_any_work_item and not unresolved_items:
                            project_settings["current_signal"] = "当前任务均已完成，项目正在等待确认。"
                            project_settings["next_action"] = "确认项目结果或继续补充新的任务。"
                        else:
                            project_settings["current_signal"] = "项目当前没有正在执行的工作，存在待评审、待处理或待确认事项。"
                            project_settings["next_action"] = "处理待评审、阻塞或缺失的任务后恢复项目。"
                        owner_project.settings = project_settings
                        add_event(
                            db,
                            owner_project,
                            "project.status.updated",
                            "Project is waiting for the next decision",
                            actor_agent_id=child.agent_id,
                            run_id=owner_run.id,
                            metadata={
                                "before": "running",
                                "after": "waiting",
                                "reason": (
                                    "owner_turn_failed_without_active_successor"
                                    if failed
                                    else "owner_turn_finished_without_active_successor"
                                ),
                                "unresolved_work_item_ids": [str(item.id) for item in unresolved_items],
                            },
                        )
        assistant_message_id = await persist_assistant_reply_row(
            db,
            agent_id=child.agent_id,
            user_id=run.execution_user_id,
            conversation_id=str(run_id),
            content=content,
            thinking=thinking,
            message_meta={
                "kind": kind,
                **({"error_code": failure_code} if failure_code else {}),
                "subagent_wake": terminal and run.mode == "async",
                **(
                    {"subagent_dispatch_state": SUBAGENT_DISPATCH_PENDING}
                    if terminal and run.mode == "async"
                    else {}
                ),
                "attachments": [],
                "project_run_ids": [str(value) for value in sorted(project_run_ids, key=str)],
                **({"reply_quality": reply_quality} if reply_quality else {}),
            },
            turn_anchor_id=anchor_id,
            turn_terminal_status="failed" if failed else "completed",
        )
        for row in processed:
            meta = _message_meta(row)
            meta["subagent_input_state"] = INPUT_DONE
            if row.id != anchor_id:
                meta["turn_status"] = "completed" if not failed else "failed"
            row.message_meta = meta
        child.last_message_at = now
        if terminal:
            run.status = RUN_FAILED if failed else RUN_COMPLETED
            run.lease_owner = None
            run.lease_expires_at = None
        else:
            run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)
        await db.commit()
        if terminal and run.mode == "async":
            if run.project_id is not None:
                _signal_project_dispatch_work()
            else:
                _signal_dispatch_work()
        from app.services.conversation_turn_lifecycle import publish_committed_turn_terminal

        await publish_committed_turn_terminal(
            agent_id=child.agent_id,
            conversation_id=str(run_id),
            turn_anchor_id=anchor_id,
            message_id=assistant_message_id,
            content=content,
        )
        return terminal
