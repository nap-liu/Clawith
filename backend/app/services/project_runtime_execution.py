"""Structural support extracted from project runtime tools."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.project import (
    Project,
    ProjectCapabilityBinding,
    ProjectEvent,
    ProjectMemberSnapshot,
    ProjectRun,
    ProjectWorkItem,
)
from app.models.subagent_run import SubagentRun
from app.services.project_git_service import (
    ProjectSandboxWorkspace,
    commit_project_changes,
    commit_project_workspace_sandbox_changes,
    delete_project_workspace_file,
    edit_project_workspace_file,
    find_project_workspace_files,
    list_project_workspace,
    materialize_project_read_workspace,
    move_project_workspace_path,
    project_agent_git_email,
    project_repository_commit_is_ancestor,
    read_project_workspace_file,
    repository_state,
    reset_project_repository_head,
    restore_as_new_commit,
    search_project_workspace,
    write_project_workspace_file,
)
from app.services.project_service import (
    add_event,
    deactivate_project_member,
    restore_project_member,
)

from app.services.project_runtime_tool_catalog import (
    PROJECT_RUNTIME_TOOL_NAMES,
    WORK_ITEM_PRIORITIES,
    WORK_ITEM_STATUSES,
    _milestone_operation_event,
    _milestone_operation_key,
    effective_project_tool_names,
)
from app.services.project_runtime_tool_support import (
    _dependency_ids,
    _enabled_member,
    _list_work_items,
    _project_context,
    _runtime_scope,
    _uuid,
)


async def execute_project_runtime_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    session_id: str,
    tool_call_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> str:
    """Execute one project tool after deriving its scope from the child session."""

    if tool_name not in PROJECT_RUNTIME_TOOL_NAMES:
        raise ValueError(f"Unknown project runtime tool: {tool_name}")
    project, member, project_run = await _runtime_scope(
        session_id,
        agent_id,
        execution_user_id,
        turn_anchor_id,
    )
    trace_metadata = {
        "session_id": session_id,
        "subagent_session_id": session_id,
        "project_run_id": str(project_run.id) if project_run else None,
        "a2a_session_id": (dict(project_run.output or {}).get("a2a_session_id") if project_run else None),
    }
    if tool_name not in effective_project_tool_names(project, member):
        raise ValueError(
            "This project tool is not allowed by the current role, project policy, and member configuration"
        )
    if tool_name == "project_get_context":
        return await _project_context(project)
    if tool_name == "project_list_work_items":
        return await _list_work_items(project, agent_id, bool(arguments.get("mine_only", False)))
    if tool_name == "project_message_agent":
        target_id = _uuid(arguments.get("agent_id"), "agent_id")
        message = str(arguments.get("message") or "").strip()
        mode = str(arguments.get("mode") or "task_delegate")
        title = str(arguments.get("title") or "").strip()
        expected_output = str(arguments.get("expected_output") or "").strip()
        if not message:
            raise ValueError("message is required")
        if mode not in {"task_delegate", "consult"}:
            raise ValueError(
                "成员协作请求需要明确任务或咨询内容。"
            )
        if not title:
            raise ValueError("成员协作请求需要填写标题。")
        if not expected_output:
            raise ValueError("成员协作请求需要填写预期结果。")
        from app.services.project_reply_quality import project_handoff_rejection_reasons

        handoff_reasons = project_handoff_rejection_reasons(message)
        if handoff_reasons:
            raise ValueError(
                "成员协作请求需要包含可执行的工作内容和预期结果。"
            )
        if target_id == agent_id:
            raise ValueError("不能向当前数字员工发起成员协作请求。")
        explicit_work_item_id = _uuid(arguments.get("work_item_id"), "work_item_id", optional=True)
        related_work_item_id = explicit_work_item_id or (project_run.work_item_id if project_run else None)
        async with async_session() as db:
            await _enabled_member(db, project, target_id)
            if related_work_item_id is not None:
                related_work_item = await db.get(ProjectWorkItem, related_work_item_id)
                if related_work_item is None or related_work_item.project_id != project.id:
                    raise ValueError("关联任务必须属于当前项目。")
                dependency_ids = list(related_work_item.dependency_ids or [])
                if dependency_ids:
                    dependency_rows = (
                        (
                            await db.execute(
                                select(ProjectWorkItem).where(
                                    ProjectWorkItem.project_id == project.id,
                                    ProjectWorkItem.id.in_(dependency_ids),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    unfinished = [row.title for row in dependency_rows if row.status != "done"]
                    missing = len(dependency_rows) != len(set(dependency_ids))
                    if unfinished or missing:
                        labels = ", ".join(unfinished) or "未找到前置任务记录"
                        raise ValueError(
                            "前置任务尚未完成，暂不能发起成员协作：" + labels
                        )
        if mode == "task_delegate" and related_work_item_id is None:
            raise ValueError("委派任务前需要关联一个项目任务。")
        from app.services.agent_tools import _send_message_to_agent

        result = await _send_message_to_agent(
            agent_id,
            {
                "agent_id": str(target_id),
                "message": f"{message}\n\nExpected output / 预期产出：{expected_output}",
                "msg_type": mode,
                "new_conversation": bool(arguments.get("new_conversation", False)),
                "_project_id": str(project.id),
                "_parent_project_run_id": str(project_run.id) if project_run else None,
                "_work_item_id": str(related_work_item_id) if related_work_item_id else None,
                "_run_title": title or None,
            },
            user_id=execution_user_id,
            origin_session_id=session_id,
            tool_call_id=tool_call_id,
            origin_turn_anchor_id=turn_anchor_id,
        )
        try:
            delivery = json.loads(result)
        except (TypeError, ValueError):
            delivery = {}
        if (
            not isinstance(delivery, dict)
            or delivery.get("status") not in {"queued", "running"}
            or not (delivery.get("a2a_session_id") or delivery.get("session_id"))
        ):
            raise RuntimeError("成员协作请求暂未提交成功，请稍后重试。")
        delivered_session_id = str(delivery.get("a2a_session_id") or delivery.get("session_id") or "") or None
        async with async_session() as db:
            attached = await db.get(Project, project.id)
            delegated_item = None
            delegated_before = None
            if mode == "task_delegate" and related_work_item_id is not None:
                delegated_item = await db.get(
                    ProjectWorkItem,
                    related_work_item_id,
                    with_for_update=True,
                )
                if delegated_item is not None and delegated_item.status in {"backlog", "todo"}:
                    delegated_before = delegated_item.status
                    delegated_item.status = "in_progress"
            add_event(
                db,
                attached,
                "project.agent.message.sent",
                f"{member.name_snapshot} sent a targeted project message",
                actor_agent_id=agent_id,
                from_agent_id=agent_id,
                to_agent_id=target_id,
                work_item_id=related_work_item_id,
                metadata={
                    "mode": mode,
                    "title": title,
                    "expected_output": expected_output,
                    "new_conversation": bool(arguments.get("new_conversation", False)),
                    "session_id": delivered_session_id,
                    "a2a_session_id": delivered_session_id,
                    "origin_session_id": session_id,
                    "project_run_id": delivery.get("project_run_id"),
                    "subagent_run_id": delivery.get("subagent_run_id"),
                    "subagent_session_id": delivery.get("subagent_session_id"),
                    "delivery_status": "delivered",
                    "visible_to_group": False,
                },
            )
            if delegated_item is not None and delegated_before is not None:
                add_event(
                    db,
                    attached,
                    "work_item.updated",
                    f"{member.name_snapshot} started work item {delegated_item.title}",
                    actor_agent_id=agent_id,
                    from_agent_id=agent_id,
                    to_agent_id=target_id,
                    work_item_id=delegated_item.id,
                    run_id=project_run.id if project_run else None,
                    metadata={
                        "before": {"status": delegated_before},
                        "after": {"status": delegated_item.status},
                        "reason": "task_delegate_dispatched",
                        "session_id": delivered_session_id,
                        "project_run_id": delivery.get("project_run_id"),
                    },
                )
            await db.commit()
        return result

    if tool_name == "project_set_status":
        requested_status = str(arguments.get("status") or "").strip()
        if requested_status not in {"waiting", "paused", "completed", "failed"}:
            raise ValueError("status must be waiting, paused, completed, or failed")
        async with async_session() as db:
            attached = (
                await db.execute(select(Project).where(Project.id == project.id).with_for_update())
            ).scalar_one()
            if attached.status in {"planning", "initializing"}:
                raise ValueError("Confirm project kickoff before changing execution status")
            if attached.status in {"completed", "failed", "archived"} and attached.status != requested_status:
                raise ValueError("A terminal project cannot be reopened by an Agent tool")
            if requested_status == "completed":
                unfinished_count = int(
                    await db.scalar(
                        select(func.count())
                        .select_from(ProjectWorkItem)
                        .where(
                            ProjectWorkItem.project_id == attached.id,
                            ProjectWorkItem.status != "done",
                        )
                    )
                    or 0
                )
                if unfinished_count:
                    raise ValueError(
                        f"Complete all project work items before completion ({unfinished_count} unfinished)"
                    )
            previous_status = attached.status
            attached.status = requested_status
            add_event(
                db,
                attached,
                "project.status.updated",
                f"{member.name_snapshot} changed project status to {requested_status}",
                actor_agent_id=agent_id,
                work_item_id=project_run.work_item_id if project_run else None,
                run_id=project_run.id if project_run else None,
                metadata={
                    "before": previous_status,
                    "after": requested_status,
                    "reason": str(arguments.get("reason") or "").strip() or None,
                    **trace_metadata,
                },
            )
            await db.commit()
        return json.dumps(
            {"project_id": str(project.id), "before": previous_status, "status": requested_status},
            ensure_ascii=False,
        )

    if tool_name == "project_restore_commit":
        policies = dict((project.settings or {}).get("policies") or {})
        if policies.get("git_restore") in {"human", "human_approval", "deny"}:
            raise ValueError("Project policy requires a Human to approve Git restore")
        commit = str(arguments.get("commit") or "").strip()
        if not commit:
            raise ValueError("commit is required")
        result = await restore_as_new_commit(
            project,
            commit,
            str(arguments.get("message") or "").strip() or None,
            author_name=member.name_snapshot,
            author_email=project_agent_git_email(agent_id),
        )
        async with async_session() as db:
            attached = await db.get(Project, project.id)
            settings = dict(attached.settings or {})
            settings["git"] = {**dict(settings.get("git") or {}), "head": result["commit"]}
            attached.settings = settings
            add_event(
                db,
                attached,
                "git.restored",
                f"{member.name_snapshot} 恢复了项目版本",
                actor_agent_id=agent_id,
                metadata={**result, "session_id": session_id},
            )
            await db.commit()
        return json.dumps(result, ensure_ascii=False)

    if tool_name == "project_create_milestone":
        message = str(arguments.get("message") or "").strip()
        if not message:
            raise ValueError("message is required")
        paths = [str(value) for value in arguments.get("paths", []) if str(value).strip()] or None
        related_work_item_ids = {
            _uuid(value, "related_work_item_id") for value in arguments.get("related_work_item_ids", [])
        }
        requested_related_run_ids = {
            _uuid(value, "related_run_id") for value in arguments.get("related_run_ids", [])
        }
        related_run_ids = set(requested_related_run_ids)
        async with async_session() as db:
            if related_work_item_ids:
                found_work_items = set(
                    (
                        await db.execute(
                            select(ProjectWorkItem.id).where(
                                ProjectWorkItem.project_id == project.id,
                                ProjectWorkItem.id.in_(related_work_item_ids),
                            )
                        )
                    ).scalars()
                )
                if found_work_items != related_work_item_ids:
                    raise ValueError("Every related work item must belong to the current project")
                if not related_run_ids:
                    related_run_ids = set(
                        (
                            await db.execute(
                                select(ProjectRun.id).where(
                                    ProjectRun.project_id == project.id,
                                    ProjectRun.tenant_id == project.tenant_id,
                                    ProjectRun.work_item_id.in_(related_work_item_ids),
                                    ProjectRun.status == "succeeded",
                                )
                            )
                        ).scalars()
                    )
            if related_run_ids:
                found_runs = set(
                    (
                        await db.execute(
                            select(ProjectRun.id).where(
                                ProjectRun.project_id == project.id,
                                ProjectRun.id.in_(related_run_ids),
                            )
                        )
                    ).scalars()
                )
                if found_runs != related_run_ids:
                    raise ValueError("Every related run must belong to the current project")
            operation_key = _milestone_operation_key(
                project,
                member,
                project_run,
                session_id,
                message,
                paths,
                related_work_item_ids,
                requested_related_run_ids,
            )
            related_metadata = {
                **trace_metadata,
                "milestone_operation_key": operation_key,
                "milestone_message": message,
                "description": message,
                "related_work_item_ids": [str(value) for value in sorted(related_work_item_ids, key=str)],
                "related_run_ids": [str(value) for value in sorted(related_run_ids, key=str)],
            }
            operation_event = await _milestone_operation_event(db, project, operation_key)
            if operation_event is None:
                operation_event = add_event(
                    db,
                    project,
                    "git.milestone.prepared",
                    f"{member.name_snapshot} 正在创建交付里程碑",
                    actor_agent_id=agent_id,
                    work_item_id=project_run.work_item_id if project_run else None,
                    run_id=project_run.id if project_run else None,
                    metadata={**related_metadata, "milestone_state": "prepared"},
                )
                await db.flush()
            operation_event_id = operation_event.id
            # The prepared audit row is the durable operation journal.  Any DB
            # constraint/serialization failure happens before Git is touched.
            await db.commit()
        result = await commit_project_changes(
            project,
            message,
            paths,
            milestone=True,
            operation_key=operation_key,
            author_name=member.name_snapshot,
            author_email=project_agent_git_email(agent_id),
        )
        async with async_session() as db:
            attached = await db.get(Project, project.id)
            operation_event = await db.get(ProjectEvent, operation_event_id)
            if operation_event is None:
                operation_event = await _milestone_operation_event(db, attached, operation_key)
            if operation_event is None:
                raise RuntimeError("Durable milestone operation journal is missing")
            settings = dict(attached.settings or {})
            settings["git"] = {**dict(settings.get("git") or {}), "head": result["commit"]}
            attached.settings = settings
            operation_event.event_type = "git.milestone.created"
            operation_event.summary = f"{member.name_snapshot} 创建了交付里程碑"
            operation_event.event_metadata = {
                **dict(operation_event.event_metadata or {}),
                **result,
                **related_metadata,
                "milestone_state": "created",
            }
            await db.commit()
        return json.dumps({**result, "event_id": str(operation_event_id)}, ensure_ascii=False)

    async with async_session() as db:
        attached = await db.get(Project, project.id)
        if tool_name == "project_update_plan":
            changed: dict[str, Any] = {}
            if "goal" in arguments:
                goal = str(arguments.get("goal") or "").strip()
                if not goal:
                    raise ValueError("goal cannot be empty")
                attached.goal = goal
                changed["goal"] = goal
            if "success_criteria" in arguments:
                criteria = [str(value).strip() for value in arguments.get("success_criteria", []) if str(value).strip()]
                if not criteria:
                    raise ValueError("success_criteria cannot be empty")
                attached.success_criteria = criteria
                changed["success_criteria"] = criteria
            settings = dict(attached.settings or {})
            for key in ("current_signal", "next_action"):
                if key in arguments:
                    settings[key] = str(arguments.get(key) or "").strip()
                    changed[key] = settings[key]
            if not changed:
                raise ValueError("At least one planning field is required")
            attached.settings = settings
            add_event(
                db,
                attached,
                "project.plan.updated",
                f"{member.name_snapshot} updated the project plan",
                actor_agent_id=agent_id,
                metadata={"changed_fields": sorted(changed), "session_id": session_id},
            )
            await db.commit()
            return json.dumps({"status": "updated", "changed": changed}, ensure_ascii=False)

        if tool_name == "project_set_member_enabled":
            target_id = _uuid(arguments.get("agent_id"), "agent_id")
            target = (
                await db.execute(
                    select(ProjectMemberSnapshot).where(
                        ProjectMemberSnapshot.project_id == attached.id,
                        ProjectMemberSnapshot.agent_id == target_id,
                    )
                )
            ).scalar_one_or_none()
            if target is None:
                raise ValueError("member was not found in the current project")
            if target.is_leader:
                raise ValueError("Project-owner enablement and transfer require a Human operator")
            enabled = bool(arguments.get("is_enabled"))
            cancelled_child_ids: list[uuid.UUID] = []
            if enabled:
                await restore_project_member(
                    db,
                    attached,
                    target,
                    actor_agent_id=agent_id,
                    reason="project_leader_tool_restore",
                )
            else:
                cancelled_child_ids = await deactivate_project_member(
                    db,
                    attached,
                    target,
                    actor_agent_id=agent_id,
                    reason="project_leader_tool_remove",
                )
            await db.commit()
            if cancelled_child_ids:
                from app.services.subagent_runtime import (
                    finalize_cancelled_project_member_turns,
                )

                await finalize_cancelled_project_member_turns(
                    attached.id,
                    cancelled_child_ids,
                )
            return json.dumps({"agent_id": str(target.agent_id), "is_enabled": target.is_enabled})

        if tool_name == "project_set_capability_enabled":
            binding_id = _uuid(arguments.get("binding_id"), "binding_id")
            binding = (
                await db.execute(
                    select(ProjectCapabilityBinding).where(
                        ProjectCapabilityBinding.id == binding_id,
                        ProjectCapabilityBinding.project_id == attached.id,
                    )
                )
            ).scalar_one_or_none()
            if binding is None:
                raise ValueError("capability binding was not found in the current project")
            binding.is_enabled = bool(arguments.get("is_enabled"))
            add_event(
                db,
                attached,
                "capability.updated",
                f"{member.name_snapshot} changed capability {binding.capability_name}",
                actor_agent_id=agent_id,
                metadata={"binding_id": str(binding.id), "enabled": binding.is_enabled, "session_id": session_id},
            )
            await db.commit()
            return json.dumps({"binding_id": str(binding.id), "is_enabled": binding.is_enabled})

        if tool_name == "project_create_work_item":
            title = str(arguments.get("title") or "").strip()
            description = str(arguments.get("description") or "").strip()
            criteria = [str(value).strip() for value in arguments.get("acceptance_criteria", []) if str(value).strip()]
            if not title or not description or not criteria:
                raise ValueError("title, description and at least one acceptance criterion are required")
            priority = str(arguments.get("priority") or "medium")
            if priority not in WORK_ITEM_PRIORITIES:
                raise ValueError("priority is invalid")
            assignee_id = _uuid(arguments.get("assignee_agent_id"), "assignee_agent_id", optional=True)
            await _enabled_member(db, attached, assignee_id)
            dependencies = await _dependency_ids(db, attached, list(arguments.get("dependency_ids") or []))
            item = ProjectWorkItem(
                tenant_id=attached.tenant_id,
                project_id=attached.id,
                assignee_agent_id=assignee_id,
                created_by_agent_id=agent_id,
                title=title,
                description=description,
                status="backlog",
                priority=priority,
                acceptance_criteria=criteria,
                dependency_ids=dependencies,
            )
            db.add(item)
            await db.flush()
            add_event(
                db,
                attached,
                "work_item.created",
                f"{member.name_snapshot} created work item {title}",
                actor_agent_id=agent_id,
                work_item_id=item.id,
                metadata={
                    "status": item.status,
                    "priority": priority,
                    "assignee_agent_id": str(assignee_id) if assignee_id else None,
                    "session_id": session_id,
                },
            )
            await db.commit()
            return json.dumps(
                {
                    "id": str(item.id),
                    "title": item.title,
                    "status": item.status,
                    "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
                },
                ensure_ascii=False,
            )

        item_id = _uuid(arguments.get("work_item_id"), "work_item_id")
        item = (
            await db.execute(
                select(ProjectWorkItem).where(
                    ProjectWorkItem.id == item_id,
                    ProjectWorkItem.project_id == attached.id,
                )
            )
        ).scalar_one_or_none()
        if item is None:
            raise ValueError("work item was not found in the current project")
        if not member.is_leader:
            if item.assignee_agent_id != agent_id:
                raise ValueError("Participants may update only work items assigned to themselves")
            participant_fields = {"work_item_id", "status", "progress_note", "evidence"}
            disallowed = set(arguments) - participant_fields
            if disallowed:
                raise ValueError(
                    "Participants may update only status, progress_note and evidence on assigned work items"
                )
        if not (set(arguments) - {"work_item_id"}):
            raise ValueError("At least one work item update field is required")
        before = {
            "title": item.title,
            "status": item.status,
            "priority": item.priority,
            "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
        }
        for field in ("title", "description"):
            if field in arguments:
                value = str(arguments[field] or "").strip()
                if not value:
                    raise ValueError(f"{field} cannot be empty")
                setattr(item, field, value)
        if "status" in arguments:
            state = str(arguments["status"])
            if state not in WORK_ITEM_STATUSES:
                raise ValueError("status is invalid")
            item.status = state
        if "priority" in arguments:
            priority = str(arguments["priority"])
            if priority not in WORK_ITEM_PRIORITIES:
                raise ValueError("priority is invalid")
            item.priority = priority
        if "acceptance_criteria" in arguments:
            criteria = [str(value).strip() for value in arguments["acceptance_criteria"] if str(value).strip()]
            if not criteria:
                raise ValueError("acceptance_criteria cannot be empty")
            item.acceptance_criteria = criteria
        if "assignee_agent_id" in arguments:
            assignee_id = _uuid(arguments.get("assignee_agent_id"), "assignee_agent_id", optional=True)
            await _enabled_member(db, attached, assignee_id)
            item.assignee_agent_id = assignee_id
        if "dependency_ids" in arguments:
            item.dependency_ids = await _dependency_ids(
                db, attached, list(arguments.get("dependency_ids") or []), item_id=item.id
            )
        after = {
            "title": item.title,
            "status": item.status,
            "priority": item.priority,
            "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
        }
        add_event(
            db,
            attached,
            "work_item.updated",
            f"{member.name_snapshot} updated work item {item.title}",
            actor_agent_id=agent_id,
            work_item_id=item.id,
            run_id=project_run.id if project_run else None,
            metadata={
                "before": before,
                "after": after,
                "progress_note": str(arguments.get("progress_note") or "").strip() or None,
                "evidence": [str(value) for value in arguments.get("evidence", [])],
                **trace_metadata,
            },
        )
        await db.commit()
        return json.dumps({"id": str(item.id), **after}, ensure_ascii=False)
