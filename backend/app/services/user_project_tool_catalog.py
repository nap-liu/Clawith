"""Schemas and registration metadata for human project tools."""

from typing import Any


USER_PROJECT_TOOL_NAMES = frozenset(
    {
        "user_project_search",
        "user_project_get",
        "user_project_work_item_list",
        "user_project_work_item_get",
        "user_project_run_list",
        "user_project_member_list",
        "user_project_milestone_list",
        "user_project_file_list",
        "user_project_file_read",
        "user_project_git_get",
        "user_project_git_diff",
        "user_project_message_list",
        "user_project_work_item_create",
        "user_project_work_item_update",
        "user_project_run_start",
        "user_project_message_send",
        "user_project_milestone_create",
        "user_project_file_write",
        "user_project_status_update",
    }
)

USER_PROJECT_MUTATION_TOOL_NAMES = frozenset(
    {
        "user_project_work_item_create",
        "user_project_work_item_update",
        "user_project_run_start",
        "user_project_message_send",
        "user_project_milestone_create",
        "user_project_file_write",
        "user_project_status_update",
    }
)

_PROJECT_STATUSES = [
    "planning",
    "initializing",
    "running",
    "waiting",
    "paused",
    "completed",
    "archived",
    "failed",
]
_WORK_ITEM_STATUSES = ["backlog", "todo", "in_progress", "review", "blocked", "done"]
_RUN_STATUSES = ["queued", "running", "waiting", "succeeded", "failed", "cancelled"]
_PRIVATE_CONNECTION_FILENAMES = frozenset(
    {
        ".mcp.json",
        ".mcp.toml",
        ".mcp.yaml",
        ".mcp.yml",
        ".gitmodules",
        "connection.json",
        "connection.toml",
        "connection.yaml",
        "connection.yml",
        "connections.json",
        "connections.toml",
        "connections.yaml",
        "connections.yml",
        "mcp.json",
        "mcp-servers.json",
        "mcp_servers.json",
        "mcp.toml",
        "mcp.yaml",
        "mcp.yml",
    }
)


