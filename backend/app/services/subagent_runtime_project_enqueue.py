"""Project A2A enqueue entrypoint."""

from __future__ import annotations

from app.services.subagent_runtime_shared import *  # noqa: F401,F403
from app.services.subagent_runtime_project_common import _project_work_item_snapshots
from app.services.subagent_runtime_project_dispatch import dispatch_project_run

async def enqueue_project_a2a_run(
    *,
    project_id: uuid.UUID,
    a2a_session_id: uuid.UUID,
    outbound_message_id: uuid.UUID,
    from_agent_id: uuid.UUID,
    to_agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    message: str,
    mode: str,
    project_run_id: uuid.UUID | None = None,
    parent_project_run_id: uuid.UUID | None = None,
    work_item_id: uuid.UUID | None = None,
    run_title: str | None = None,
) -> dict:
    """Queue one exact, project-scoped Agent-to-Agent wake.

    The visible A2A ``ChatSession`` is only the collaboration timeline.  The
    target executes in its reusable project ``SubagentRun`` child so project
    tools, frozen capability policy, membership revocation and durable recovery
    are identical to project-group execution.  No other member is awakened.
    """
    from app.models.project import (
        Project,
        ProjectEvent,
        ProjectMemberSnapshot,
        ProjectRun,
        ProjectWorkItem,
    )
    from app.services.project_service import add_event, freeze_run_members

    task_text = str(message or "").strip()
    if not task_text:
        raise SubagentError("项目 A2A 消息不能为空。")
    if from_agent_id == to_agent_id:
        raise SubagentError("项目 A2A 发送方和接收方不能相同。")

    async with async_session() as db:
        project = await db.get(Project, project_id)
        if project is None:
            raise SubagentError("项目 A2A 会话或成员作用域已经失效。")
        if project.status != "running":
            raise SubagentError("项目已暂停；恢复项目后才能唤醒数字员工。")
        parent = await db.get(ChatSession, a2a_session_id)
        outbound = await db.get(ChatMessage, outbound_message_id, with_for_update=True)
        members = (
            (
                await db.execute(
                    select(ProjectMemberSnapshot).where(
                        ProjectMemberSnapshot.project_id == project_id,
                        ProjectMemberSnapshot.agent_id.in_([from_agent_id, to_agent_id]),
                        ProjectMemberSnapshot.is_enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        member_by_agent = {member.agent_id: member for member in members}
        source_member = member_by_agent.get(from_agent_id)
        target_member = member_by_agent.get(to_agent_id)
        if (
            parent is None
            or parent.project_id != project_id
            or parent.source_channel != "agent"
            or outbound is None
            or outbound.conversation_id != str(parent.id)
            or outbound.sender_agent_id != from_agent_id
            or {parent.agent_id, parent.peer_agent_id} != {from_agent_id, to_agent_id}
            or set(member_by_agent) != {from_agent_id, to_agent_id}
            or source_member is None
            or target_member is None
        ):
            raise SubagentError("项目 A2A 会话或成员作用域已经失效。")

        run = await db.get(ProjectRun, project_run_id, with_for_update=True) if project_run_id else None
        if project_run_id is not None and (
            run is None or run.project_id != project_id or run.agent_id != to_agent_id or run.trigger_type != "a2a"
        ):
            raise SubagentError("项目 A2A Run 与当前消息不匹配。")
        parent_run = await db.get(ProjectRun, parent_project_run_id) if parent_project_run_id else None
        if parent_project_run_id is not None and (
            parent_run is None or parent_run.project_id != project_id or parent_run.agent_id != from_agent_id
        ):
            raise SubagentError("项目 A2A 父 Run 与当前发送方不匹配。")

        parent_work_item_id = parent_run.work_item_id if parent_run else None
        resolved_work_item_id = work_item_id or parent_work_item_id or (run.work_item_id if run else None)
        work_item_title = None
        work_item_snapshot = None
        if resolved_work_item_id is not None:
            work_item = await db.get(ProjectWorkItem, resolved_work_item_id)
            if work_item is None or work_item.project_id != project_id:
                raise SubagentError("项目 A2A 工作项不属于当前项目。")
            work_item_title = work_item.title
            persisted_snapshot = dict(run.input or {}).get("work_item_snapshot") if run else None
            if isinstance(persisted_snapshot, dict):
                work_item_snapshot = persisted_snapshot
            else:
                snapshots = await _project_work_item_snapshots(
                    db,
                    project_id,
                    [resolved_work_item_id],
                )
                work_item_snapshot = snapshots[0] if snapshots else None
        if run is not None and work_item_id is not None and run.work_item_id not in {None, work_item_id}:
            raise SubagentError("项目 A2A Run 与显式工作项不匹配。")
        parent_title = str(dict(parent_run.input or {}).get("title") or "").strip() if parent_run else ""
        persisted_title = (
            str(run_title or "").strip() or parent_title or str(work_item_title or "").strip() or "处理 Agent 协作请求"
        )[:120]
        from app.services.project_collaboration_prompt import build_project_a2a_task

        dispatch = {
            "group_session_id": str(parent.id),
            "project_member_id": str(target_member.id),
            "turn_anchor_id": str(outbound_message_id),
            "task": build_project_a2a_task(
                task_text,
                source_name=source_member.name_snapshot,
                source_role=source_member.role_snapshot,
                target_name=target_member.name_snapshot,
                target_role=target_member.role_snapshot,
                work_item_title=str(work_item_title or ""),
                work_item_snapshot=work_item_snapshot,
            ),
            "a2a": {
                "session_id": str(parent.id),
                "message_id": str(outbound_message_id),
                "from_agent_id": str(from_agent_id),
                "to_agent_id": str(to_agent_id),
                "from_agent_name_snapshot": source_member.name_snapshot,
                "from_agent_role_snapshot": source_member.role_snapshot,
                "to_agent_name_snapshot": target_member.name_snapshot,
                "to_agent_role_snapshot": target_member.role_snapshot,
                "mode": str(mode or "notify"),
            },
        }
        if run is None:
            run = ProjectRun(
                tenant_id=project.tenant_id,
                project_id=project.id,
                work_item_id=resolved_work_item_id,
                agent_id=to_agent_id,
                initiated_by_user_id=execution_user_id,
                execution_user_id=execution_user_id,
                status="queued",
                trigger_type="a2a",
                input={
                    "title": persisted_title,
                    "parent_project_run_id": (str(parent_project_run_id) if parent_project_run_id else None),
                    "work_item_id": str(resolved_work_item_id) if resolved_work_item_id else None,
                    "work_item_snapshot": work_item_snapshot,
                    "from_agent_id": str(from_agent_id),
                    "to_agent_id": str(to_agent_id),
                    "message": task_text,
                    "mode": str(mode or "notify"),
                    "session_id": str(parent.id),
                    "dispatch": dispatch,
                },
                output={
                    "session_id": str(parent.id),
                    "session_agent_id": str(parent.agent_id),
                    "session_access_agent_id": str(parent.agent_id),
                    "session_title": parent.title,
                },
            )
            db.add(run)
            await db.flush()
            await freeze_run_members(
                db,
                project,
                run,
                source_run_id=parent_project_run_id,
            )
        else:
            run.input = {
                **dict(run.input or {}),
                "title": str(dict(run.input or {}).get("title") or persisted_title),
                "parent_project_run_id": (str(parent_project_run_id) if parent_project_run_id else None),
                "work_item_id": str(resolved_work_item_id) if resolved_work_item_id else None,
                "work_item_snapshot": work_item_snapshot,
                "session_id": str(parent.id),
                "dispatch": dispatch,
            }
            # An explicit work-item reference is the source of truth. Otherwise
            # inherit the exact parent Run lineage, while preserving a
            # pre-created API Run's existing work-item association.
            run.work_item_id = resolved_work_item_id
            run.output = {
                **dict(run.output or {}),
                "session_id": str(parent.id),
                "session_agent_id": str(parent.agent_id),
                "session_access_agent_id": str(parent.agent_id),
                "session_title": parent.title,
            }
        outbound.message_meta = {
            **_message_meta(outbound),
            "project_run_id": str(run.id),
            "work_item_id": str(run.work_item_id) if run.work_item_id else None,
        }
        existing_event = (
            await db.execute(
                select(ProjectEvent.id).where(
                    ProjectEvent.run_id == run.id,
                    ProjectEvent.event_type == "a2a.queued",
                )
            )
        ).scalar_one_or_none()
        if existing_event is None:
            add_event(
                db,
                project,
                "a2a.queued",
                "Queued one explicit project A2A wake",
                actor_user_id=execution_user_id,
                actor_agent_id=from_agent_id,
                from_agent_id=from_agent_id,
                to_agent_id=to_agent_id,
                work_item_id=run.work_item_id,
                run_id=run.id,
                metadata={
                    "mode": str(mode or "notify"),
                    "session_id": str(parent.id),
                    "message_id": str(outbound_message_id),
                    "awakened_agent_ids": [str(to_agent_id)],
                    "broadcast": False,
                    "parent_project_run_id": (str(parent_project_run_id) if parent_project_run_id else None),
                    "work_item_id": str(run.work_item_id) if run.work_item_id else None,
                },
            )
        await db.commit()
        run_id = run.id

    result = await dispatch_project_run(run_id)
    return {**result, "project_run_id": str(run_id)}
