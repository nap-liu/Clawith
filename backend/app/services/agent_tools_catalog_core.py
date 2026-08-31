"""Core workspace, scheduling, and delivery tool schemas."""

from app.services.media_tool_contract import SEND_MEDIA_FUNCTION_TOOL


AGENT_TOOL_CORE = [
    {
        "type": "function",
        "function": {
            "name": "run_background_resource",
            "description": (
                "Manually queue one task, trigger, or schedule for immediate testing. "
                "The run uses the current conversation user's permissions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_type": {
                        "type": "string",
                        "enum": ["trigger", "task", "schedule"],
                    },
                    "resource": {
                        "type": "string",
                        "description": "Exact UUID, or an exact unique title/name.",
                    },
                },
                "required": ["resource_type", "resource"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_execution_user",
            "description": (
                "Set the user permissions used by future runs of a background resource. "
                "Runs that are already active or queued are not affected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_type": {
                        "type": "string",
                        "enum": ["trigger", "task", "schedule"],
                    },
                    "resource_id": {
                        "type": "string",
                        "description": "Exact UUID of the target background resource.",
                    },
                    "execution_user_id": {
                        "type": "string",
                        "description": (
                            "Canonical user_id for future runs; the target user must be able "
                            "to access the current Agent."
                        ),
                    },
                    "expected_execution_user_id": {
                        "type": ["string", "null"],
                        "description": ("Execution user ID read before this change; pass null when it is unset."),
                    },
                    "reason": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                        "description": "Human-approved reason stored in the audit trail.",
                    },
                },
                "required": [
                    "resource_type",
                    "resource_id",
                    "execution_user_id",
                    "expected_execution_user_id",
                    "reason",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders in a directory within my workspace. Copy each displayed path exactly when calling another file tool. Use this before writing new workspace documents so you can inspect the current folder structure, reuse existing topical subfolders when appropriate, and avoid dumping files directly into the workspace root unless there is a clear reason. Can also list enterprise_info/ for shared company information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list, defaults to root (empty string). e.g.: '', 'skills', 'workspace', 'enterprise_info', 'enterprise_info/knowledge_base'",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the requested workspace file as UTF-8 text. The path is matched exactly and is never corrected to a similar filename, so copy the path from the attachment context or list_files output. This tool accepts any file type but does not parse document formats; binary content may be unreadable as text. Can read soul.md for personality, memory/memory.md for memory, skills/ for skill files, and enterprise_info/ for shared company info. Focus is not stored in files; use list_focus_items and upsert_focus_item for Focus. Use offset and limit for ordinary text pagination, but note that line limits do not bound characters when HTML, JSON, or generated data is stored on one long line. For large or data-heavy files, use execute_code_aio to inspect and process the original path directly, write the result to a file, and print only a bounded summary, validation result, and output path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path, e.g.: soul.md, memory/memory.md, skills/xxx.md, enterprise_info/company_profile.md",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Starting line number (0-indexed, default 0). Use with limit for pagination.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read (default 2000). Use with offset for pagination.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_focus_items",
            "description": "List your structured Focus items. Focus is your current working state and is stored in the system database, not in focus.md.",
            "parameters": {
                "type": "object",
                "properties": {
                    "include_completed": {
                        "type": "boolean",
                        "description": "Whether to include completed Focus items. Default true.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_focus_item",
            "description": "Create or update one Focus item in structured storage. Use this whenever you start tracking an active task, reminder, delegated wait, or system concern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Stable short identifier, snake_case preferred. If omitted, the system derives one from description.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title (Focus名称). Use this for a quick summary of the focus. Keep it brief. New focus items should have both a title and a description.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Clear human-readable description of what is being tracked.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["normal", "system"],
                        "description": "Use normal for user/business work, system for platform-maintained focus such as heartbeat/OKR automation.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Optional origin label, e.g. user, trigger, a2a, okr.",
                    },
                },
                "required": ["description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_focus_item",
            "description": "Mark a Focus item completed. Use this after the tracked task/reminder/wait has been handled.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Focus item identifier to complete.",
                    }
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or fully overwrite a file in the workspace. Use this when writing a new file or replacing the entire content. For targeted edits to an existing file (change one section without rewriting everything), prefer edit_file instead. Before creating a new document under workspace/, first inspect the relevant directories with list_files, prefer an existing topical subfolder (for example workspace/reports/, workspace/knowledge_base/, workspace/research/) over the workspace root, and create a new subfolder when the content belongs to a new category. Avoid placing standalone document files directly in workspace/ root unless the user explicitly wants that. Can update memory/memory.md, task_history.md, create documents in workspace/, create skills in skills/. When meaningful work produces reusable results, create the current Daily Memory at memory/<YYYY-MM-DD>/memory.md if it does not exist; read and edit the existing file when it does. Record outcomes rather than transcripts and avoid duplicate entries. Focus is managed with Focus tools, not files. enterprise_info/ is shared company context and is read-only for agents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path, e.g.: memory/memory.md, workspace/reports/report.md, workspace/knowledge_base/notes.md, skills/data_analysis.md. Prefer a meaningful subfolder instead of writing loose files into workspace/ root.",
                    },
                    "content": {
                        "type": "string",
                        "description": "File content to write",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file from the workspace. Cannot delete soul.md, tasks.json, or shared enterprise_info/ files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to delete",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Move or rename a file or folder within the workspace. Use this for reorganizing workspace files, moving generated documents into subfolders, or renaming files (prefer it over shell commands like mv). Cannot move protected files or shared enterprise_info/ files. If destination_path is an existing folder or ends with '/', the original filename is preserved inside that folder. By default this will not overwrite an existing destination.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_path": {
                        "type": "string",
                        "description": "Current file or folder path, e.g.: workspace/report.md or workspace/presentations/deck.pptx",
                    },
                    "destination_path": {
                        "type": "string",
                        "description": "Destination file/folder path, e.g.: workspace/archive/report.md or workspace/presentations/PPT/",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Replace the destination if it already exists. Default false.",
                    },
                },
                "required": ["source_path", "destination_path"],
            },
        },
    },
    # --- Enhanced file management tools ---
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Surgically replace a specific string inside an existing file without rewriting the whole content. Prefer this over write_file when you only need to change one or more sections — it avoids accidentally overwriting content outside the edit target and is safer in multi-agent scenarios. When meaningful work produces reusable results, maintain the existing current Daily Memory at memory/<YYYY-MM-DD>/memory.md after reading it; record outcomes rather than transcripts and avoid duplicate entries. enterprise_info/ is shared company context and is read-only for agents. The old_string must match exactly (including all whitespace and newlines).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to edit, e.g.: memory/memory.md, skills/my-skill/SKILL.md",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact text to find and replace. Must match exactly including whitespace and newlines.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace all occurrences if true (default: false). Set to true when you want to replace every match.",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for content patterns across files using regex. Returns matching lines with file paths and line numbers. Useful for finding code, configurations, or text across the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for, e.g.: 'API_KEY', 'def\\\\s+\\\\w+', '@app\\\\.(get|post)'",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search in (default: root). e.g.: 'skills', 'workspace', 'memory'",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "File pattern to match (default: all files). e.g.: '*.md', '*.py', '*.json'",
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Case-insensitive search (default: false)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find files matching glob patterns. Returns file paths with sizes and modification info. Useful for discovering files in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern to match files, e.g.: '**/*.md' (all markdown files), 'skills/*.md' (skill files), 'workspace/**/*' (all files under workspace)",
                    },
                    "path": {
                        "type": "string",
                        "description": "Base directory for search (default: root). e.g.: 'skills', 'workspace'",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    # --- Trigger management tools (Aware engine) ---
    {
        "type": "function",
        "function": {
            "name": "set_trigger",
            "description": "Set a new trigger to wake yourself up at a specific time or condition. Use this to schedule future actions, monitor changes, or wait for messages. The trigger will fire and invoke you with the reason text as context. Every trigger is attached to a focus item; if focus_ref is omitted, the system will automatically create a focus item from the reason and attach the trigger to it. Trigger types: 'cron' (recurring schedule), 'once' (fire once at a time), 'interval' (every N minutes), 'poll' (HTTP monitoring), 'on_message' (when another agent or human replies — identify exactly one actor with from_agent_id or from_user_id), 'webhook' (receive external HTTP POST — system generates a unique URL, give it to the user so they can configure it in external services like GitHub, Grafana, etc.). For type=webhook you can also set webhook_mode to control how bursts of rapid triggers are handled — see the webhook_mode parameter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Unique name for this trigger, e.g. 'daily_briefing' or 'wait_<name>_reply'",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["cron", "once", "interval", "poll", "on_message", "webhook"],
                        "description": "Trigger type",
                    },
                    "config": {
                        "type": "object",
                        "description": 'Type-specific config. cron: {"expr": "0 9 * * *"}. once: {"at": "2026-03-10T09:00:00+08:00"}. interval: {"minutes": 30}. poll: {"url": "...", "json_path": "$.status", "fire_on": "change", "interval_min": 5}. on_message must contain exactly one canonical actor: {"from_agent_id": "<agent_id>"} or {"from_user_id": "<user_id>"}. webhook: {"secret": "optional_hmac_secret"} (system auto-generates the URL)',
                    },
                    "reason": {
                        "type": "string",
                        "description": "What you should do when this trigger fires. This will be shown to you as context when you wake up.",
                    },
                    "focus_ref": {
                        "type": "string",
                        "description": "Optional: identifier of the structured Focus item that this trigger relates to. If omitted, a Focus item is created automatically from the trigger reason.",
                    },
                    "webhook_mode": {
                        "type": "string",
                        "enum": ["legacy", "queue", "merge"],
                        "description": "Webhook processing mode (type=webhook only). Every authenticated submission accepted by the endpoint is stored byte-for-byte in this agent's webhook/ inbox; the wake context provides its event ID, millisecond timestamp, file path, size, and SHA-256, and you should read the referenced file before processing it. legacy (default) wakes from only the newest event while older inbox files remain discoverable. queue wakes once per event in FIFO order. merge wakes once for the batch captured when execution starts. Choose the mode when creating the trigger; if changing it later, briefly pause upstream submissions and do not switch during an active webhook run.",
                    },
                },
                "required": ["name", "type", "config", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_trigger",
            "description": "Update an existing trigger's configuration or reason. Use this to adjust timing, change parameters, etc. For example, change interval from 5 minutes to 30 minutes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the trigger to update",
                    },
                    "config": {
                        "type": "object",
                        "description": "New config. For webhook triggers this is a partial patch: omitted URL token, secret, webhook mode, and internal queue state remain unchanged.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "New reason text",
                    },
                    "webhook_mode": {
                        "type": "string",
                        "enum": ["legacy", "queue", "merge"],
                        "description": "For an existing webhook trigger only. Briefly pause upstream submissions and switch only when no webhook run is active and no event is pending or queued. The change is immediate and affects subsequent scheduling; it does not convert or drain in-flight work. The existing URL token, secret, and stored webhook inbox files remain unchanged. legacy uses the newest event, queue processes FIFO, and merge processes the batch captured when execution starts.",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_trigger",
            "description": "Cancel (disable) a trigger by name. Use this when a task is completed and the trigger is no longer needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the trigger to cancel",
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_triggers",
            "description": "List all your triggers, including each trigger's creator and execution user IDs.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_channel_file",
            "description": "Send a workspace file through an existing conversation or to a person. Omit all targets only for the current conversation. Use exact session_id for another existing person/group Session, or canonical user_id (and channel when needed) for direct person delivery. Never provide both session_id and user_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Workspace-relative path to the file, e.g. workspace/report.md",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "Canonical platform user_id for direct person delivery. Mutually exclusive with session_id.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Exact existing Session UUID for person or group delivery. Mutually exclusive with user_id.",
                    },
                    "channel": {
                        "type": "string",
                        "enum": ["feishu", "slack"],
                        "description": "Executable explicit file route chosen by the Agent.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Optional message to accompany the file",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    SEND_MEDIA_FUNCTION_TOOL,
]

__all__ = ["AGENT_TOOL_CORE"]