def _parameters(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _seed(
    name: str,
    display_name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "display_name": display_name,
        "description": description,
        "category": "project_management",
        "icon": "📁",
        "is_default": False,
        "parameters_schema": _parameters(properties, required),
        "config": {},
        "config_schema": {"fields": []},
    }


_PROJECT_ID = {"type": "string", "description": "Exact project identifier returned by a project result."}
_LIMIT_50 = {
    "type": "integer",
    "minimum": 1,
    "maximum": 50,
    "default": 20,
    "description": "Maximum number of results to return.",
}
_OFFSET = {
    "type": "integer",
    "minimum": 0,
    "maximum": 5000,
    "default": 0,
    "description": "Starting position. Use next_offset from the previous result to continue.",
}

USER_PROJECT_TOOL_SEEDS = [
    _seed(
        "user_project_search",
        "Search Projects",
        "Find projects by name, goal, status, or ownership scope.",
        {
            "query": {"type": "string", "maxLength": 200},
            "scope": {
                "type": "string",
                "enum": ["all", "mine", "shared", "running", "archived"],
                "default": "all",
            },
            "status": {"type": "string", "enum": _PROJECT_STATUSES},
            "offset": _OFFSET,
            "limit": _LIMIT_50,
        },
    ),
    _seed(
        "user_project_get",
        "Get Project Details",
        "Read a project's goal, progress, status, and delivery summary.",
        {"project_id": _PROJECT_ID},
        ["project_id"],
    ),
    _seed(
        "user_project_work_item_list",
        "List Project Work Items",
        "List project work items with status, priority, assignment, and due dates.",
        {
            "project_id": _PROJECT_ID,
            "status": {"type": "string", "enum": _WORK_ITEM_STATUSES},
            "assignee_agent_id": {"type": "string", "description": "Exact project member identifier."},
            "offset": _OFFSET,
            "limit": _LIMIT_50,
        },
        ["project_id"],
    ),
    _seed(
        "user_project_work_item_get",
        "Get Project Work Item",
        "Read one work item's description, acceptance criteria, dependencies, and status.",
        {
            "project_id": _PROJECT_ID,
            "work_item_id": {"type": "string", "description": "Exact work item identifier."},
        },
        ["project_id", "work_item_id"],
    ),
    _seed(
        "user_project_run_list",
        "List Project Runs",
        "List project work progress with status, assigned member, how the work started, and timing.",
        {
            "project_id": _PROJECT_ID,
            "run_id": {"type": "string", "description": "Exact work progress identifier."},
            "work_item_id": {"type": "string", "description": "Exact related work item identifier."},
            "agent_id": {"type": "string", "description": "Exact project member identifier."},
            "status": {"type": "string", "enum": _RUN_STATUSES},
            "offset": _OFFSET,
            "limit": _LIMIT_50,
        },
        ["project_id"],
    ),
    _seed(
        "user_project_member_list",
        "List Project Members",
        "List project members, responsibilities, availability, and the designated project lead.",
        {
            "project_id": _PROJECT_ID,
            "include_disabled": {"type": "boolean", "default": False},
            "offset": _OFFSET,
            "limit": _LIMIT_50,
        },
        ["project_id"],
    ),
    _seed(
        "user_project_milestone_list",
        "List Project Milestones",
        "List project delivery milestones and their saved version identifiers.",
        {"project_id": _PROJECT_ID, "offset": _OFFSET, "limit": _LIMIT_50},
        ["project_id"],
    ),
    _seed(
        "user_project_file_list",
        "List Project Files",
        "List files currently saved in a project workspace.",
        {
            "project_id": _PROJECT_ID,
            "path_prefix": {
                "type": "string",
                "maxLength": 1024,
                "description": "Optional project-relative folder or path prefix.",
            },
            "offset": _OFFSET,
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 100},
        },
        ["project_id"],
    ),
    _seed(
        "user_project_file_read",
        "Read Project File",
        "Read text from one saved project file, up to the requested character limit.",
        {
            "project_id": _PROJECT_ID,
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1024,
                "description": "Exact path returned by the project file list.",
            },
            "max_chars": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20000,
                "default": 12000,
                "description": "Maximum number of characters to return.",
            },
        },
        ["project_id", "path"],
    ),
    _seed(
        "user_project_git_get",
        "Get Project Version History",
        "Read the current saved version, file count, and recent version history.",
        {"project_id": _PROJECT_ID, "offset": _OFFSET, "limit": _LIMIT_50},
        ["project_id"],
    ),
    _seed(
        "user_project_git_diff",
        "Compare Project Versions",
        "Read file change statistics for one saved version, or detailed text changes for one file.",
        {
            "project_id": _PROJECT_ID,
            "commit": {
                "type": "string",
                "minLength": 7,
                "maxLength": 64,
                "description": "Exact saved version identifier to inspect.",
            },
            "parent": {
                "type": "string",
                "minLength": 7,
                "maxLength": 64,
                "description": "Optional earlier saved version to compare against.",
            },
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "description": "Optional exact project-relative file path for detailed text changes.",
            },
            "max_patch_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 65536,
                "default": 32768,
            },
        },
        ["project_id", "commit"],
    ),
    _seed(
        "user_project_message_list",
        "List Project Messages",
        "Read recent messages from the project's shared conversation.",
        {
            "project_id": _PROJECT_ID,
            "before_message_id": {
                "type": "string",
                "description": "Use next_before_message_id from the previous result to read older messages.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 20},
            "max_chars_per_message": {
                "type": "integer",
                "minimum": 100,
                "maximum": 3000,
                "default": 2000,
            },
        },
        ["project_id"],
    ),
    _seed(
        "user_project_work_item_create",
        "Create Project Work Item",
        "Create one project work item with delivery criteria and assignment. This records the work but does not "
        "start it.",
        {
            "project_id": _PROJECT_ID,
            "title": {"type": "string", "minLength": 1, "maxLength": 500},
            "description": {"type": "string", "maxLength": 20000},
            "status": {"type": "string", "enum": _WORK_ITEM_STATUSES, "default": "backlog"},
            "priority": {
                "type": "string",
                "enum": ["low", "medium", "high", "urgent"],
                "default": "medium",
            },
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                "maxItems": 30,
            },
            "assignee_agent_id": {
                "type": "string",
                "description": "Identifier of an active project member responsible for the work.",
            },
            "parent_id": {"type": "string", "description": "Optional parent work item identifier."},
            "dependency_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 100,
                "description": "Work item identifiers that must be completed first.",
            },
            "due_at": {"type": ["string", "null"], "format": "date-time", "description": "ISO 8601 due time. Values without an offset use the effective Agent timezone."},
        },
        ["project_id", "title"],
    ),
    _seed(
        "user_project_work_item_update",
        "Update Project Work Item",
        "Update selected fields on one project work item.",
        {
            "project_id": _PROJECT_ID,
            "work_item_id": {"type": "string", "description": "Exact work item identifier."},
            "title": {"type": "string", "minLength": 1, "maxLength": 500},
            "description": {"type": "string", "maxLength": 20000},
            "status": {"type": "string", "enum": _WORK_ITEM_STATUSES},
            "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                "maxItems": 30,
            },
            "assignee_agent_id": {
                "type": ["string", "null"],
                "description": "Active project member identifier, or null to leave the work unassigned.",
            },
            "dependency_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 100,
                "description": "Work item identifiers that must be completed first.",
            },
            "due_at": {"type": ["string", "null"], "format": "date-time", "description": "ISO 8601 due time. Values without an offset use the effective Agent timezone."},
        },
        ["project_id", "work_item_id"],
    ),
    _seed(
        "user_project_run_start",
        "Start Project Run",
        "Start work in a running project using either a work item or a clear instruction. An active project member "
        "must be assigned to perform the work.",
        {
            "project_id": _PROJECT_ID,
            "work_item_id": {"type": "string", "description": "Exact work item identifier to start."},
            "agent_id": {
                "type": "string",
                "description": (
                    "Active project member to perform the work. Defaults to the work item assignee or project lead."
                ),
            },
            "title": {"type": "string", "maxLength": 120},
            "instruction": {"type": "string", "maxLength": 10000},
        },
        ["project_id"],
    ),
    _seed(
        "user_project_message_send",
        "Send Project Group Message",
        "Send a message to the project's shared conversation for the project lead to act on. The project must be "
        "running.",
        {
            "project_id": _PROJECT_ID,
            "content": {"type": "string", "minLength": 1, "maxLength": 10000},
            "work_item_id": {"type": "string", "description": "Optional related work item identifier."},
        },
        ["project_id", "content"],
    ),
    _seed(
        "user_project_milestone_create",
        "Create Project Milestone",
        "Create one named delivery checkpoint from selected project files.",
        {
            "project_id": _PROJECT_ID,
            "message": {"type": "string", "minLength": 1, "maxLength": 500},
            "paths": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 1024},
                "minItems": 1,
                "maxItems": 100,
                "description": "Project-relative file paths to include in the checkpoint.",
            },
        },
        ["project_id", "message", "paths"],
    ),
    _seed(
        "user_project_file_write",
        "Write Project Text File",
        "Save one project text file as a new project version.",
        {
            "project_id": _PROJECT_ID,
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1024,
                "description": "Writable project-relative file path.",
            },
            "content": {"type": "string", "maxLength": 50000},
        },
        ["project_id", "path", "content"],
    ),
    _seed(
        "user_project_status_update",
        "Update Project Status",
        "Pause or resume project work. The project must already be running or paused, and only the project owner "
        "can change it.",
        {
            "project_id": _PROJECT_ID,
            "status": {"type": "string", "enum": ["running", "paused"]},
        },
        ["project_id", "status"],
    ),
]

USER_PROJECT_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": seed["name"],
            "description": seed["description"],
            "parameters": seed["parameters_schema"],
        },
    }
    for seed in USER_PROJECT_TOOL_SEEDS
]
