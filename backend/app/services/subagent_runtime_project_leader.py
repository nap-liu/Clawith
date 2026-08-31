"""Project leader batch dispatch helpers."""

from __future__ import annotations

from app.services.subagent_runtime_shared import *  # noqa: F401,F403
from app.services.subagent_runtime_lifecycle import append_subagent_message, create_subagent
from app.services.subagent_runtime_project_common import (
    _batch_attachment_refs,
    _batch_original_human_request,
    _ensure_project_leader_or_none,
    _leader_batch_causal_keys,
    _project_run_has_child_input,
    _project_work_item_snapshots,
    _publish_project_run_group_turn,
    _resolve_batch_work_item_lineage,
    _select_leader_batch_rows,
    _truncate_batch_content,
)

async def _dispatch_project_leader_batch(
    group_session_id: uuid.UUID,
    *,
    debounce_seconds: float = 0.5,
) -> bool:
    """Atomically claim pending Agent replies and enqueue one Leader input.

    Claimed rows and the batch ProjectRun are durable before child dispatch. A
    crash retries the same batch id and append_subagent_message's external key
    makes the Leader input idempotent.
    """
    from app.models.project import Project, ProjectMemberSnapshot, ProjectRun
    from app.services.project_service import add_event, freeze_run_members, project_execution_user_id

    cutoff = datetime.now(UTC) - timedelta(seconds=max(0.0, debounce_seconds))
    async with async_session() as db:
        group = await db.get(ChatSession, group_session_id, with_for_update=True)
        if group is None or group.source_channel != "project" or group.project_id is None:
            return True
        project = await db.get(Project, group.project_id, with_for_update=True)
        if project is None:
            return False
        leader = await _ensure_project_leader_or_none(db, project)
        if leader is None:
            blocked_rows = (
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id == str(group.id),
                            ChatMessage.message_meta["kind"].as_string() == "project_subagent_reply",
                            ChatMessage.message_meta["leader_batch_state"].as_string() == "pending",
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for row in blocked_rows:
                row.message_meta = {
                    **_message_meta(row),
                    "leader_batch_state": "blocked_no_leader",
                    "wake_policy": "blocked_no_leader",
                }
            if blocked_rows:
                add_event(
                    db,
                    project,
                    "leader.missing",
                    "当前没有可用的项目负责人",
                    metadata={"blocked_reply_ids": [str(row.id) for row in blocked_rows]},
                )
                await db.commit()
            return False
        if project.status != "running":
            return False

        claimed = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(group.id),
                        ChatMessage.message_meta["kind"].as_string() == "project_subagent_reply",
                        ChatMessage.message_meta["leader_batch_state"].as_string() == "claimed",
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        if claimed:
            batch_id = str(_message_meta(claimed[0]).get("leader_batch_id") or "")
            source_rows = [row for row in claimed if str(_message_meta(row).get("leader_batch_id") or "") == batch_id]
            raw_project_run_id = _message_meta(source_rows[0]).get("leader_batch_project_run_id")
            try:
                project_run_id = uuid.UUID(str(raw_project_run_id))
            except (TypeError, ValueError):
                return False
            project_run = await db.get(ProjectRun, project_run_id)
            if project_run is None:
                return False
            persisted_batch_input = dict(project_run.input or {})
            original_human_request = persisted_batch_input.get("original_human_request")
            if not isinstance(original_human_request, dict):
                original_human_request = None
            work_item_snapshots = persisted_batch_input.get("work_item_snapshots")
            if not isinstance(work_item_snapshots, list):
                work_item_snapshots = []
        else:
            pending_rows = (
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id == str(group.id),
                            ChatMessage.message_meta["kind"].as_string() == "project_subagent_reply",
                            ChatMessage.message_meta["leader_batch_state"].as_string() == "pending",
                            ChatMessage.created_at <= cutoff,
                            ChatMessage.sender_agent_id != leader.agent_id,
                        )
                        .order_by(ChatMessage.created_at, ChatMessage.id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if not pending_rows:
                return False
            causal_keys = await _leader_batch_causal_keys(db, project.id, pending_rows)
            source_rows = _select_leader_batch_rows(pending_rows, causal_keys)
            batch_id = str(uuid.uuid4())
            work_item_id, related_work_item_ids = await _resolve_batch_work_item_lineage(
                db,
                project.id,
                source_rows,
            )
            original_human_request = await _batch_original_human_request(
                db,
                project.id,
                group.id,
                source_rows,
            )
            cohort_anchor_id = (
                str(original_human_request.get("message_id") or "")
                if original_human_request is not None
                else ""
            )
            if not cohort_anchor_id:
                cohort_anchor_id = next(
                    (
                        str(_message_meta(row).get("timeline_anchor_id") or "")
                        for row in source_rows
                        if _message_meta(row).get("timeline_anchor_id")
                    ),
                    "",
                )
            work_item_snapshots = await _project_work_item_snapshots(
                db,
                project.id,
                related_work_item_ids,
            )
            source_snapshot_run_id = None
            for source_row in source_rows:
                for raw_run_id in _message_meta(source_row).get("source_project_run_ids", []):
                    try:
                        source_snapshot_run_id = uuid.UUID(str(raw_run_id))
                    except (TypeError, ValueError):
                        continue
                    break
                if source_snapshot_run_id is not None:
                    break
            project_run = ProjectRun(
                tenant_id=project.tenant_id,
                project_id=project.id,
                work_item_id=work_item_id,
                agent_id=leader.agent_id,
                initiated_by_user_id=project.owner_user_id,
                execution_user_id=project_execution_user_id(project),
                status="queued",
                trigger_type="leader_reply_batch",
                input={
                    "title": f"汇总 {len(source_rows)} 条成员回复并推进项目",
                    "group_session_id": str(group.id),
                    **(
                        {"group_message_id": cohort_anchor_id}
                        if cohort_anchor_id
                        else {}
                    ),
                    "leader_agent_id": str(leader.agent_id),
                    "leader_batch_id": batch_id,
                    "source_group_message_ids": [str(row.id) for row in source_rows],
                    "related_work_item_ids": [str(value) for value in related_work_item_ids],
                    "original_human_request": original_human_request,
                    "work_item_snapshots": work_item_snapshots,
                    "batch_limits": {
                        "max_replies": PROJECT_LEADER_BATCH_MAX_REPLIES,
                        "max_bytes": PROJECT_LEADER_BATCH_MAX_BYTES,
                    },
                },
                output={
                    "group_session_id": str(group.id),
                    "leader_batch_id": batch_id,
                    "related_work_item_ids": [str(value) for value in related_work_item_ids],
                },
            )
            db.add(project_run)
            await db.flush()
            await freeze_run_members(
                db,
                project,
                project_run,
                source_run_id=source_snapshot_run_id,
            )
            for row in source_rows:
                row.message_meta = {
                    **_message_meta(row),
                    "leader_batch_state": "claimed",
                    "leader_batch_id": batch_id,
                    "leader_batch_project_run_id": str(project_run.id),
                    "leader_batch_claimed_at": datetime.now(UTC).isoformat(),
                }
        source_agent_ids = {row.sender_agent_id for row in source_rows if row.sender_agent_id is not None}
        source_members = (
            (
                await db.execute(
                    select(ProjectMemberSnapshot).where(
                        ProjectMemberSnapshot.project_id == project.id,
                        ProjectMemberSnapshot.agent_id.in_(source_agent_ids),
                    )
                )
            )
            .scalars()
            .all()
            if source_agent_ids
            else []
        )
        source_member_by_agent = {member.agent_id: member for member in source_members}
        batch_execution_user_id = project_run.execution_user_id or project.owner_user_id
        await db.commit()

    async with async_session() as db:
        resumed_project = await db.get(Project, project.id)
        if resumed_project is None or resumed_project.status != "running":
            return False

    source_content_limit = max(
        512,
        (PROJECT_LEADER_BATCH_MAX_BYTES - 8 * 1024) // max(1, len(source_rows)),
    )
    sources = [
        {
            "group_message_id": str(row.id),
            "source_agent_id": str(row.sender_agent_id) if row.sender_agent_id else None,
            "source_agent_name": (
                source_member_by_agent[row.sender_agent_id].name_snapshot
                if row.sender_agent_id in source_member_by_agent
                else None
            ),
            "source_role_snapshot": (
                source_member_by_agent[row.sender_agent_id].role_snapshot
                if row.sender_agent_id in source_member_by_agent
                else None
            ),
            "child_message_id": _message_meta(row).get("child_message_id"),
            "subagent_session_id": _message_meta(row).get("subagent_id"),
            "subagent_run_id": _message_meta(row).get("subagent_id"),
            "project_run_ids": _message_meta(row).get("source_project_run_ids", []),
            "content": _truncate_batch_content(row.content, limit=source_content_limit),
            "attachments": _batch_attachment_refs(_message_meta(row).get("attachments", [])),
        }
        for row in source_rows
    ]
    from app.services.project_collaboration_prompt import build_project_owner_batch_task

    task = build_project_owner_batch_task(
        batch_id=batch_id,
        group_session_id=str(group_session_id),
        replies=sources,
        original_human_request=original_human_request,
        work_item_snapshots=work_item_snapshots,
    )
    if len(task.encode("utf-8")) > PROJECT_LEADER_BATCH_MAX_BYTES:
        sources = [
            {
                **source,
                "content": _truncate_batch_content(str(source.get("content") or ""), limit=256),
                "attachments": [],
            }
            for source in sources
        ]
        task = build_project_owner_batch_task(
            batch_id=batch_id,
            group_session_id=str(group_session_id),
            replies=sources,
            original_human_request=original_human_request,
            work_item_snapshots=work_item_snapshots,
        )
    if len(task.encode("utf-8")) > PROJECT_LEADER_BATCH_MAX_BYTES:
        task = _truncate_batch_content(task, limit=PROJECT_LEADER_BATCH_MAX_BYTES)
    async with async_session() as db:
        existing_child = (
            await db.execute(
                select(SubagentRun)
                .where(
                    SubagentRun.parent_session_id == group_session_id,
                    SubagentRun.project_member_id == leader.id,
                    SubagentRun.origin_tool_call_id
                    == _project_member_origin_tool_call_id(
                        leader,
                        batch_execution_user_id,
                        project.owner_user_id,
                    ),
                )
                .order_by(SubagentRun.id)
                .limit(1)
            )
        ).scalar_one_or_none()
    input_metadata = {
        "project_leader_batch": True,
        "leader_batch_id": batch_id,
        "source_group_message_ids": [str(row.id) for row in source_rows],
        "source_replies": sources,
        "work_item_id": str(project_run.work_item_id) if project_run.work_item_id else None,
        "related_work_item_ids": list(dict(project_run.input or {}).get("related_work_item_ids") or []),
        "original_human_request": original_human_request,
        "work_item_snapshots": work_item_snapshots,
        "batch_limits": {
            "max_replies": PROJECT_LEADER_BATCH_MAX_REPLIES,
            "max_bytes": PROJECT_LEADER_BATCH_MAX_BYTES,
        },
        "batch_input_bytes": len(task.encode("utf-8")),
    }
    if existing_child is None:
        durable_run, created = await create_subagent(
            agent_id=leader.agent_id,
            execution_user_id=batch_execution_user_id,
            parent_session_id=str(group_session_id),
            origin_tool_call_id=_project_member_origin_tool_call_id(
                leader,
                batch_execution_user_id,
                project.owner_user_id,
            ),
            task=task,
            mode="async",
            fork=True,
            turn_anchor_id=source_rows[-1].id,
            project_run_id=project_run.id,
            input_metadata=input_metadata,
            allow_parent_continuation=True,
        )
        child_id = durable_run.id
        child_status = durable_run.status
        if not created and not await _project_run_has_child_input(child_id, project_run.id):
            child_status = await append_subagent_message(
                agent_id=leader.agent_id,
                parent_session_id=str(group_session_id),
                subagent_id=str(durable_run.id),
                message=task,
                execution_user_id=durable_run.execution_user_id,
                origin_tool_call_id=f"project-leader-batch:{batch_id}",
                project_run_id=project_run.id,
                input_metadata=input_metadata,
                allow_parent_continuation=True,
            )
    else:
        child_id = existing_child.id
        child_status = existing_child.status
        if not await _project_run_has_child_input(child_id, project_run.id):
            child_status = await append_subagent_message(
                agent_id=leader.agent_id,
                parent_session_id=str(group_session_id),
                subagent_id=str(existing_child.id),
                message=task,
                execution_user_id=batch_execution_user_id,
                origin_tool_call_id=f"project-leader-batch:{batch_id}",
                project_run_id=project_run.id,
                input_metadata=input_metadata,
                allow_parent_continuation=True,
            )

    async with async_session() as db:
        project_run = await db.get(ProjectRun, project_run.id, with_for_update=True)
        batch_input = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == str(child_id),
                    ChatMessage.message_meta["leader_batch_id"].as_string() == batch_id,
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        delivered_rows = (
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.conversation_id == str(group_session_id),
                        ChatMessage.message_meta["leader_batch_id"].as_string() == batch_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in delivered_rows:
            row.message_meta = {
                **_message_meta(row),
                "leader_batch_state": "delivered",
                "leader_batch_input_id": str(batch_input.id) if batch_input else None,
                "leader_subagent_session_id": str(child_id),
            }
        if project_run is not None:
            project_run.status = "queued" if child_status == RUN_QUEUED else "running"
            project_run.output = {
                **dict(project_run.output or {}),
                "subagent_run_id": str(child_id),
                "subagent_session_id": str(child_id),
                "leader_batch_input_id": str(batch_input.id) if batch_input else None,
                "source_count": len(delivered_rows),
                "batch_limits": {
                    "max_replies": PROJECT_LEADER_BATCH_MAX_REPLIES,
                    "max_bytes": PROJECT_LEADER_BATCH_MAX_BYTES,
                },
            }
        attached_project = await db.get(Project, project.id)
        if attached_project is not None:
            add_event(
                db,
                attached_project,
                "project.agent_reply_batch.dispatched",
                f"Coalesced {len(delivered_rows)} Agent replies into one Leader turn",
                actor_agent_id=leader.agent_id,
                work_item_id=project_run.work_item_id if project_run else None,
                run_id=project_run.id if project_run else None,
                metadata={
                    "leader_batch_id": batch_id,
                    "leader_agent_id": str(leader.agent_id),
                    "leader_subagent_session_id": str(child_id),
                    "leader_batch_input_id": str(batch_input.id) if batch_input else None,
                    "source_group_message_ids": [str(row.id) for row in delivered_rows],
                    "source_count": len(delivered_rows),
                    "batch_limits": {
                        "max_replies": PROJECT_LEADER_BATCH_MAX_REPLIES,
                        "max_bytes": PROJECT_LEADER_BATCH_MAX_BYTES,
                    },
                    "related_work_item_ids": (
                        list(dict(project_run.input or {}).get("related_work_item_ids") or []) if project_run else []
                    ),
                },
            )
        published_project_run_id = project_run.id if project_run is not None else None
        await db.commit()
    if published_project_run_id is not None:
        await _publish_project_run_group_turn(published_project_run_id)
    return True
