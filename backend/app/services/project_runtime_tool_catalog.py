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
