"""Human-delegated project management tools for standard digital employees.

These tools intentionally use the current interactive Human as the project
principal.  An Agent's creator, project membership, or background execution
identity never grants project visibility here.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.permissions import get_agent_access_level_for_user_id
from app.database import async_session
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.project import (
    Project,
    ProjectAccessGrant,
    ProjectEvent,
    ProjectMemberSnapshot,
    ProjectRun,
    ProjectWorkItem,
)
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.services.activity_logger import log_activity
from app.services.execution_identity import is_human_interactive_turn
from app.services.project_agent_workspace import _is_sensitive_asset
from app.services.project_git_service import (
    list_project_files,
    project_commit_diff,
    read_project_file,
    repository_state,
)
from app.services.project_service import (
    accessible_projects_clause,
    add_event,
    require_owner,
    require_project,
)
from app.services.project_user_commands import (
    create_milestone,
    create_work_item,
    start_run,
    update_project_runtime_status,
    update_work_item,
    write_text_file,
)

from app.services.user_project_tool_catalog import (
    USER_PROJECT_MUTATION_TOOL_NAMES,
    USER_PROJECT_TOOL_NAMES,
    USER_PROJECT_TOOL_SCHEMAS,
    USER_PROJECT_TOOL_SEEDS,
    _RUN_STATUSES,
    _WORK_ITEM_STATUSES,
    _parameters,
    _seed,
)
from app.services.timezone_utils import (
    add_local_datetime_projections,
    get_agent_timezone_in_session,
    normalize_datetime_arguments,
)


from app.services.user_project_tool_support import (
    _bounded_int,
    _bounded_list,
    _clip,
    _datetime,
    _get_project,
    _interactive_actor,
    _iso,
    _json,
    _optional_uuid,
    _page_metadata,
    _project_access_role,
    _safe_file_path,
    _search_projects,
    _text,
    _uuid,
    record_user_project_tool_activity,
)


def _work_item_payload(item: ProjectWorkItem, *, detail: bool) -> dict[str, Any]:
    payload = {
        "id": str(item.id),
        "project_id": str(item.project_id),
        "title": item.title,
        "status": item.status,
        "priority": item.priority,
        "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
        "parent_id": str(item.parent_id) if item.parent_id else None,
        "due_at": _iso(item.due_at),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }
    if detail:
        payload.update(
            {
                "description": _clip(item.description, 6000)[0],
                "acceptance_criteria": [_clip(value, 1200)[0] for value in list(item.acceptance_criteria or [])[:30]],
                "dependency_ids": [str(value) for value in list(item.dependency_ids or [])[:100]],
            }
        )
    return payload


async def _list_work_items(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(db, actor, _uuid(arguments.get("project_id"), "project_id"))
    offset = _bounded_int(arguments.get("offset"), default=0, minimum=0, maximum=5000)
    limit = _bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=50)
    stmt = select(ProjectWorkItem).where(
        ProjectWorkItem.project_id == project.id,
        ProjectWorkItem.tenant_id == project.tenant_id,
    )
    status = str(arguments.get("status") or "").strip()
    if status and status not in _WORK_ITEM_STATUSES:
        raise ValueError("Unknown work item status")
    if status:
        stmt = stmt.where(ProjectWorkItem.status == status)
    assignee_id = _optional_uuid(arguments.get("assignee_agent_id"), "assignee_agent_id")
    if assignee_id:
        stmt = stmt.where(ProjectWorkItem.assignee_agent_id == assignee_id)
    rows = (
        (
            await db.execute(
                stmt.order_by(ProjectWorkItem.updated_at.desc(), ProjectWorkItem.id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [_work_item_payload(item, detail=False) for item in rows[:limit]],
        **_page_metadata(offset=offset, limit=limit, has_more=len(rows) > limit),
    }


async def _get_work_item(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(db, actor, _uuid(arguments.get("project_id"), "project_id"))
    item = (
        await db.execute(
            select(ProjectWorkItem).where(
                ProjectWorkItem.id == _uuid(arguments.get("work_item_id"), "work_item_id"),
                ProjectWorkItem.project_id == project.id,
                ProjectWorkItem.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise ValueError("Work item not found")
    return _work_item_payload(item, detail=True)


async def _list_runs(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(db, actor, _uuid(arguments.get("project_id"), "project_id"))
    offset = _bounded_int(arguments.get("offset"), default=0, minimum=0, maximum=5000)
    limit = _bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=50)
    stmt = select(ProjectRun).where(
        ProjectRun.project_id == project.id,
        ProjectRun.tenant_id == project.tenant_id,
    )
    for field, column in (
        ("run_id", ProjectRun.id),
        ("work_item_id", ProjectRun.work_item_id),
        ("agent_id", ProjectRun.agent_id),
    ):
        value = _optional_uuid(arguments.get(field), field)
        if value:
            stmt = stmt.where(column == value)
    status = str(arguments.get("status") or "").strip()
    if status and status not in _RUN_STATUSES:
        raise ValueError("Unknown project run status")
    if status:
        stmt = stmt.where(ProjectRun.status == status)
    rows = (
        (
            await db.execute(
                stmt.order_by(ProjectRun.created_at.desc(), ProjectRun.id.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(run.id),
                "project_id": str(run.project_id),
                "work_item_id": str(run.work_item_id) if run.work_item_id else None,
                "agent_id": str(run.agent_id) if run.agent_id else None,
                "status": run.status,
                "trigger_type": run.trigger_type,
                "title": _clip(dict(run.input or {}).get("title"), 500)[0] or None,
                "has_error": bool(run.error),
                "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at),
                "created_at": _iso(run.created_at),
                "updated_at": _iso(run.updated_at),
            }
            for run in rows[:limit]
        ],
        **_page_metadata(offset=offset, limit=limit, has_more=len(rows) > limit),
    }


async def _list_members(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(db, actor, _uuid(arguments.get("project_id"), "project_id"))
    offset = _bounded_int(arguments.get("offset"), default=0, minimum=0, maximum=5000)
    limit = _bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=50)
    stmt = select(ProjectMemberSnapshot).where(
        ProjectMemberSnapshot.project_id == project.id,
        ProjectMemberSnapshot.tenant_id == project.tenant_id,
    )
    if not bool(arguments.get("include_disabled", False)):
        stmt = stmt.where(ProjectMemberSnapshot.is_enabled.is_(True))
    rows = (
        (
            await db.execute(
                stmt.order_by(
                    ProjectMemberSnapshot.is_leader.desc(),
                    ProjectMemberSnapshot.created_at,
                    ProjectMemberSnapshot.id,
                )
                .offset(offset)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": str(member.id),
                "agent_id": str(member.agent_id),
                "name": member.name_snapshot,
                "role_description": _clip(member.role_snapshot, 1000)[0],
                "is_owner": member.is_leader,
                "is_enabled": member.is_enabled,
                "created_at": _iso(member.created_at),
                "updated_at": _iso(member.updated_at),
            }
            for member in rows[:limit]
        ],
        **_page_metadata(offset=offset, limit=limit, has_more=len(rows) > limit),
    }


async def _list_milestones(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(db, actor, _uuid(arguments.get("project_id"), "project_id"))
    offset = _bounded_int(arguments.get("offset"), default=0, minimum=0, maximum=5000)
    limit = _bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=50)
    events = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(
                    ProjectEvent.project_id == project.id,
                    ProjectEvent.tenant_id == project.tenant_id,
                    ProjectEvent.event_type == "git.milestone.created",
                )
                .order_by(ProjectEvent.created_at.desc(), ProjectEvent.id.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    state = await repository_state(project, min(5000, max(100, (offset + limit) * 20)))
    commits = {str(item.get("commit") or ""): item for item in list(state.get("commits") or [])}
    items = []
    for event in events:
        metadata = dict(event.event_metadata or {})
        commit_hash = str(metadata.get("commit") or "")
        commit = commits.get(commit_hash)
        if commit is None:
            continue
        items.append(
            {
                "id": str(event.id),
                "commit": commit_hash,
                "short_commit": commit.get("short_commit") or commit_hash[:12],
                "message": _clip(
                    metadata.get("milestone_message")
                    or metadata.get("description")
                    or commit.get("message")
                    or event.summary,
                    1000,
                )[0],
                "author": _clip(commit.get("author"), 200)[0] or None,
                "work_item_id": str(event.work_item_id) if event.work_item_id else None,
                "run_id": str(event.run_id) if event.run_id else None,
                "agent_id": str(event.actor_agent_id) if event.actor_agent_id else None,
                "paths": [str(path) for path in list(metadata.get("paths") or [])[:100] if _safe_file_path(str(path))],
                "created_at": _iso(event.created_at),
            }
        )
        if len(items) >= limit + 1:
            break
    return {
        "items": items[:limit],
        **_page_metadata(offset=offset, limit=limit, has_more=len(items) > limit),
    }


async def _list_files(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(db, actor, _uuid(arguments.get("project_id"), "project_id"))
    offset = _bounded_int(arguments.get("offset"), default=0, minimum=0, maximum=5000)
    limit = _bounded_int(arguments.get("limit"), default=100, minimum=1, maximum=200)
    prefix = str(arguments.get("path_prefix") or "").strip().strip("/")
    if prefix and not _safe_file_path(prefix):
        raise ValueError("Project file path is unavailable")
    records = await list_project_files(project)
    visible = [
        record
        for record in records
        if _safe_file_path(str(record.get("path") or ""))
        and (
            not prefix
            or str(record.get("path") or "") == prefix
            or str(record.get("path") or "").startswith(f"{prefix}/")
        )
    ]
    items = [
        {
            "path": record["path"],
            "name": record["name"],
            "size": record["size"],
            "head": record["head"],
            "last_commit": record["commit"],
            "mime_type": record["mime_type"],
            "kind": record["kind"],
            "is_text": record["is_text"],
        }
        for record in visible[offset : offset + limit]
    ]
    return {
        "items": items,
        **_page_metadata(offset=offset, limit=limit, has_more=len(visible) > offset + limit),
    }


async def _read_file(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(db, actor, _uuid(arguments.get("project_id"), "project_id"))
    path = str(arguments.get("path") or "").strip()
    if not _safe_file_path(path):
        raise ValueError("Project file path is unavailable")
    max_chars = _bounded_int(arguments.get("max_chars"), default=12000, minimum=1, maximum=20000)
    return await read_project_file(project, path, max_chars=max_chars)


async def _get_git(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(db, actor, _uuid(arguments.get("project_id"), "project_id"))
    offset = _bounded_int(arguments.get("offset"), default=0, minimum=0, maximum=5000)
    limit = _bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=50)
    state = await repository_state(project, offset + limit + 1)
    commits = list(state.get("commits") or [])
    return {
        "mode": state.get("mode"),
        "head": state.get("head"),
        "branches": [str(branch) for branch in list(state.get("branches") or [])[:50]],
        "file_count": len(list(state.get("files") or [])),
        "commits": [
            {
                "commit": commit.get("commit"),
                "short_commit": commit.get("short_commit"),
                "author": _clip(commit.get("author"), 200)[0],
                "created_at": commit.get("created_at"),
                "message": _clip(commit.get("message"), 1000)[0],
            }
            for commit in commits[offset : offset + limit]
        ],
        **_page_metadata(offset=offset, limit=limit, has_more=len(commits) > offset + limit),
    }


async def _get_git_diff(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(db, actor, _uuid(arguments.get("project_id"), "project_id"))
    path = str(arguments.get("path") or "").strip() or None
    if path is not None and not _safe_file_path(path):
        raise ValueError("Project file path is unavailable")
    max_bytes = _bounded_int(arguments.get("max_patch_bytes"), default=32768, minimum=1, maximum=65536)
    result = await project_commit_diff(
        project,
        str(arguments.get("commit") or ""),
        str(arguments.get("parent") or "") or None,
        path,
        max_bytes,
    )
    files = [
        {
            "path": item.get("path"),
            "status": item.get("status"),
            "additions": item.get("additions"),
            "deletions": item.get("deletions"),
            "binary": item.get("binary"),
            "original_size": item.get("original_size"),
            "modified_size": item.get("modified_size"),
        }
        for item in list(result.get("files") or [])
        if _safe_file_path(str(item.get("path") or ""))
    ]
    return {
        "commit": result.get("commit"),
        "parent": result.get("parent"),
        "is_root": result.get("is_root"),
        "path": result.get("path"),
        "patch": result.get("patch") if path is not None else None,
        "patch_truncated": result.get("patch_truncated") if path is not None else False,
        "patch_bytes": result.get("patch_bytes") if path is not None else 0,
        "files": files[:200],
        "files_truncated": bool(result.get("files_truncated")) or len(files) > 200,
        "patch_note": None if path is not None else "Provide path to read bounded patch text.",
    }


async def _list_messages(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(db, actor, _uuid(arguments.get("project_id"), "project_id"))
    limit = _bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=20)
    max_chars = _bounded_int(
        arguments.get("max_chars_per_message"),
        default=2000,
        minimum=100,
        maximum=3000,
    )
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.project_id == project.id,
                ChatSession.agent_id.is_not(None),
                ChatSession.source_channel == "project",
                ChatSession.is_group.is_(True),
            )
        )
    ).scalar_one_or_none()
    if session is None:
        return {"items": [], "has_more": False, "next_before_message_id": None}
    stmt = select(ChatMessage).where(
        ChatMessage.conversation_id == str(session.id),
        ChatMessage.role.in_(["user", "assistant"]),
    )
    before_id = _optional_uuid(arguments.get("before_message_id"), "before_message_id")
    if before_id is not None:
        before = await db.get(ChatMessage, before_id)
        if before is None or before.conversation_id != str(session.id):
            raise ValueError("Message cursor not found")
        stmt = stmt.where(
            or_(
                ChatMessage.created_at < before.created_at,
                and_(ChatMessage.created_at == before.created_at, ChatMessage.id < before.id),
            )
        )
    rows = (
        (await db.execute(stmt.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(limit + 1)))
        .scalars()
        .all()
    )
    page = rows[:limit]
    user_ids = {row.sender_user_id for row in page if row.sender_user_id}
    agent_ids = {row.sender_agent_id for row in page if row.sender_agent_id}
    user_names = (
        dict((await db.execute(select(User.id, User.display_name).where(User.id.in_(user_ids)))).all())
        if user_ids
        else {}
    )
    agent_names = (
        dict(
            (
                await db.execute(
                    select(ProjectMemberSnapshot.agent_id, ProjectMemberSnapshot.name_snapshot).where(
                        ProjectMemberSnapshot.project_id == project.id,
                        ProjectMemberSnapshot.tenant_id == project.tenant_id,
                        ProjectMemberSnapshot.agent_id.in_(agent_ids),
                    )
                )
            ).all()
        )
        if agent_ids
        else {}
    )
    items = []
    for row in reversed(page):
        content, truncated = _clip(row.content, max_chars)
        items.append(
            {
                "id": str(row.id),
                "role": row.role,
                "sender_type": "user" if row.sender_user_id else "digital_employee",
                "sender_id": str(row.sender_user_id or row.sender_agent_id or "") or None,
                "sender_name": user_names.get(row.sender_user_id) or agent_names.get(row.sender_agent_id),
                "content": content,
                "content_truncated": truncated,
                "created_at": _iso(row.created_at),
            }
        )
    return {
        "items": items,
        "has_more": len(rows) > limit,
        "next_before_message_id": str(page[-1].id) if page and len(rows) > limit else None,
    }


async def _create_work_item(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(
        db,
        actor,
        _uuid(arguments.get("project_id"), "project_id"),
        edit=True,
        lock=True,
    )
    criteria = [
        _text(value, "acceptance_criterion", maximum=1000, required=True)
        for value in _bounded_list(arguments.get("acceptance_criteria"), "acceptance_criteria", 30)
    ]
    dependencies = [
        _uuid(value, "dependency_id") for value in _bounded_list(arguments.get("dependency_ids"), "dependency_ids", 100)
    ]
    return await create_work_item(
        db,
        actor,
        project,
        title=_text(arguments.get("title"), "title", maximum=500, required=True),
        description=_text(arguments.get("description"), "description", maximum=20000),
        status=str(arguments.get("status") or "backlog"),
        priority=str(arguments.get("priority") or "medium"),
        acceptance_criteria=criteria,
        assignee_agent_id=_optional_uuid(arguments.get("assignee_agent_id"), "assignee_agent_id"),
        parent_id=_optional_uuid(arguments.get("parent_id"), "parent_id"),
        dependency_ids=dependencies,
        due_at=_datetime(arguments.get("due_at"), "due_at"),
    )


async def _update_work_item(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(
        db,
        actor,
        _uuid(arguments.get("project_id"), "project_id"),
        edit=True,
        lock=True,
    )
    updates: dict[str, Any] = {}
    if "title" in arguments:
        updates["title"] = _text(arguments["title"], "title", maximum=500, required=True)
    if "description" in arguments:
        updates["description"] = _text(arguments["description"], "description", maximum=20000)
    for field in ("status", "priority"):
        if field in arguments:
            updates[field] = str(arguments[field])
    if "assignee_agent_id" in arguments:
        updates["assignee_agent_id"] = _optional_uuid(arguments["assignee_agent_id"], "assignee_agent_id")
    if "acceptance_criteria" in arguments:
        updates["acceptance_criteria"] = [
            _text(value, "acceptance_criterion", maximum=1000, required=True)
            for value in _bounded_list(arguments.get("acceptance_criteria"), "acceptance_criteria", 30)
        ]
    if "dependency_ids" in arguments:
        updates["dependency_ids"] = [
            _uuid(value, "dependency_id")
            for value in _bounded_list(arguments.get("dependency_ids"), "dependency_ids", 100)
        ]
    if "due_at" in arguments:
        updates["due_at"] = _datetime(arguments["due_at"], "due_at")
    return await update_work_item(
        db,
        actor,
        project,
        _uuid(arguments.get("work_item_id"), "work_item_id"),
        updates,
    )


async def _start_run(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(
        db,
        actor,
        _uuid(arguments.get("project_id"), "project_id"),
        edit=True,
        lock=True,
    )
    return await start_run(
        db,
        actor,
        project,
        instruction=_text(arguments.get("instruction"), "instruction", maximum=10000),
        title=_text(arguments.get("title"), "title", maximum=120),
        work_item_id=_optional_uuid(arguments.get("work_item_id"), "work_item_id"),
        agent_id=_optional_uuid(arguments.get("agent_id"), "agent_id"),
        group_message=False,
        operation_token=str(arguments.get("_turn_anchor_id") or ""),
    )


async def _send_message(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(
        db,
        actor,
        _uuid(arguments.get("project_id"), "project_id"),
        edit=True,
        lock=True,
    )
    content = _text(arguments.get("content"), "content", maximum=10000, required=True)
    return await start_run(
        db,
        actor,
        project,
        instruction=content,
        title=content.splitlines()[0][:120],
        work_item_id=_optional_uuid(arguments.get("work_item_id"), "work_item_id"),
        agent_id=None,
        group_message=True,
        operation_token=str(arguments.get("_turn_anchor_id") or ""),
    )


async def _create_milestone(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(
        db,
        actor,
        _uuid(arguments.get("project_id"), "project_id"),
        edit=True,
        lock=True,
    )
    paths = [
        _text(value, "path", maximum=1024, required=True)
        for value in _bounded_list(arguments.get("paths"), "paths", 100)
    ]
    if not paths or len(paths) > 100 or len(paths) != len(set(paths)):
        raise ValueError("paths must contain between 1 and 100 unique project paths")
    if any(not _safe_file_path(path) for path in paths):
        raise ValueError("A selected project path is unavailable")
    return await create_milestone(
        db,
        actor,
        project,
        message=_text(arguments.get("message"), "message", maximum=500, required=True),
        paths=paths,
        operation_token=str(arguments.get("_turn_anchor_id") or ""),
    )


async def _write_file(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_project(
        db,
        actor,
        _uuid(arguments.get("project_id"), "project_id"),
        edit=True,
        lock=True,
    )
    path = _text(arguments.get("path"), "path", maximum=1024, required=True)
    parts = PurePosixPath(path).parts
    if not _safe_file_path(path) or (parts and parts[0].casefold() in {".agents", ".git"}):
        raise ValueError("Project file path is unavailable for writing")
    content = str(arguments.get("content") or "")
    if len(content) > 50000 or len(content.encode("utf-8")) > 200000:
        raise ValueError("content exceeds the project text write limit")
    return await write_text_file(db, actor, project, path=path, content=content)


async def _update_status(db, actor: User, arguments: dict[str, Any]) -> dict[str, Any]:
    project = await require_owner(
        db,
        actor,
        _uuid(arguments.get("project_id"), "project_id"),
        lock=True,
    )
    return await update_project_runtime_status(
        db,
        actor,
        project,
        str(arguments.get("status") or ""),
    )


_HANDLERS = {
    "user_project_search": _search_projects,
    "user_project_get": _get_project,
    "user_project_work_item_list": _list_work_items,
    "user_project_work_item_get": _get_work_item,
    "user_project_run_list": _list_runs,
    "user_project_member_list": _list_members,
    "user_project_milestone_list": _list_milestones,
    "user_project_file_list": _list_files,
    "user_project_file_read": _read_file,
    "user_project_git_get": _get_git,
    "user_project_git_diff": _get_git_diff,
    "user_project_message_list": _list_messages,
    "user_project_work_item_create": _create_work_item,
    "user_project_work_item_update": _update_work_item,
    "user_project_run_start": _start_run,
    "user_project_message_send": _send_message,
    "user_project_milestone_create": _create_milestone,
    "user_project_file_write": _write_file,
    "user_project_status_update": _update_status,
}


async def execute_user_project_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    agent_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_id: str,
    tool_call_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> str:
    """Execute one manually enabled project query as the current Human sender."""

    if tool_name not in USER_PROJECT_TOOL_NAMES:
        raise ValueError(f"Unknown user project tool: {tool_name}")
    async with async_session() as db:
        agent, actor = await _interactive_actor(
            db,
            tool_name=tool_name,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
        timezone_name = await get_agent_timezone_in_session(db, agent)
        handler_arguments = {
            **normalize_datetime_arguments(arguments, timezone_name, "due_at"),
            "_turn_anchor_id": str(turn_anchor_id),
        }
        result = await _HANDLERS[tool_name](db, actor, handler_arguments)
        project_id = _optional_uuid(arguments.get("project_id"), "project_id")
        if project_id is not None and tool_name in USER_PROJECT_MUTATION_TOOL_NAMES:
            project = await db.get(Project, project_id)
            if project is not None and project.tenant_id == actor.tenant_id:
                result_work_item_value = result.get("work_item_id") or arguments.get("work_item_id")
                if tool_name == "user_project_work_item_create":
                    result_work_item_value = result.get("id")
                result_work_item_id = _optional_uuid(result_work_item_value, "work_item_id")
                result_run_id = _optional_uuid(result.get("run_id"), "run_id")
                add_event(
                    db,
                    project,
                    "project.management.action.completed",
                    "Project collaboration action completed",
                    actor_user_id=actor.id,
                    actor_agent_id=agent.id,
                    work_item_id=result_work_item_id,
                    run_id=result_run_id,
                    metadata={
                        "session_id": str(session_id),
                        "anchor_message_id": str(turn_anchor_id),
                        "turn_anchor_id": str(turn_anchor_id),
                        "tool_call_id": str(tool_call_id or ""),
                        "tool_name": tool_name,
                        "outcome": "completed",
                    },
                )
                try:
                    await db.commit()
                except SQLAlchemyError:
                    await db.rollback()
                    logger.exception(
                        "[UserProjectTool] Failed to append project event for {}",
                        tool_name,
                    )
        await record_user_project_tool_activity(
            agent_id=agent.id,
            tool_name=tool_name,
            outcome="completed",
            user_id=actor.id,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
            tool_call_id=tool_call_id,
            project_id=project_id,
        )
        return _json(add_local_datetime_projections(result, timezone_name))


def user_project_tool_error(exc: Exception) -> str:
    """Return a truthful but non-leaking error for the shared tool dispatcher."""

    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc)
