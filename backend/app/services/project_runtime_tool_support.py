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
    PROJECT_FILE_WORKSPACES,
    PROJECT_STANDARD_FILE_TOOL_NAMES,
    PROJECT_STRUCTURED_READ_TOOL_NAMES,
    PROJECT_MEMBER_NAME_MAX_CHARS,
    PROJECT_MEMBER_ROLE_MAX_CHARS,
    WORK_ITEM_EVIDENCE_MAX_CHARS,
    WORK_ITEM_EVIDENCE_MAX_ITEMS,
    WORK_ITEM_EVENT_SCAN_MAX,
    WORK_ITEM_PROGRESS_MAX_CHARS,
    _bounded_context_text,
)


async def _resolve_uncertain_project_file_audit(
    project: Project,
    *,
    previous_head: str,
    result: dict[str, Any],
    tool_call_id: str,
    member: ProjectMemberSnapshot,
    agent_id: uuid.UUID,
    project_run: ProjectRun | None,
    summary: str,
    metadata: dict[str, Any],
) -> bool:
    """Resolve an uncertain DB commit without erasing a later Git commit."""

    commit = str(result["commit"])
    call_id = str(tool_call_id or "")

    async def _event_exists(db) -> bool:
        query = select(ProjectEvent.id).where(
            ProjectEvent.project_id == project.id,
            ProjectEvent.event_type == "project.file.committed",
            ProjectEvent.event_metadata["commit"].astext == commit,
        )
        if call_id:
            query = query.where(ProjectEvent.event_metadata["tool_call_id"].astext == call_id)
        return (await db.execute(query.limit(1))).scalar_one_or_none() is not None

    async with async_session() as verify_db:
        if await _event_exists(verify_db):
            return True

    compensated = await reset_project_repository_head(
        project,
        previous_head,
        expected_head=commit,
    )
    if compensated:
        return False

    # HEAD advanced after this commit. Preserve later work and durably restore
    # the missing audit record instead of rewinding the repository.
    async with async_session() as recovery_db:
        attached = (
            await recovery_db.execute(
                select(Project)
                .where(Project.id == project.id, Project.tenant_id == project.tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if attached is None:
            return False
        if await _event_exists(recovery_db):
            return True
        current_head = (await repository_state(attached))["head"]
        if not await project_repository_commit_is_ancestor(attached, commit, current_head):
            return False
        settings = dict(attached.settings or {})
        settings["git"] = {**dict(settings.get("git") or {}), "head": current_head}
        attached.settings = settings
        add_event(
            recovery_db,
            attached,
            "project.file.committed",
            summary,
            actor_agent_id=agent_id,
            work_item_id=project_run.work_item_id if project_run else None,
            run_id=project_run.id if project_run else None,
            metadata={**metadata, "audit_recovered": True, "current_head": current_head},
        )
        await recovery_db.commit()
        return True

def _uuid(value: Any, field: str, *, optional: bool = False) -> uuid.UUID | None:
    if optional and (value is None or str(value).strip() == ""):
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a complete platform UUID") from exc


async def load_project_runtime_scope(
    db,
    *,
    session_id: str | uuid.UUID,
    agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
) -> tuple[Project, ProjectMemberSnapshot, ChatSession, SubagentRun]:
    """Resolve a project child from durable relational ownership, never config hints."""
    child_id = _uuid(session_id, "session_id")
    child = await db.get(ChatSession, child_id)
    run = await db.get(SubagentRun, child_id)
    parent = await db.get(ChatSession, run.parent_session_id) if run is not None else None
    if (
        child is None
        or child.source_channel != "subagent"
        or child.project_id is None
        or child.agent_id != agent_id
        or run is None
        or run.project_id is None
        or run.project_id != child.project_id
        or run.project_member_id is None
        or parent is None
        or parent.project_id != child.project_id
    ):
        raise ValueError("Project tools are only available in an authorized project Subagent runtime")

    project = await db.get(Project, child.project_id)
    member = await db.get(ProjectMemberSnapshot, run.project_member_id)
    if (
        project is None
        or member is None
        or member.project_id != project.id
        or member.tenant_id != project.tenant_id
        or member.agent_id != agent_id
        or not member.is_enabled
    ):
        raise ValueError("The project or its active runtime member snapshot is no longer available")

    # ``im_config`` remains useful as a frozen capability snapshot, but it is
    # never an authority. If its scope anchors are present, they must agree with
    # the relational child/run/member chain before the snapshot can be consumed.
    runtime_config = dict(child.im_config or {})
    if bool(runtime_config.get("membership_revoked")):
        raise ValueError("This historical project session was revoked and is permanently read-only")
    config_project_id = _uuid(runtime_config.get("project_id"), "project_id", optional=True)
    config_member_id = _uuid(runtime_config.get("project_member_id"), "project_member_id", optional=True)
    if config_project_id != project.id or config_member_id != member.id:
        raise ValueError("Project Subagent runtime snapshot does not match its durable scope")
    if run.execution_user_id != execution_user_id:
        from app.models.user import User
        from app.services.project_service import project_session_access_mode

        execution_user = await db.get(User, execution_user_id)
        if execution_user is None or await project_session_access_mode(db, execution_user, child) != "edit":
            raise ValueError(
                "Project tools are only available in an authorized project Subagent runtime "
                "for the project owner or an editor"
            )
    return project, member, child, run


async def _runtime_scope(
    session_id: str,
    agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    turn_anchor_id: uuid.UUID | None,
) -> tuple[Project, ProjectMemberSnapshot, ProjectRun | None]:
    async with async_session() as db:
        project, member, _child, _run = await load_project_runtime_scope(
            db,
            session_id=session_id,
            agent_id=agent_id,
            execution_user_id=execution_user_id,
        )
        project_run = None
        if turn_anchor_id is not None:
            anchor = await db.get(ChatMessage, turn_anchor_id)
            raw_run_id = dict(anchor.message_meta or {}).get("project_run_id") if anchor else None
            try:
                project_run_id = uuid.UUID(str(raw_run_id)) if raw_run_id else None
            except (TypeError, ValueError):
                project_run_id = None
            if project_run_id is not None:
                candidate = await db.get(ProjectRun, project_run_id)
                if candidate is not None and candidate.project_id == project.id:
                    project_run = candidate
        db.expunge(project)
        db.expunge(member)
        if project_run is not None:
            db.expunge(project_run)
        return project, member, project_run


async def execute_project_workspace_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    session_id: str,
    tool_call_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> str:
    """Route standard file-tool contracts to the shared project Git tree."""

    if tool_name not in PROJECT_STANDARD_FILE_TOOL_NAMES | PROJECT_STRUCTURED_READ_TOOL_NAMES:
        raise ValueError(f"Unknown project workspace tool: {tool_name}")
    project, member, project_run = await _runtime_scope(
        session_id,
        agent_id,
        execution_user_id,
        turn_anchor_id,
    )
    if tool_name in {"write_file", "edit_file", "move_file", "delete_file"} and project.status != "running":
        raise ValueError("Project files can be modified only while the project is running")

    if tool_name == "read_document":
        path = str(arguments.get("path") or "").strip()
        if not path:
            raise ValueError("path is required")
        from app.services.agent_tools import _READ_DOCUMENT_MAX_FILE_BYTES, _read_document
        materialized = await materialize_project_read_workspace(
            project,
            [path],
            max_bytes=_READ_DOCUMENT_MAX_FILE_BYTES,
        )
        try:
            return await _read_document(
                materialized.root,
                path,
                max_chars=min(int(arguments.get("max_chars", 8000)), 20000),
                tenant_id=None,
            )
        finally:
            materialized.cleanup()
    if tool_name == "read_image":
        image_paths = arguments.get("image_paths") or []
        if not isinstance(image_paths, list):
            raise ValueError("image_paths must be an array")
        local_paths: list[str] = []
        for image_path in image_paths:
            value = str(image_path or "").strip()
            if not value.lower().startswith(("http://", "https://", "data:")):
                local_paths.append(value)
        from app.services.tools.read_image import (
            get_effective_read_image_max_bytes,
            handle_read_image,
        )
        max_image_bytes = await get_effective_read_image_max_bytes(agent_id)
        materialized = await materialize_project_read_workspace(
            project,
            local_paths,
            max_bytes=max_image_bytes,
        )
        try:
            return await handle_read_image(
                agent_id,
                arguments,
                workspace_root=materialized.root,
            )
        finally:
            materialized.cleanup()

    if tool_name == "list_files":
        return await list_project_workspace(project, str(arguments.get("path") or ""))
    if tool_name == "read_file":
        path = str(arguments.get("path") or "").strip()
        if not path:
            raise ValueError("path is required")
        return await read_project_workspace_file(
            project,
            path,
            offset=int(arguments.get("offset", 0)),
            limit=int(arguments.get("limit", 2000)),
        )
    if tool_name == "search_files":
        pattern = str(arguments.get("pattern") or "")
        if not pattern:
            raise ValueError("pattern is required")
        return await search_project_workspace(
            project,
            pattern,
            path=str(arguments.get("path") or "."),
            file_pattern=str(arguments.get("file_pattern") or "*"),
            ignore_case=bool(arguments.get("ignore_case", False)),
        )
    if tool_name == "find_files":
        pattern = str(arguments.get("pattern") or "")
        if not pattern:
            raise ValueError("pattern is required")
        return await find_project_workspace_files(
            project,
            pattern,
            path=str(arguments.get("path") or "."),
        )

    trace_metadata = {
        "tool_name": tool_name,
        "tool_call_id": str(tool_call_id or ""),
        "session_id": session_id,
        "subagent_session_id": session_id,
        "project_run_id": str(project_run.id) if project_run else None,
        "work_item_id": str(project_run.work_item_id) if project_run and project_run.work_item_id else None,
    }
    async with async_session() as db:
        attached = (
            await db.execute(
                select(Project)
                .where(Project.id == project.id, Project.tenant_id == project.tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if attached is None or attached.status != "running":
            raise ValueError("Project files can be modified only while the project is running")
        previous_head = (await repository_state(attached))["head"]
        author_email = project_agent_git_email(agent_id)
        if tool_name == "write_file":
            path = str(arguments.get("path") or "").strip()
            content = arguments.get("content")
            if not path or not isinstance(content, str):
                raise ValueError("path and string content are required")
            result = await write_project_workspace_file(
                attached, path, content,
                author_name=member.name_snapshot, author_email=author_email,
            )
        elif tool_name == "edit_file":
            path = str(arguments.get("path") or "").strip()
            old_string = arguments.get("old_string")
            new_string = arguments.get("new_string")
            if not path or not isinstance(old_string, str) or not isinstance(new_string, str):
                raise ValueError("path, old_string, and new_string are required")
            result = await edit_project_workspace_file(
                attached, path, old_string, new_string,
                replace_all=bool(arguments.get("replace_all", False)),
                author_name=member.name_snapshot, author_email=author_email,
            )
        elif tool_name == "move_file":
            source_path = str(arguments.get("source_path") or "").strip()
            destination_path = str(arguments.get("destination_path") or "").strip()
            if not source_path or not destination_path:
                raise ValueError("source_path and destination_path are required")
            result = await move_project_workspace_path(
                attached, source_path, destination_path,
                overwrite=bool(arguments.get("overwrite", False)),
                author_name=member.name_snapshot, author_email=author_email,
            )
        else:
            path = str(arguments.get("path") or "").strip()
            if not path:
                raise ValueError("path is required")
            result = await delete_project_workspace_file(
                attached, path,
                author_name=member.name_snapshot, author_email=author_email,
            )
        event_metadata = {
            key: result[key]
            for key in ("operation", "path", "source_path", "destination_path", "commit", "replacements")
            if key in result
        }
        settings = dict(attached.settings or {})
        settings["git"] = {**dict(settings.get("git") or {}), "head": result["commit"]}
        attached.settings = settings
        display_path = result.get("path") or result.get("destination_path") or result.get("source_path")
        add_event(
            db,
            attached,
            "project.file.committed",
            f"{member.name_snapshot} 更新了项目文件：{display_path}",
            actor_agent_id=agent_id,
            work_item_id=project_run.work_item_id if project_run else None,
            run_id=project_run.id if project_run else None,
            metadata={**event_metadata, **trace_metadata},
        )
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            if not await _resolve_uncertain_project_file_audit(
                project,
                previous_head=previous_head,
                result=result,
                tool_call_id=tool_call_id,
                member=member,
                agent_id=agent_id,
                project_run=project_run,
                summary=f"{member.name_snapshot} 更新了项目文件：{display_path}",
                metadata={**event_metadata, **trace_metadata},
            ):
                raise
    return json.dumps(result, ensure_ascii=False)


async def resolve_project_sandbox_scope(
    *,
    agent_id: uuid.UUID,
    execution_user_id: uuid.UUID,
    session_id: str,
    turn_anchor_id: uuid.UUID | None,
) -> tuple[Project, ProjectMemberSnapshot, ProjectRun | None]:
    """Resolve the same authorized project scope used by every project tool."""

    project, member, project_run = await _runtime_scope(
        session_id,
        agent_id,
        execution_user_id,
        turn_anchor_id,
    )
    if project.status != "running":
        raise ValueError("Project sandbox can run only while the project is running")
    return project, member, project_run


async def finalize_project_sandbox_changes(
    project: Project,
    member: ProjectMemberSnapshot,
    project_run: ProjectRun | None,
    workspace: ProjectSandboxWorkspace,
    *,
    agent_id: uuid.UUID,
    session_id: str,
    tool_call_id: str,
    tool_name: str,
) -> dict[str, Any] | None:
    """Commit and audit public repository changes made by sandbox code."""

    async with async_session() as db:
        attached = (
            await db.execute(
                select(Project)
                .where(Project.id == project.id, Project.tenant_id == project.tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if (
            attached is None
            or attached.status != "running"
        ):
            raise ValueError("Project sandbox results can be committed only while the project is running")
        previous_head = (await repository_state(attached))["head"]
        result = await commit_project_workspace_sandbox_changes(
            attached,
            workspace,
            author_name=member.name_snapshot,
            author_email=project_agent_git_email(agent_id),
        )
        if result is None:
            return None
        settings = dict(attached.settings or {})
        settings["git"] = {**dict(settings.get("git") or {}), "head": result["commit"]}
        attached.settings = settings
        sandbox_metadata = {
            **result,
            "tool_name": tool_name,
            "tool_call_id": str(tool_call_id or ""),
            "session_id": session_id,
            "subagent_session_id": session_id,
            "project_run_id": str(project_run.id) if project_run else None,
            "work_item_id": (
                str(project_run.work_item_id)
                if project_run and project_run.work_item_id
                else None
            ),
        }
        add_event(
            db,
            attached,
            "project.file.committed",
            f"{member.name_snapshot} 更新了项目文件",
            actor_agent_id=agent_id,
            work_item_id=project_run.work_item_id if project_run else None,
            run_id=project_run.id if project_run else None,
            metadata=sandbox_metadata,
        )
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            if not await _resolve_uncertain_project_file_audit(
                project,
                previous_head=previous_head,
                result=result,
                tool_call_id=tool_call_id,
                member=member,
                agent_id=agent_id,
                project_run=project_run,
                summary=f"{member.name_snapshot} 更新了项目文件",
                metadata=sandbox_metadata,
            ):
                raise
    return result


async def _enabled_member(db, project: Project, agent_id: uuid.UUID | None) -> ProjectMemberSnapshot | None:
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
        raise ValueError("assignee/target must be an enabled project member")
    return member


async def _dependency_ids(db, project: Project, raw_ids: list[Any], *, item_id: uuid.UUID | None = None) -> list[str]:
    ids = {_uuid(value, "dependency_id") for value in raw_ids}
    if item_id is not None and item_id in ids:
        raise ValueError("A work item cannot depend on itself")
    if not ids:
        return []
    found = set(
        (
            await db.execute(
                select(ProjectWorkItem.id).where(
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.id.in_(ids),
                )
            )
        ).scalars()
    )
    if found != ids:
        raise ValueError("Every dependency must belong to the current project")
    return [str(value) for value in ids]


async def _project_context(project: Project) -> str:
    git = await repository_state(project, limit=1)
    settings = dict(project.settings or {})
    async with async_session() as db:
        members = (
            (
                await db.execute(
                    select(ProjectMemberSnapshot)
                    .where(ProjectMemberSnapshot.project_id == project.id)
                    .order_by(
                        ProjectMemberSnapshot.is_leader.desc(),
                        ProjectMemberSnapshot.created_at,
                    )
                )
            )
            .scalars()
            .all()
        )
    payload = {
        "id": str(project.id),
        "name": project.name,
        "status": project.status,
        "goal": project.goal,
        "description": project.description,
        "success_criteria": list(project.success_criteria or []),
        "current_signal": settings.get("current_signal"),
        "next_action": settings.get("next_action"),
        "git_head": git["head"],
        "members": [
            {
                "member_id": str(member.id),
                "agent_id": str(member.agent_id),
                "name": _bounded_context_text(member.name_snapshot, PROJECT_MEMBER_NAME_MAX_CHARS),
                "project_role": "owner" if member.is_leader else "participant",
                "professional_role": _bounded_context_text(
                    member.role_snapshot,
                    PROJECT_MEMBER_ROLE_MAX_CHARS,
                ),
                "enabled": member.is_enabled,
            }
            for member in members
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


async def _list_work_items(project: Project, agent_id: uuid.UUID, mine_only: bool) -> str:
    async with async_session() as db:
        statement = select(ProjectWorkItem).where(ProjectWorkItem.project_id == project.id)
        if mine_only:
            statement = statement.where(ProjectWorkItem.assignee_agent_id == agent_id)
        items = (await db.execute(statement.order_by(ProjectWorkItem.created_at))).scalars().all()

        assignee_ids = {item.assignee_agent_id for item in items if item.assignee_agent_id is not None}
        members = (
            (
                await db.execute(
                    select(ProjectMemberSnapshot).where(
                        ProjectMemberSnapshot.project_id == project.id,
                        ProjectMemberSnapshot.tenant_id == project.tenant_id,
                        ProjectMemberSnapshot.agent_id.in_(assignee_ids),
                    )
                )
            )
            .scalars()
            .all()
            if assignee_ids
            else []
        )
        member_by_agent_id = {member.agent_id: member for member in members}

        item_ids = [item.id for item in items]
        events = (
            (
                await db.execute(
                    select(ProjectEvent)
                    .where(
                        ProjectEvent.project_id == project.id,
                        ProjectEvent.tenant_id == project.tenant_id,
                        ProjectEvent.work_item_id.in_(item_ids),
                        ProjectEvent.event_type == "work_item.updated",
                    )
                    .order_by(ProjectEvent.created_at.desc())
                    .limit(WORK_ITEM_EVENT_SCAN_MAX)
                )
            )
            .scalars()
            .all()
            if item_ids
            else []
        )
        progress_by_item_id: dict[uuid.UUID, str] = {}
        evidence_by_item_id: dict[uuid.UUID, list[str]] = {}
        for event in events:
            if event.work_item_id is None:
                continue
            metadata = dict(event.event_metadata or {})
            progress = _bounded_context_text(metadata.get("progress_note"), WORK_ITEM_PROGRESS_MAX_CHARS)
            if progress and event.work_item_id not in progress_by_item_id:
                progress_by_item_id[event.work_item_id] = progress

            evidence = evidence_by_item_id.setdefault(event.work_item_id, [])
            raw_evidence = metadata.get("evidence") or []
            if not isinstance(raw_evidence, list):
                raw_evidence = [raw_evidence]
            for raw_value in raw_evidence:
                value = _bounded_context_text(raw_value, WORK_ITEM_EVIDENCE_MAX_CHARS)
                if value and value not in evidence and len(evidence) < WORK_ITEM_EVIDENCE_MAX_ITEMS:
                    evidence.append(value)

        payload = [
            {
                "id": str(item.id),
                "title": item.title,
                "description": item.description,
                "status": item.status,
                "priority": item.priority,
                "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
                "assignee_name": (
                    _bounded_context_text(
                        member_by_agent_id[item.assignee_agent_id].name_snapshot,
                        PROJECT_MEMBER_NAME_MAX_CHARS,
                    )
                    if item.assignee_agent_id in member_by_agent_id
                    else None
                ),
                "professional_role": (
                    _bounded_context_text(
                        member_by_agent_id[item.assignee_agent_id].role_snapshot,
                        PROJECT_MEMBER_ROLE_MAX_CHARS,
                    )
                    if item.assignee_agent_id in member_by_agent_id
                    else None
                ),
                "dependency_ids": list(item.dependency_ids or []),
                "acceptance_criteria": list(item.acceptance_criteria or []),
                "progress": progress_by_item_id.get(item.id),
                "evidence": evidence_by_item_id.get(item.id, []),
            }
            for item in items
        ]
    return json.dumps(payload, ensure_ascii=False)
