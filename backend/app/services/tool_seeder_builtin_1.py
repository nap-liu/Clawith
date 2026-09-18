"""Builtin tool seed catalog part 1."""

from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_SEED
from app.services.agent_self_settings_tool import UPDATE_SELF_SETTINGS_TOOL_SEED
from app.schemas.tool_settings import ToolSetting, MCPServerOverrideSetting


BUILTIN_TOOLS_PART_1 = [
    REQUEST_CONFIRMATION_TOOL_SEED,
    UPDATE_SELF_SETTINGS_TOOL_SEED,
    {
        "name": "run_background_resource",
        "display_name": "Run Background Resource",
        "description": (
            "Manually queue one task, trigger, or schedule for immediate testing. "
            "The run uses the current conversation user's permissions."
        ),
        "category": "general",
        "icon": "▶️",
        "is_default": True,
        "parameters_schema": {
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
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "run_subagent",
        "display_name": "Run Subagent",
        "description": (
            "Delegate a named, focused task to a child Agent session. sync waits for the final "
            "result and reports any child messages with it; async returns the subagent_id "
            "immediately and supports live multi-round messages in both directions. In "
            "async mode, child messages and completion durably wake this exact session. "
            "The subagent_id is also the standard child session id."
        ),
        "category": "subagent",
        "icon": "🧩",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 80,
                    "description": "A concise, meaningful name for this child Agent and its session.",
                },
                "task": {"type": "string", "minLength": 1, "maxLength": 12000},
                "mode": {"type": "string", "enum": ["sync", "async"], "default": "sync"},
                "model": {
                    "type": "string",
                    "description": "Optional model UUID, model key, or unique display label from the tenant catalog.",
                },
                "temperature": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 2,
                    "description": "Optional imagination override. Omit to inherit the Digital Employee setting.",
                },
                "reasoning_effort": {
                    "type": "string",
                    "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                    "description": "Optional reasoning override. none disables thinking; omit to inherit.",
                },
                "fork": {
                    "type": "boolean",
                    "default": False,
                    "description": "Copy the current compacted, LLM-visible context into the child once.",
                },
                "soul": {
                    "type": "boolean",
                    "default": True,
                    "description": "Load this Agent's soul.md into the child runtime context.",
                },
                "memory": {
                    "type": "boolean",
                    "default": True,
                    "description": "Load Core Memory, the memory guide, and recent Daily Memory into the child runtime context.",
                },
            },
            "required": ["name", "task"],
        },
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "get_subagent_status",
        "display_name": "Get Subagent Status",
        "description": (
            "Get the current lifecycle state and latest observable result of an async "
            "Subagent created by this exact parent session."
        ),
        "category": "subagent",
        "icon": "🔎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "subagent_id": {"type": "string", "format": "uuid"},
            },
            "required": ["subagent_id"],
        },
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "send_message_to_subagent",
        "display_name": "Message Subagent",
        "description": (
            "Send one of any number of messages to a child you created. While it is "
            "running, messages interrupt its current turn at the next LLM round; a "
            "finished child resumes asynchronously with the same id and history."
        ),
        "category": "subagent",
        "icon": "➡️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "subagent_id": {"type": "string", "format": "uuid"},
                "message": {"type": "string", "minLength": 1, "maxLength": 12000},
            },
            "required": ["subagent_id", "message"],
        },
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "stop_subagent",
        "display_name": "Stop Subagent",
        "description": "Stop a queued or running child you created. Already-started external tool side effects are not rolled back.",
        "category": "subagent",
        "icon": "⏹️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "subagent_id": {"type": "string", "format": "uuid", "description": "Child session identifier."},
                "task_id": {"type": "string", "format": "uuid", "description": "Optional media task identifier. Stops only that turn; other queued media turns remain available."},
            },
            "required": ["subagent_id"],
        },
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "send_message_to_parent",
        "display_name": "Message Parent Agent",
        "description": (
            "Send one of any number of interim messages to the exact parent session. "
            "In async mode each message durably wakes the parent and can receive a reply "
            "in a later round; in sync mode messages are collected into the final tool "
            "result because the parent turn is blocked. Available only inside a "
            "Subagent session."
        ),
        "category": "subagent",
        "icon": "⬅️",
        # This protocol tool is only surfaced inside Subagent sessions, but its
        # per-Agent assignment is still managed by the standard tool panel.
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"message": {"type": "string", "minLength": 1, "maxLength": 12000}},
            "required": ["message"],
        },
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "set_execution_user",
        "display_name": "Set Background Execution User",
        "description": (
            "Set the user permissions used by future runs of a background resource. "
            "Runs that are already active or queued are not affected."
        ),
        "category": "general",
        "icon": "🔐",
        "is_default": True,
        "parameters_schema": {
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
                    "description": (
                        "Execution user ID read before this change; pass null when it is unset."
                    ),
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
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "manage_scene",
        "display_name": "场景配置",
        "description": (
            "Manage versioned scene configurations. This tool may only execute inside a "
            "direct conversation whose current human user has administrator permission. "
            "Use scenes to maintain a welcome message, ordered system prompt blocks, and "
            "unlimited quick actions for URI navigation or sending a preset message. "
            "Scene configuration is independent of the consuming channel. "
            "SAVE RULES: save is a field-level incremental update by default. Omitted "
            "fields preserve the current draft, or the published configuration when no "
            "draft exists. Passing an explicit empty string or empty array clears that "
            "field. Set force_overwrite=true to reset every omitted optional configuration "
            "field to its empty default and reset omitted enabled to true. The name may be "
            "omitted when updating an existing scene, but is required when creating one. "
            "When an array field is provided, it replaces that entire ordered array. "
            "Every save requires expected_revision: pass the revision returned by get, "
            "or 0 when the scene has never been published. Use get before updating when "
            "the current revision or contents are unknown, then publish separately after save."
        ),
        "category": "general",
        "icon": "🎬",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "include_soul": {"type": "boolean", "description": "Load Soul in this scene; defaults to true."},
                "include_memory": {"type": "boolean", "description": "Load core and daily memory in this scene; defaults to true."},
                "tools": {
                    "anyOf": [{"type": "null"}, {"type": "array", "items": ToolSetting.model_json_schema()}],
                    "description": "Complete scene tool-panel assignments; null follows the digital employee. Empty array disables optional tools.",
                },
                "mcp_server_overrides": {"type": "array", "items": MCPServerOverrideSetting.model_json_schema()},
                "operation": {
                    "type": "string",
                    "enum": ["list", "get", "save", "publish", "delete", "rollback"],
                    "description": (
                        "Operation to perform: list returns scenes; get returns the current "
                        "draft configuration when present (otherwise the published configuration) "
                        "together with the current published revision; save creates or "
                        "incrementally updates a draft unless force_overwrite=true; publish "
                        "makes the saved draft active; delete removes a scene; rollback "
                        "copies a historical revision into a new published revision."
                    ),
                },
                "scene_key": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": "^[a-z][a-z0-9_-]{0,63}$",
                    "description": (
                        "Required scene key for get, save, publish, delete, and rollback. "
                        "It starts with a lowercase letter and contains only lowercase "
                        "letters, digits, underscores, or hyphens; for example warranty."
                    ),
                },
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 100,
                    "description": (
                        "Human-readable scene name. Required when creating a scene; "
                        "omit during an incremental update to preserve the current name."
                    ),
                },
                "enabled": {
                    "type": "boolean",
                    "description": (
                        "Whether the scene is active. When omitted from an incremental save, "
                        "the current value is preserved."
                    ),
                },
                "expected_revision": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Required for save, publish, and rollback. This is the current published "
                        "revision returned by get and is used for optimistic concurrency. "
                        "Use 0 when saving or publishing a scene that has never been published."
                    ),
                },
                "welcome_message": {
                    "type": "string",
                    "maxLength": 12000,
                    "description": (
                        "Optional initial welcome message. Omit to preserve the current value "
                        "during an incremental save; use an empty string to clear it."
                    ),
                },
                "system_prompts": {
                    "type": "array",
                    "description": (
                        "Optional ordered prompt blocks. Omit to preserve the current list "
                        "during an incremental save; use an empty array to clear it."
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string", "minLength": 1, "maxLength": 120},
                            "name": {"type": "string", "minLength": 1, "maxLength": 80},
                            "content": {"type": "string", "minLength": 1, "maxLength": 30000},
                            "enabled": {
                                "type": "boolean",
                                "default": True,
                                "description": (
                                    "Whether this prompt block is active. "
                                    "Omitting it enables the supplied block."
                                ),
                            },
                        },
                        "required": ["id", "name", "content"],
                    },
                },
                "quick_actions": {
                    "type": "array",
                    "description": (
                        "Optional ordered quick actions. Omit to preserve the current list "
                        "during an incremental save; use an empty array to clear it. "
                        "When supplied, the whole list replaces the previous list. For each supplied "
                        "item, omitted menu_visible and ai_visible fields default to true, including "
                        "an item with the same id as a previously hidden item. No item count limit is imposed. "
                        "AI-visible entries are injected in list order up to a shared 24000-character "
                        "runtime context budget; overflow is omitted with an explicit marker."
                    ),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 64,
                                "pattern": "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
                            },
                            "label": {"type": "string", "minLength": 1, "maxLength": 80},
                            "type": {"type": "string", "enum": ["open_uri", "send_message"]},
                            "menu_visible": {
                                "type": "boolean",
                                "default": True,
                                "description": (
                                    "Whether this quick action is shown in the user-facing quick-action menu. "
                                    "Omitting it makes the supplied action menu-visible."
                                ),
                            },
                            "ai_visible": {
                                "type": "boolean",
                                "default": True,
                                "description": (
                                    "Whether this quick action and its detailed context are included in the "
                                    "current scene's AI context. Omitting it makes the supplied action AI-visible."
                                ),
                            },
                            "ai_context": {
                                "type": "string",
                                "maxLength": 4000,
                                "description": (
                                    "Optional AI-only detailed context, such as business meaning, applicable "
                                    "situations, prerequisites, and guidance. It is never displayed in the menu."
                                ),
                            },
                            "uri": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 2048,
                                "description": "Required for open_uri; supports relative, http(s), and miniprogram://navigate-to/.",
                            },
                            "message": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 12000,
                                "description": "Required for send_message; sent exactly as a normal user message.",
                            },
                            "style": {
                                "type": "object",
                                "description": (
                                    "Optional text style for the H5 horizontal quick-action button. "
                                    "Other quick-action surfaces keep their platform styling."
                                ),
                                "additionalProperties": False,
                                "properties": {
                                    "bold": {"type": "boolean", "default": False},
                                    "italic": {"type": "boolean", "default": False},
                                    "color": {
                                        "type": ["string", "null"],
                                        "pattern": "^#[0-9A-Fa-f]{6}$",
                                    },
                                    "font": {
                                        "type": "string",
                                        "enum": ["default", "sans", "serif", "monospace"],
                                        "default": "default",
                                    },
                                },
                            },
                        },
                        "required": ["id", "label", "type"],
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string", "enum": ["open_uri"]}
                                },
                                "required": ["type", "uri"],
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string", "enum": ["send_message"]}
                                },
                                "required": ["type", "message"],
                            },
                        ],
                    },
                },
                "force_overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Only applies to save. Defaults to false for incremental updates. "
                        "When true, omitted optional configuration fields are reset to their "
                        "empty defaults instead of preserving current values, and omitted "
                        "enabled resets to true. For an existing scene, an omitted name is "
                        "still preserved; creating a scene always requires name."
                    ),
                },
                "target_revision": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Historical revision to copy when rolling back.",
                },
            },
            "additionalProperties": False,
            "required": ["operation"],
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["list"]}
                    },
                    "required": ["operation"],
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["get", "delete"]}
                    },
                    "required": ["operation", "scene_key"],
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["save"]}
                    },
                    "required": ["operation", "scene_key", "expected_revision"],
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["publish"]}
                    },
                    "required": ["operation", "scene_key", "expected_revision"],
                },
                {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["rollback"]}
                    },
                    "required": [
                        "operation",
                        "scene_key",
                        "expected_revision",
                        "target_revision",
                    ],
                },
            ],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "list_files",
        "display_name": "List Files",
        "description": "List files and folders in a directory within the workspace. Copy each displayed path exactly when calling another file tool. Use this before writing new workspace documents so you can inspect the current folder structure, reuse existing topical subfolders when appropriate, and avoid dumping files directly into the workspace root unless there is a clear reason. Can also list enterprise_info/ for shared company information.",
        "category": "file",
        "icon": "📁",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path to list, defaults to root (empty string)"}
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "read_file",
        "display_name": "Read File",
        "description": "Read the requested workspace file as UTF-8 text. The path is matched exactly and is never corrected to a similar filename, so copy the path from the attachment context or list_files output. This tool accepts any file type but does not parse document formats; binary content may be unreadable as text. Can read soul.md, memory/memory.md, skills/, and enterprise_info/. Focus is stored in system tools, not focus.md. Use offset and limit for ordinary text pagination, but note that line limits do not bound characters when HTML, JSON, or generated data is stored on one long line. For large or data-heavy files, use execute_code_aio to inspect and process the original path directly, write the result to a file, and print only a bounded summary, validation result, and output path.",
        "category": "file",
        "icon": "📄",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, e.g.: soul.md, memory/memory.md"},
                "offset": {"type": "integer", "description": "Starting line number (0-indexed, default 0). Use with limit for pagination."},
                "limit": {"type": "integer", "description": "Maximum number of lines to read (default 2000). Use with offset for pagination."},
            },
            "required": ["path"],
        },
        "config": {"max_file_size_kb": 500},
        "config_schema": {
            "fields": [
                {"key": "max_file_size_kb", "label": "Max file size (KB)", "type": "number", "default": 500},
            ]
        },
    },
    {
        "name": "list_focus_items",
        "display_name": "List Focus Items",
        "description": "List structured Focus items from the system database.",
        "category": "file",
        "icon": "◎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "include_completed": {"type": "boolean", "description": "Whether to include completed Focus items. Default true."},
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "upsert_focus_item",
        "display_name": "Upsert Focus Item",
        "description": "Create or update a structured Focus item in the system database.",
        "category": "file",
        "icon": "◎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Stable short identifier, snake_case preferred."},
                "title": {"type": "string", "description": "Short title (Focus名称)."},
                "description": {"type": "string", "description": "Human-readable description of what is being tracked."},
                "kind": {"type": "string", "enum": ["normal", "system"], "description": "normal or system"},
                "source": {"type": "string", "description": "Optional origin label, e.g. user, trigger, a2a, okr."},
            },
            "required": ["description"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "complete_focus_item",
        "display_name": "Complete Focus Item",
        "description": "Mark a structured Focus item completed.",
        "category": "file",
        "icon": "◎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Focus item identifier to complete."},
            },
            "required": ["key"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "write_file",
        "display_name": "Write File",
        "description": "Write or update a file in the workspace. Before creating a new document under workspace/, first inspect the relevant directories with list_files, prefer an existing topical subfolder over the workspace root, and create a new subfolder when the content belongs to a new category. Avoid placing standalone document files directly in workspace/ root unless the user explicitly wants that. Can update memory/memory.md, create documents in workspace/, create skills in skills/. When meaningful work produces reusable results, create the current Daily Memory at memory/<YYYY-MM-DD>/memory.md if needed; otherwise read and edit the existing file. Record outcomes rather than transcripts and avoid duplicate entries.",
        "category": "file",
        "icon": "✏️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, e.g.: memory/memory.md, workspace/reports/report.md, workspace/knowledge_base/notes.md. Prefer a meaningful subfolder instead of writing loose files into workspace/ root."},
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "delete_file",
        "display_name": "Delete File",
        "description": "Delete a file from the workspace. Cannot delete soul.md or tasks.json.",
        "category": "file",
        "icon": "🗑️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to delete"}
            },
            "required": ["path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "move_file",
        "display_name": "Move File",
        "description": "Move or rename a file or folder within the workspace. Use this for reorganizing workspace files, moving generated documents into subfolders, or renaming files (prefer it over shell commands like mv). Cannot move soul.md, tasks.json, or enterprise_info/. If destination_path is an existing folder or ends with '/', the original filename is preserved inside that folder. Does not overwrite by default.",
        "category": "file",
        "icon": "↪",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Current file or folder path, e.g.: workspace/report.md"},
                "destination_path": {"type": "string", "description": "Destination file/folder path, e.g.: workspace/archive/report.md or workspace/presentations/PPT/"},
                "overwrite": {"type": "boolean", "description": "Replace the destination if it already exists. Default false."},
            },
            "required": ["source_path", "destination_path"],
        },
        "config": {},
        "config_schema": {},
    },
    # --- Enhanced file management tools ---
    {
        "name": "edit_file",
        "display_name": "Edit File",
        "description": "Surgically replace a specific string inside an existing file without rewriting the whole content. Prefer this over write_file when you only need to change one or more sections. When meaningful work produces reusable results, maintain the existing current Daily Memory at memory/<YYYY-MM-DD>/memory.md after reading it; record outcomes rather than transcripts and avoid duplicate entries.",
        "category": "file",
        "icon": "✂️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to edit, e.g.: memory/memory.md, skills/my-skill/SKILL.md"},
                "old_string": {"type": "string", "description": "Exact text to find and replace. Must match exactly including whitespace and newlines."},
                "new_string": {"type": "string", "description": "Replacement text"},
                "replace_all": {"type": "boolean", "description": "Replace all occurrences if true (default: false)"},
            },
            "required": ["path", "old_string", "new_string"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "search_files",
        "display_name": "Search Files",
        "description": "Search for content patterns across files using regex. Returns matching lines with file paths and line numbers. Results capped at 50 per query.",
        "category": "file",
        "icon": "🔍",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for, e.g.: 'API_KEY', 'def\\\\s+\\\\w+'"},
                "path": {"type": "string", "description": "Directory to search in (default: root)"},
                "file_pattern": {"type": "string", "description": "File pattern to match (default: all files). e.g.: '*.md', '*.py'"},
                "ignore_case": {"type": "boolean", "description": "Case-insensitive search (default: false)"},
            },
            "required": ["pattern"],
        },
        "config": {},
        "config_schema": {},
    },
]
