"""Low-risk project mutations delegated by an interactive platform user.

The caller owns authentication and resolves the authoritative user.  Every
command still receives an already ACL-checked project and applies the domain
invariants that also protect the project REST surface.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.project import Project, ProjectEvent, ProjectMemberSnapshot, ProjectRun, ProjectWorkItem
from app.models.user import User
from app.services.project_collaboration_prompt import build_project_group_task
from app.services.project_git_service import (
    commit_project_changes,
    project_user_git_email,
    reconcile_project_repository_operations,
    write_project_file,
)
from app.services.project_service import (
    add_event,
    ensure_project_group_session,
    ensure_project_running,
    freeze_run_members,
    project_execution_user_id,
)

WORK_ITEM_STATUSES = frozenset({"backlog", "todo", "in_progress", "review", "blocked", "done"})
WORK_ITEM_PRIORITIES = frozenset({"low", "medium", "high", "urgent"})
PROJECT_RUNTIME_SWITCH_STATUSES = frozenset({"running", "paused"})
PROJECT_RUNTIME_RESUMABLE_STATUSES = frozenset({"running", "paused", "waiting"})


def _git_head(project: Project, commit: str) -> None:
    settings = dict(project.settings or {})
    settings["git"] = {**dict(settings.get("git") or {}), "head": commit}
    project.settings = settings


async def _enabled_member(
    db: AsyncSession,
    project: Project,
    agent_id: uuid.UUID | None,
) -> ProjectMemberSnapshot | None:
    if agent_id is None:
        return None
    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == agent_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise ValueError("数字员工必须是已启用的项目成员")
    return member


async def _work_item_links(
    db: AsyncSession,
    project: Project,
    *,
    parent_id: uuid.UUID | None,
    dependency_ids: list[uuid.UUID],
    item_id: uuid.UUID | None = None,
) -> list[str]:
    linked_ids = set(dependency_ids)
    if parent_id is not None:
        linked_ids.add(parent_id)
    if item_id is not None and item_id in linked_ids:
        raise ValueError("A work item cannot depend on or parent itself")
    if linked_ids:
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
            raise ValueError("Every parent and dependency must belong to this project")
    return [str(value) for value in dependency_ids]


def _work_item_result(item: ProjectWorkItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "project_id": str(item.project_id),
        "title": item.title,
        "status": item.status,
        "priority": item.priority,
        "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
        "parent_id": str(item.parent_id) if item.parent_id else None,
        "due_at": item.due_at.isoformat() if item.due_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


async def create_work_item(
    db: AsyncSession,
    actor: User,
    project: Project,
    *,
    title: str,
    description: str,
    status: str,
    priority: str,
    acceptance_criteria: list[str],
    assignee_agent_id: uuid.UUID | None,
    parent_id: uuid.UUID | None,
    dependency_ids: list[uuid.UUID],
    due_at: datetime | None,
) -> dict[str, Any]:
    if status not in WORK_ITEM_STATUSES:
        raise ValueError("Unknown work item status")
    if priority not in WORK_ITEM_PRIORITIES:
        raise ValueError("Unknown work item priority")
    await _enabled_member(db, project, assignee_agent_id)
    dependencies = await _work_item_links(
        db,
        project,
        parent_id=parent_id,
        dependency_ids=dependency_ids,
    )
    item = ProjectWorkItem(
        tenant_id=project.tenant_id,
        project_id=project.id,
        created_by_user_id=actor.id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        acceptance_criteria=acceptance_criteria,
        assignee_agent_id=assignee_agent_id,
        parent_id=parent_id,
        dependency_ids=dependencies,
        due_at=due_at,
    )
    db.add(item)
    await db.flush()
    add_event(
        db,
        project,
        "work_item.created",
        f"Created work item {item.title}",
        actor_user_id=actor.id,
        work_item_id=item.id,
        metadata={
            "status": item.status,
            "priority": item.priority,
            "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
        },
    )
    await db.commit()
    await db.refresh(item)
    return _work_item_result(item)


async def update_work_item(
    db: AsyncSession,
    actor: User,
    project: Project,
    item_id: uuid.UUID,
    updates: dict[str, Any],
) -> dict[str, Any]:
    item = (
        await db.execute(
            select(ProjectWorkItem)
            .where(
                ProjectWorkItem.id == item_id,
                ProjectWorkItem.project_id == project.id,
                ProjectWorkItem.tenant_id == project.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise ValueError("Work item not found")
    if not updates:
        raise ValueError("At least one work item field is required")
    before = {
        "title": item.title,
        "status": item.status,
        "priority": item.priority,
        "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
    }
    if "title" in updates:
        item.title = updates["title"]
    if "description" in updates:
        item.description = updates["description"]
    if "status" in updates:
        if updates["status"] not in WORK_ITEM_STATUSES:
            raise ValueError("Unknown work item status")
        item.status = updates["status"]
    if "priority" in updates:
        if updates["priority"] not in WORK_ITEM_PRIORITIES:
            raise ValueError("Unknown work item priority")
        item.priority = updates["priority"]
    if "assignee_agent_id" in updates:
        await _enabled_member(db, project, updates["assignee_agent_id"])
        item.assignee_agent_id = updates["assignee_agent_id"]
    if "acceptance_criteria" in updates:
        item.acceptance_criteria = updates["acceptance_criteria"]
    if "dependency_ids" in updates:
        item.dependency_ids = await _work_item_links(
            db,
            project,
            parent_id=item.parent_id,
            dependency_ids=updates["dependency_ids"],
            item_id=item.id,
        )
    if "due_at" in updates:
        item.due_at = updates["due_at"]
    after = {
        "title": item.title,
        "status": item.status,
        "priority": item.priority,
        "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
    }
    changed_fields = sorted(key for key in after if before[key] != after[key])
    changed_fields.extend(
        key for key in ("description", "acceptance_criteria", "dependency_ids", "due_at") if key in updates
    )
    add_event(
        db,
        project,
        "work_item.updated",
        f"Updated work item {item.title}",
        actor_user_id=actor.id,
        work_item_id=item.id,
        metadata={"before": before, "after": after, "changed_fields": sorted(set(changed_fields))},
    )
    await db.commit()
    await db.refresh(item)
    return _work_item_result(item)


async def update_project_runtime_status(
    db: AsyncSession,
    actor: User,
    project: Project,
    status: str,
) -> dict[str, Any]:
    if status not in PROJECT_RUNTIME_SWITCH_STATUSES:
        raise ValueError("Project status must be running or paused")
    await db.refresh(project, with_for_update=True)
    if project.status not in PROJECT_RUNTIME_RESUMABLE_STATUSES:
        raise ValueError("The project runtime switch is unavailable in the current project state")
    previous = project.status
    if status == previous:
        return {"project_id": str(project.id), "status": status, "changed": False}
    project.status = status
    add_event(
        db,
        project,
        "project.paused" if status == "paused" else "project.resumed",
        "Paused all new project work" if status == "paused" else "Resumed project work",
        actor_user_id=actor.id,
        metadata={"previous_status": previous},
    )
    await db.commit()
    await db.refresh(project)
    return {"project_id": str(project.id), "status": project.status, "changed": True}


def _operation_key(kind: str, project: Project, actor: User, token: str, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        {
            "kind": kind,
            "project_id": str(project.id),
            "actor_user_id": str(actor.id),
            "token": token,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"user-project-{kind}-v1-{hashlib.sha256(encoded).hexdigest()}"


async def write_text_file(
    db: AsyncSession,
    actor: User,
    project: Project,
    *,
    path: str,
    content: str,
) -> dict[str, Any]:
    await reconcile_project_repository_operations(project.id, db=db)
    result = await write_project_file(
        project,
        path,
        content,
        author_name=actor.display_name,
        author_email=project_user_git_email(actor.id),
    )
    _git_head(project, result["commit"])
    event = add_event(
        db,
        project,
        "project.file.committed",
        f"Wrote and committed project file {result['path']}",
        actor_user_id=actor.id,
        metadata={
            "path": result["path"],
            "commit": result["commit"],
            "operation": "write_and_commit",
        },
    )
    await db.commit()
    return {
        "status": result["status"],
        "path": result["path"],
        "commit": result["commit"],
        "event_id": str(event.id),
    }


async def create_milestone(
    db: AsyncSession,
    actor: User,
    project: Project,
    *,
    message: str,
    paths: list[str],
    operation_token: str,
) -> dict[str, Any]:
    await reconcile_project_repository_operations(project.id, db=db)
    operation_key = _operation_key(
        "milestone",
        project,
        actor,
        operation_token,
        {"message": message, "paths": sorted(paths)},
    )
    event = (
        await db.execute(
            select(ProjectEvent).where(
                ProjectEvent.project_id == project.id,
                ProjectEvent.tenant_id == project.tenant_id,
                ProjectEvent.event_type.in_(["git.milestone.prepared", "git.milestone.created"]),
                ProjectEvent.event_metadata["operation_key"].as_string() == operation_key,
            )
        )
    ).scalar_one_or_none()
    if event is not None and event.event_type == "git.milestone.created":
        metadata = dict(event.event_metadata or {})
        return {
            "status": "completed",
            "commit": metadata.get("commit"),
            "paths": list(metadata.get("paths") or []),
            "event_id": str(event.id),
            "idempotent_replay": True,
        }
    if event is None:
        event = add_event(
            db,
            project,
            "git.milestone.prepared",
            "Prepared a delivery milestone",
            actor_user_id=actor.id,
            metadata={
                "operation_key": operation_key,
                "milestone_state": "prepared",
                "milestone_message": message,
                "paths": paths,
            },
        )
        await db.flush()
    event_id = event.id
    await db.commit()
    result = await commit_project_changes(
        project,
        message,
        paths,
        milestone=True,
        operation_key=operation_key,
        author_name=actor.display_name,
        author_email=project_user_git_email(actor.id),
    )
    attached = await db.get(Project, project.id)
    event = await db.get(ProjectEvent, event_id)
    if attached is None or event is None:
        raise RuntimeError("Milestone operation journal is unavailable")
    _git_head(attached, result["commit"])
    event.event_type = "git.milestone.created"
    event.summary = f"Created delivery milestone {result['commit'][:12]}"
    event.event_metadata = {
        **dict(event.event_metadata or {}),
        **result,
        "operation_key": operation_key,
        "milestone_state": "created",
        "milestone_message": message,
    }
    await db.commit()
    return {
        "status": result["status"],
        "commit": result["commit"],
        "paths": result.get("paths"),
        "changed": bool(result.get("changed")),
        "event_id": str(event.id),
        "idempotent_replay": bool(result.get("idempotent_replay")),
    }


async def _dispatch_run(run: ProjectRun) -> bool:
    # Local import avoids the established project_service/subagent_runtime cycle.
    from app.services.subagent_runtime import dispatch_project_run

    try:
        await dispatch_project_run(run.id)
        return False
    except Exception as exc:  # noqa: BLE001 - the durable queued outbox remains retryable
        logger.warning("User project run dispatch deferred run={} error={}", run.id, exc)
        return True


async def start_run(
    db: AsyncSession,
    actor: User,
    project: Project,
    *,
    instruction: str,
    title: str,
    work_item_id: uuid.UUID | None,
    agent_id: uuid.UUID | None,
    group_message: bool,
    operation_token: str,
) -> dict[str, Any]:
    ensure_project_running(project)
    work_item = None
    if work_item_id is not None:
        work_item = (
            await db.execute(
                select(ProjectWorkItem).where(
                    ProjectWorkItem.id == work_item_id,
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.tenant_id == project.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if work_item is None:
            raise ValueError("Work item not found")
    requested_agent_id = None if group_message else agent_id or (work_item.assignee_agent_id if work_item else None)
    conditions = [
        ProjectMemberSnapshot.project_id == project.id,
        ProjectMemberSnapshot.tenant_id == project.tenant_id,
        ProjectMemberSnapshot.is_enabled.is_(True),
    ]
    if requested_agent_id is None:
        conditions.append(ProjectMemberSnapshot.is_leader.is_(True))
    else:
        conditions.append(ProjectMemberSnapshot.agent_id == requested_agent_id)
    member = (
        await db.execute(
            select(ProjectMemberSnapshot)
            .where(*conditions)
            .order_by(ProjectMemberSnapshot.is_leader.desc(), ProjectMemberSnapshot.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if member is None:
        raise ValueError("项目需要一名已启用的负责人或指定的项目数字员工")
    task = instruction
    if work_item is not None:
        criteria = "\n".join(f"- {item}" for item in (work_item.acceptance_criteria or [])) or "- None recorded"
        work_context = (
            f"Execute project work item {work_item.id}: {work_item.title}\n\n"
            f"Description:\n{work_item.description or '(none)'}\n\n"
            f"Acceptance criteria:\n{criteria}"
        )
        task = f"{work_context}\n\nAdditional instruction:\n{task}" if task else work_context
    if not task:
        raise ValueError("A work item or instruction is required")
    run_title = title or (work_item.title if work_item else "") or task.splitlines()[0]
    run_title = run_title.strip()[:120]
    session = await ensure_project_group_session(db, project)
    event_key = _operation_key(
        "group-message" if group_message else "run",
        project,
        actor,
        operation_token,
        {
            "instruction": instruction,
            "title": run_title,
            "work_item_id": str(work_item_id) if work_item_id else None,
            "agent_id": str(agent_id) if agent_id else None,
        },
    )
    existing = (
        await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == event_key))
    ).scalar_one_or_none()
    if existing is not None:
        run_anchor = (
            ProjectRun.input["group_message_id"].as_string() == str(existing.id)
            if group_message
            else ProjectRun.input["dispatch"]["turn_anchor_id"].as_string() == str(existing.id)
        )
        existing_run = (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project.id,
                    run_anchor,
                )
            )
        ).scalar_one_or_none()
        if existing_run is None:
            raise RuntimeError("Project run is unavailable")
        await db.commit()
        dispatch_deferred = await _dispatch_run(existing_run)
        await db.refresh(existing_run)
        return {
            "message_id": str(existing.id),
            "run_id": str(existing_run.id),
            "status": existing_run.status,
            "digital_employee_id": str(existing_run.agent_id),
            "dispatch_deferred": dispatch_deferred,
            "idempotent_replay": True,
        }
    anchor = ChatMessage(
        id=uuid.uuid4(),
        agent_id=session.agent_id,
        user_id=actor.id,
        sender_user_id=actor.id,
        role="user",
        content=instruction if group_message else task,
        conversation_id=str(session.id),
        external_event_key=event_key,
        message_meta={
            "kind": "project_group_message" if group_message else "project_run_request",
            "project_id": str(project.id),
            "visible_to_group": True,
            "mentions": [],
            "default_leader_agent_id": str(member.agent_id) if group_message else None,
            "attachments": [],
            "work_item_id": str(work_item_id) if work_item_id else None,
            "awakened_agent_ids": [],
            "subagent_runs": [],
            "wake_policy": "default_leader" if group_message else "single_explicit_or_default_leader",
            "initiator_user_id": str(actor.id),
            "target_agent_id": str(member.agent_id),
        },
    )
    dispatch_task = build_project_group_task(instruction, is_owner=True) if group_message else task
    run = ProjectRun(
        tenant_id=project.tenant_id,
        project_id=project.id,
        work_item_id=work_item_id,
        agent_id=member.agent_id,
        initiated_by_user_id=actor.id,
        execution_user_id=project_execution_user_id(project),
        status="queued",
        trigger_type="group_leader_message" if group_message else "manual",
        input={
            "title": run_title,
            "group_session_id": str(session.id),
            "group_message_id": str(anchor.id) if group_message else None,
            "dispatch": {
                "group_session_id": str(session.id),
                "project_member_id": str(member.id),
                "turn_anchor_id": str(anchor.id),
                "task": dispatch_task,
            },
        },
        output={"group_session_id": str(session.id)},
    )
    db.add_all([anchor, run])
    session.last_message_at = func.now()
    await db.flush()
    await freeze_run_members(db, project, run)
    add_event(
        db,
        project,
        "group.message.created" if group_message else "run.queued",
        "Appended a project group message and notified the project owner"
        if group_message
        else "Queued project run and froze run snapshots",
        actor_user_id=actor.id,
        actor_agent_id=run.agent_id,
        work_item_id=run.work_item_id,
        run_id=run.id,
        metadata={
            "group_session_id": str(session.id),
            "group_message_id": str(anchor.id) if group_message else None,
            "target_agent_id": str(member.agent_id),
            "dispatch_policy": "single_owner" if group_message else "single_agent",
        },
    )
    await db.commit()
    dispatch_deferred = await _dispatch_run(run)
    await db.refresh(run)
    return {
        "message_id": str(anchor.id),
        "run_id": str(run.id),
        "status": run.status,
        "digital_employee_id": str(run.agent_id),
        "work_item_id": str(run.work_item_id) if run.work_item_id else None,
        "dispatch_deferred": dispatch_deferred,
        "idempotent_replay": False,
    }
