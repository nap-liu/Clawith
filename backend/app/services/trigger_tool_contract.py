"""Canonical trigger tool definitions for seed and fallback catalogs."""

TRIGGER_TOOLS = [
    {
        "name": "set_trigger",
        "display_name": "Set Trigger",
        "description": "Create or re-enable a trigger; webhook_mode controls webhook processing. Creation requires type, config and reason. Re-enabling a "
        "disabled name patches only submitted fields, preserving type and fire history. Returns the "
        "complete management record.",
        "category": "aware",
        "icon": "⚡",
        "is_default": True,
        "parameters_schema": {
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
                    "description": "Type-specific configuration as a partial patch. Objects merge "
                    "recursively for every trigger type; arrays "
                    "replace whole. Omitted keys remain "
                    "unchanged. JSON null is a value; use "
                    "clear_fields to remove keys. cron requires "
                    "expr; once requires at (ISO 8601); "
                    "interval requires positive minutes; poll "
                    "requires url; on_message requires exactly "
                    "one of from_agent_id/from_user_id; webhook "
                    "accepts optional secret and generates its "
                    "callback token.",
                },
                "reason": {
                    "type": "string",
                    "description": "What you should do when this trigger "
                    "fires. This will be shown to you as "
                    "context when you wake up.",
                },
                "focus_ref": {
                    "type": "string",
                    "description": "Optional: identifier of the structured "
                    "Focus item that this trigger relates "
                    "to. If omitted, a Focus item is created "
                    "automatically from the trigger reason.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model UUID, key, or unique label. Omit to inherit the Agent model.",
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
                "soul": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to use the Agent's Soul for this trigger.",
                },
                "memory": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to use the Agent's memory for this trigger.",
                },
                "webhook_mode": {
                    "type": "string",
                    "enum": ["legacy", "queue", "merge"],
                    "description": "Webhook processing mode "
                    "(type=webhook only). Every "
                    "authenticated submission accepted by "
                    "the endpoint is stored byte-for-byte "
                    "in this agent's webhook/ inbox; the "
                    "wake context provides its event ID, "
                    "millisecond timestamp, file path, "
                    "size, and SHA-256, and you should "
                    "read the referenced file before "
                    "processing it. legacy (default) "
                    "wakes from only the newest event "
                    "while older inbox files remain "
                    "discoverable. queue wakes once per "
                    "event in FIFO order. merge wakes "
                    "once for the batch captured when "
                    "execution starts. Choose the mode "
                    "when creating the trigger; if "
                    "changing it later, briefly pause "
                    "upstream submissions and do not "
                    "switch during an active webhook "
                    "run.",
                },
                "clear_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicitly clear optional fields "
                    "using JSON Pointer paths, e.g. "
                    "/config/headers/X-Debug or "
                    "/expires_at. Omission preserves "
                    "values. Required/internal fields, "
                    "callback token and array elements "
                    "cannot be cleared. Do not write and "
                    "clear overlapping paths.",
                },
                "max_fires": {"type": ["integer", "null"], "minimum": 0},
                "cooldown_seconds": {"type": "integer", "minimum": 0},
                "expires_at": {
                    "type": ["string", "null"],
                    "description": "ISO 8601 timestamp with timezone; null removes expiry.",
                },
            },
            "required": ["name"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "update_trigger",
        "display_name": "Update Trigger",
        "description": "Update a trigger by exact id or name. Only submitted fields change. Returns the complete "
        "updated management record.",
        "category": "aware",
        "icon": "🔄",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the trigger to update"},
                "config": {
                    "type": "object",
                    "description": "Type-specific configuration as a partial patch. Objects merge "
                    "recursively for every trigger type; arrays "
                    "replace whole. Omitted keys remain "
                    "unchanged. JSON null is a value; use "
                    "clear_fields to remove keys. cron requires "
                    "expr; once requires at (ISO 8601); "
                    "interval requires positive minutes; poll "
                    "requires url; on_message requires exactly "
                    "one of from_agent_id/from_user_id; webhook "
                    "accepts optional secret and generates its "
                    "callback token.",
                },
                "reason": {"type": "string", "description": "New reason text"},
                "model": {
                    "type": "string",
                    "description": "Optional model UUID, key, or unique label. Empty means inherit the Agent model.",
                },
                "temperature": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 2,
                    "description": "Optional imagination override.",
                },
                "reasoning_effort": {
                    "type": "string",
                    "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                    "description": "Optional reasoning override. none disables thinking.",
                },
                "soul": {"type": "boolean", "description": "Whether to use the Agent's Soul."},
                "memory": {"type": "boolean", "description": "Whether to use the Agent's memory."},
                "webhook_mode": {
                    "type": "string",
                    "enum": ["legacy", "queue", "merge"],
                    "description": "For an existing webhook trigger "
                    "only. Briefly pause upstream "
                    "submissions and switch only when no "
                    "webhook run is active and no event "
                    "is pending or queued. The change is "
                    "immediate and affects subsequent "
                    "scheduling; it does not convert or "
                    "drain in-flight work. The existing "
                    "URL token, secret, and stored "
                    "webhook inbox files remain "
                    "unchanged. legacy uses the newest "
                    "event, queue processes FIFO, and "
                    "merge processes the batch captured "
                    "when execution starts.",
                },
                "clear_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicitly clear optional fields "
                    "using JSON Pointer paths, e.g. "
                    "/config/headers/X-Debug or "
                    "/expires_at. Omission preserves "
                    "values. Required/internal fields, "
                    "callback token and array elements "
                    "cannot be cleared. Do not write and "
                    "clear overlapping paths.",
                },
                "max_fires": {"type": ["integer", "null"], "minimum": 0},
                "cooldown_seconds": {"type": "integer", "minimum": 0},
                "expires_at": {
                    "type": ["string", "null"],
                    "description": "ISO 8601 timestamp with timezone; null removes expiry.",
                },
                "id": {"type": "string"},
                "is_enabled": {"type": "boolean"},
                "focus_ref": {"type": ["string", "null"]},
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "cancel_trigger",
        "display_name": "Cancel Trigger",
        "description": "Cancel (disable) a trigger by name. Use when a task is completed.",
        "category": "aware",
        "icon": "⏹️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the trigger to cancel"},
                "id": {"type": "string"},
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "delete_trigger",
        "display_name": "Delete Trigger",
        "description": "Delete a disabled trigger you no longer need. Cancel active triggers first. Past runs remain in history.",
        "category": "aware",
        "icon": "🗑️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Trigger name"},
                "id": {"type": "string", "description": "Trigger ID"},
            },
            "anyOf": [{"required": ["name"]}, {"required": ["id"]}],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "list_triggers",
        "display_name": "List Triggers",
        "description": "Read complete trigger configuration, reason, options, status and limits. Optional exact "
        "id/name lookup. Follow next_offset until null for all records.",
        "category": "aware",
        "icon": "📋",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "is_enabled": {"type": "boolean"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
        "config": {},
        "config_schema": {},
    },
]

TRIGGER_FUNCTIONS = [
    {
        "type": "function",
        "function": {"name": tool["name"], "description": tool["description"], "parameters": tool["parameters_schema"]},
    }
    for tool in TRIGGER_TOOLS
]
