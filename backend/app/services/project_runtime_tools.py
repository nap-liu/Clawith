"""Project-scoped tools exposed only inside a frozen project child runtime."""

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

PARTICIPANT_PROJECT_TOOLS = frozenset(
    {
        "project_get_context",
        "project_list_work_items",
        "project_update_work_item",
        "project_message_agent",
    }
)
LEADER_ONLY_PROJECT_TOOLS = frozenset(
    {
        "project_update_plan",
        "project_create_work_item",
        "project_set_member_enabled",
        "project_set_capability_enabled",
        "project_create_milestone",
        "project_set_status",
        "project_restore_commit",
    }
)
PROJECT_RUNTIME_TOOL_NAMES = PARTICIPANT_PROJECT_TOOLS | LEADER_ONLY_PROJECT_TOOLS
PROJECT_STANDARD_FILE_TOOL_NAMES = frozenset(
    {
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "search_files",
        "find_files",
        "move_file",
        "delete_file",
    }
)
PROJECT_STRUCTURED_READ_TOOL_NAMES = frozenset({"read_document", "read_image"})
PROJECT_SANDBOX_TOOL_NAMES = frozenset({"execute_code"})
PROJECT_WORKSPACE_ROUTED_TOOL_NAMES = (
    PROJECT_STANDARD_FILE_TOOL_NAMES
    | PROJECT_STRUCTURED_READ_TOOL_NAMES
    | PROJECT_SANDBOX_TOOL_NAMES
)
PROJECT_FILE_WORKSPACES = frozenset({"agent", "project"})

WORK_ITEM_STATUSES = {"backlog", "todo", "in_progress", "review", "blocked", "done"}
WORK_ITEM_PRIORITIES = {"low", "medium", "high", "urgent"}

# Project context is injected into an LLM tool result, so every newly exposed
# human-authored field must have a deterministic upper bound.  These limits are
# intentionally smaller than storage limits: the complete records remain
# available through the project detail APIs and event timeline.
PROJECT_MEMBER_NAME_MAX_CHARS = 100
PROJECT_MEMBER_ROLE_MAX_CHARS = 500
WORK_ITEM_PROGRESS_MAX_CHARS = 600
WORK_ITEM_EVIDENCE_MAX_ITEMS = 6
WORK_ITEM_EVIDENCE_MAX_CHARS = 300
WORK_ITEM_EVENT_SCAN_MAX = 600


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


def add_project_workspace_parameter(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extend standard file schemas only while they run inside a project.

    The original tool name and all existing arguments stay unchanged. Requiring
    one explicit workspace prevents a project Agent from accidentally crossing
    between its private workspace and the shared project repository.
    """

    projected: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("function", {}).get("name") not in PROJECT_WORKSPACE_ROUTED_TOOL_NAMES:
            projected.append(tool)
            continue
        item = copy.deepcopy(tool)
        function = item["function"]
        parameters = function.setdefault("parameters", {"type": "object", "properties": {}})
        properties = parameters.setdefault("properties", {})
        properties["workspace"] = {
            "type": "string",
            "enum": sorted(PROJECT_FILE_WORKSPACES),
            "description": (
                "Required in a project: use 'agent' for the Digital Employee's private workspace, "
                "or 'project' for the shared project repository."
            ),
        }
        required = list(parameters.get("required") or [])
        if "workspace" not in required:
            required.append("workspace")
        parameters["required"] = required
        function["description"] = (
            f"{str(function.get('description') or '').rstrip()} "
            "In a project, explicitly select the target with workspace."
        ).strip()
        projected.append(item)
    return projected


def _bounded_context_text(value: Any, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1].rstrip()}…"


def _milestone_operation_key(
    project: Project,
    member: ProjectMemberSnapshot,
    project_run: ProjectRun | None,
    session_id: str,
    message: str,
    paths: list[str] | None,
    related_work_item_ids: set[uuid.UUID],
    related_run_ids: set[uuid.UUID],
) -> str:
    """Build a semantic retry key independent of an LLM-generated tool-call id."""

    payload = {
        "version": 1,
        "project_id": str(project.id),
        "agent_id": str(member.agent_id),
        "project_run_id": str(project_run.id) if project_run else None,
        "session_id": session_id,
        "message": message,
        "paths": sorted(paths or []),
        "related_work_item_ids": sorted(str(value) for value in related_work_item_ids),
        "related_run_ids": sorted(str(value) for value in related_run_ids),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"project-milestone-v1-{digest}"


async def _milestone_operation_event(
    db,
    project: Project,
    operation_key: str,
) -> ProjectEvent | None:
    candidates = (
        (
            await db.execute(
                select(ProjectEvent).where(
                    ProjectEvent.project_id == project.id,
                    ProjectEvent.tenant_id == project.tenant_id,
                    ProjectEvent.event_type.in_(["git.milestone.prepared", "git.milestone.created"]),
                )
            )
        )
        .scalars()
        .all()
    )
    return next(
        (
            event
            for event in candidates
            if dict(event.event_metadata or {}).get("milestone_operation_key") == operation_key
        ),
        None,
    )


def _schema(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


PROJECT_TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "project_get_context": _schema(
        "project_get_context",
        "Read the project's goal, current plan, status, saved version, and member responsibilities.",
        {},
    ),
    "project_list_work_items": _schema(
        "project_list_work_items",
        "List project work items with assignee responsibility, latest progress, and evidence. "
        "Optionally return only work assigned to this member.",
        {"mine_only": {"type": "boolean", "default": False}},
    ),
    "project_create_work_item": _schema(
        "project_create_work_item",
        "Create one project work item. This records the work but does not start it. Available to the project lead.",
        {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "assignee_agent_id": {
                "type": "string",
                "description": "Identifier of an active project member responsible for the work.",
            },
            "priority": {"type": "string", "enum": sorted(WORK_ITEM_PRIORITIES)},
            "dependency_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Work item identifiers that must be completed first.",
            },
        },
        ["title", "description", "acceptance_criteria"],
    ),
    "project_update_work_item": _schema(
        "project_update_work_item",
        "Update one project work item. Members may report status, progress, and evidence for their own assigned "
        "work; the project lead may also change its content, priority, dependencies, or assignee.",
        {
            "work_item_id": {"type": "string", "description": "Exact work item identifier."},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
            "assignee_agent_id": {
                "type": ["string", "null"],
                "description": "Active project member identifier, or null to leave the work unassigned.",
            },
            "status": {"type": "string", "enum": sorted(WORK_ITEM_STATUSES)},
            "priority": {"type": "string", "enum": sorted(WORK_ITEM_PRIORITIES)},
            "dependency_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Work item identifiers that must be completed first.",
            },
            "progress_note": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        ["work_item_id"],
    ),
    "project_message_agent": _schema(
        "project_message_agent",
        "Send one active project member a review request or assigned task. Include the relevant context, requested "
        "work, expected result, and related work item when one exists.",
        {
            "agent_id": {"type": "string", "description": "Identifier of the active project member to contact."},
            "work_item_id": {
                "type": "string",
                "description": (
                    "Exact related work item identifier. Required for delegated work unless the current work is "
                    "already associated with that item."
                ),
            },
            "title": {
                "type": "string",
                "maxLength": 120,
                "description": "Concise title for the review request or assigned task.",
            },
            "message": {
                "type": "string",
                "description": "Context, requested work, and expected result.",
            },
            "mode": {
                "type": "string",
                "enum": ["task_delegate", "consult"],
                "description": (
                    "Choose task_delegate for assigned work and consult for a review or decision."
                ),
            },
            "expected_output": {
                "type": "string",
                "description": "Expected review, decision, analysis, or deliverable.",
            },
            "new_conversation": {
                "type": "boolean",
                "description": "Start a separate conversation instead of continuing the existing one.",
            },
        },
        ["agent_id", "title", "message", "mode", "expected_output"],
    ),
    "project_update_plan": _schema(
        "project_update_plan",
        "Update the project goal, success criteria, current signal, or next action. Available to the project lead.",
        {
            "goal": {"type": "string"},
            "success_criteria": {"type": "array", "items": {"type": "string"}},
            "current_signal": {"type": "string"},
            "next_action": {"type": "string"},
        },
    ),
    "project_set_member_enabled": _schema(
        "project_set_member_enabled",
        "Activate or deactivate one project member who is not the project lead. Available to the project lead.",
        {
            "agent_id": {"type": "string", "description": "Exact project member identifier."},
            "is_enabled": {"type": "boolean"},
        },
        ["agent_id", "is_enabled"],
    ),
    "project_set_capability_enabled": _schema(
        "project_set_capability_enabled",
        "Enable or disable a capability already available to the project. Available to the project lead.",
        {
            "binding_id": {
                "type": "string",
                "description": "Exact identifier of the project capability entry.",
            },
            "is_enabled": {"type": "boolean"},
        },
        ["binding_id", "is_enabled"],
    ),
    "project_create_milestone": _schema(
        "project_create_milestone",
        "Create a named delivery checkpoint, optionally limited to selected project files. Available to the "
        "project lead.",
        {
            "message": {"type": "string"},
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Project-relative paths to include in the checkpoint.",
            },
            "related_work_item_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Work item identifiers associated with this checkpoint.",
            },
            "related_run_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Work progress identifiers associated with this checkpoint. When omitted, "
                "successful progress for the selected work items is included.",
            },
        },
        ["message"],
    ),
    "project_set_status": _schema(
        "project_set_status",
        "Change the project to waiting, paused, completed, or failed. Available to the project lead.",
        {
            "status": {"type": "string", "enum": ["waiting", "paused", "completed", "failed"]},
            "reason": {"type": "string", "description": "Reason for the status change."},
        },
        ["status"],
    ),
    "project_restore_commit": _schema(
        "project_restore_commit",
        "Restore project files from an earlier saved version while keeping later versions available. Available to "
        "the project lead when project policy permits; otherwise human approval is required.",
        {
            "commit": {"type": "string", "description": "Exact saved version identifier to restore."},
            "message": {"type": "string", "description": "Reason for restoring this version."},
        },
        ["commit"],
    ),
}


def effective_project_tool_names(project: Project, member: ProjectMemberSnapshot) -> set[str]:
    """Intersect role baseline, project policy, and member-local projection."""
    if not member.is_enabled:
        return set()
    effective = set(PARTICIPANT_PROJECT_TOOLS)
    role = "leader" if member.is_leader else "participant"
    if member.is_leader:
        effective.update(LEADER_ONLY_PROJECT_TOOLS)
    policies = dict((project.settings or {}).get("policies") or {})
    policy = dict(policies.get("project_tools") or {})
    disabled = set(policy.get("disabled") or []) | set(policy.get(f"{role}_disabled") or [])
    allowed = policy.get(f"{role}_allowed")
    if isinstance(allowed, list):
        effective.intersection_update(str(name) for name in allowed)
    member_config = dict(member.config_snapshot or {})
    effective.difference_update(str(name) for name in member_config.get("disabled_project_tools", []))
    member_allowed = member_config.get("enabled_project_tools")
    if isinstance(member_allowed, list):
        effective.intersection_update(str(name) for name in member_allowed)
    effective.difference_update(disabled)
    return effective & PROJECT_RUNTIME_TOOL_NAMES


def project_runtime_tool_schemas(
    project: Project,
    member: ProjectMemberSnapshot,
) -> list[dict[str, Any]]:
    names = effective_project_tool_names(project, member)
    return [PROJECT_TOOL_REGISTRY[name] for name in PROJECT_TOOL_REGISTRY if name in names]


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
