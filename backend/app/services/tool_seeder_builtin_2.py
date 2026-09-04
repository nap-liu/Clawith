"""Builtin tool seed catalog part 2."""

from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_SEED
from app.services.media_tool_contract import SEND_MEDIA_TOOL_SEED


BUILTIN_TOOLS_PART_2 = [
    {
        "name": "find_files",
        "display_name": "Find Files",
        "description": "Find files matching glob patterns. Returns file paths with sizes and modification info. Results capped at 100 per query.",
        "category": "file",
        "icon": "📁",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match files, e.g.: '**/*.md', 'skills/*.md'"},
                "path": {"type": "string", "description": "Base directory for search (default: root)"},
            },
            "required": ["pattern"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "read_document",
        "display_name": "Read Document",
        "description": "Extract text from an office document (PDF, Word, Excel, PPT). The path is matched exactly and is never corrected to a similar filename, so copy the path from the attachment context or list_files output. Storage, materialization, size-limit, timeout, and parser failures are reported as distinct results.",
        "category": "file",
        "icon": "📑",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Document file path, e.g.: workspace/report.pdf"}
            },
            "required": ["path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "read_image",
        "display_name": "Read Image",
        "description": (
            "Read an image via a vision LLM: transcribe text or describe content. "
            "Admin-configurable; enabled by default."
        ),
        "category": "document",
        "icon": "👁️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "image_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 6,
                    "description": (
                        "要识别的图片列表：workspace 相对路径 / http(s) URL / base64 data URL。"
                    ),
                }
            },
            "required": ["image_paths"],
        },
        "config": {
            "model_id": None,
            "fallback_model_id": None,
            "input_modes": {
                "workspace_path": {"enabled": True},
                "url": {
                    "enabled": False,
                    "allowlist": [],
                    "fetch_timeout_seconds": 10,
                    "max_redirects": 3,
                },
                "base64": {"enabled": False, "max_bytes": 1048576},
            },
            "max_images_per_call": 6,
            "max_image_bytes_per_file": 5242880,
            "image_compression": {"max_width": 1920, "jpeg_quality": 85},
            "vision_call_timeout_seconds": 90,
            "vision_max_output_tokens": 4096,
        },
        "config_schema": {
            "fields": [
                {
                    "key": "model_id",
                    "label": "视觉模型",
                    "type": "llm_model_picker",
                    "filter": {"supports_vision": True},
                    "required": True,
                    "help": "选择一个已启用 supports_vision 的 LLM 模型",
                },
                {
                    "key": "input_modes.url.enabled",
                    "label": "允许 URL 输入",
                    "type": "checkbox",
                    "default": False,
                },
                {
                    "key": "input_modes.url.allowlist",
                    "label": "URL 允许列表（每行一条 domain glob）",
                    "type": "textarea",
                    "depends_on": {"input_modes.url.enabled": [True]},
                    "help": "例如：*.cdn.example.com，每行一条。即使在列表内，解析到私网 IP 仍会被拒绝。",
                },
                {
                    "key": "input_modes.base64.enabled",
                    "label": "允许 base64 输入",
                    "type": "checkbox",
                    "default": False,
                },
                {
                    "key": "input_modes.base64.max_bytes",
                    "label": "base64 单图上限（字节）",
                    "type": "number",
                    "default": 1048576,
                    "depends_on": {"input_modes.base64.enabled": [True]},
                },
                {
                    "key": "max_images_per_call",
                    "label": "单次最多图片数",
                    "type": "number",
                    "default": 6,
                    "min": 1,
                    "max": 10,
                },
                {
                    "key": "max_image_bytes_per_file",
                    "label": "单图大小上限（字节）",
                    "type": "number",
                    "default": 5242880,
                },
            ]
        },
    },
    {
        "name": "convert_csv_to_xlsx",
        "display_name": "CSV to Excel",
        "description": "Convert a CSV source file into an Excel .xlsx file. Create/edit the CSV first, then use this tool.",
        "category": "file",
        "icon": "📊",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Path to the source CSV file"},
                "target_path": {"type": "string", "description": "Path for the output Excel file (.xlsx)"},
            },
            "required": ["source_path", "target_path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "convert_html_to_pdf",
        "display_name": "HTML to PDF",
        "description": "Convert an HTML source file into a PDF document. Uses headless Chrome by default for higher-fidelity rendering of modern CSS and screen layouts, with WeasyPrint as a fallback.",
        "category": "file",
        "icon": "📄",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Path to the source HTML file"},
                "target_path": {"type": "string", "description": "Path for the output PDF file (.pdf)"},
                "design_width": {"type": "number", "description": "Optional browser viewport width in pixels, default 1280"},
                "design_height": {"type": "number", "description": "Optional browser viewport height in pixels, default 720"},
                "pdf_mode": {"type": "string", "enum": ["pages", "single"], "description": "pages outputs paginated PDF, single outputs one long full-page PDF. Default: pages"},
                "scale": {"type": "number", "description": "Optional Chrome PDF scale for paginated output, default 0.64"},
                "paper_width": {"type": "number", "description": "Optional paper width in inches for paginated output, default 8.27"},
                "paper_height": {"type": "number", "description": "Optional paper height in inches for paginated output, default 11.69"},
            },
            "required": ["source_path", "target_path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "convert_html_to_pptx",
        "display_name": "HTML to PowerPoint",
        "description": "Convert an HTML source file into a PowerPoint .pptx file. By default, render_mode='editable' opens the HTML in headless Chrome, samples real element positions/styles, and maps explicit .slide/data-slide nodes or top-level page sections into editable PPT elements. Use render_mode='visual' as a high-fidelity screenshot fallback when exact visual preservation is more important than editability.",
        "category": "file",
        "icon": "📽️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Path to the source HTML file"},
                "target_path": {"type": "string", "description": "Path for the output PowerPoint file (.pptx)"},
                "design_width": {"type": "number", "description": "Optional source design width in pixels, default 1280"},
                "design_height": {"type": "number", "description": "Optional source design height in pixels, default 720"},
                "render_mode": {"type": "string", "enum": ["editable", "visual"], "description": "editable maps HTML/CSS into editable PPT elements using Chrome layout sampling; visual preserves styling with Chrome-rendered screenshots as a fallback. Default: editable"},
                "render_scale": {"type": "number", "description": "Optional Chrome raster scale for screenshots and complex CSS captures. Higher values improve sharpness but increase PPTX size. Default: 2, clamped between 1 and 4"},
            },
            "required": ["source_path", "target_path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "convert_markdown_to_docx",
        "display_name": "Markdown to Word",
        "description": "Convert a Markdown source file into a Word .docx file.",
        "category": "file",
        "icon": "📝",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Path to the source Markdown file"},
                "target_path": {"type": "string", "description": "Path for the output Word file (.docx)"},
            },
            "required": ["source_path", "target_path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "convert_markdown_to_pdf",
        "display_name": "Markdown to PDF",
        "description": "Convert a Markdown source file into a PDF document.",
        "category": "file",
        "icon": "📄",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source_path": {"type": "string", "description": "Path to the source Markdown file"},
                "target_path": {"type": "string", "description": "Path for the output PDF file (.pdf)"},
            },
            "required": ["source_path", "target_path"],
        },
        "config": {},
        "config_schema": {},
    },
    # --- Aware trigger management tools ---
    {
        "name": "set_trigger",
        "display_name": "Set Trigger",
        "description": "Set a new trigger to wake yourself up at a specific time or condition. Use this to schedule future actions, monitor changes, or wait for messages. The trigger will fire and invoke you with the reason text as context. Every trigger is attached to a focus item; if focus_ref is omitted, the system will automatically create a focus item from the reason and attach the trigger to it. Trigger types: 'cron' (recurring schedule), 'once' (fire once at a time), 'interval' (every N minutes), 'poll' (HTTP monitoring), 'on_message' (when another agent or human replies — identify exactly one actor with from_agent_id or from_user_id), 'webhook' (receive external HTTP POST — system generates a unique URL, give it to the user so they can configure it in external services like GitHub, Grafana, etc.). For type=webhook you can also set webhook_mode to control how bursts of rapid triggers are handled — see the webhook_mode parameter.",
        "category": "aware",
        "icon": "⚡",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique name for this trigger, e.g. 'daily_briefing' or 'wait_<name>_reply'"},
                "type": {"type": "string", "enum": ["cron", "once", "interval", "poll", "on_message", "webhook"], "description": "Trigger type"},
                "config": {"type": "object", "description": "Type-specific config. cron: {\"expr\": \"0 9 * * *\", \"timezone\": \"optional IANA name\"}. once: {\"at\": \"2026-03-10T09:00:00\"}; an offset is honored when present, otherwise the effective Agent timezone is used. interval: {\"minutes\": 30}. poll: {\"url\": \"...\", \"json_path\": \"$.status\", \"fire_on\": \"change\", \"interval_min\": 5}. on_message must contain exactly one canonical actor: {\"from_agent_id\": \"<agent_id>\"} or {\"from_user_id\": \"<user_id>\"}. webhook: {\"secret\": \"optional_hmac_secret\"} (system auto-generates the URL)"},
                "reason": {"type": "string", "description": "What you should do when this trigger fires. This will be shown to you as context when you wake up."},
                "focus_ref": {"type": "string", "description": "Optional: identifier of the structured Focus item that this trigger relates to. If omitted, a Focus item is created automatically from the trigger reason."},
                "model": {"type": "string", "description": "Optional model UUID, key, or unique label. Omit to inherit the Agent model."},
                "temperature": {"type": "number", "minimum": 0, "maximum": 2, "description": "Optional imagination override. Omit to inherit the Digital Employee setting."},
                "reasoning_effort": {"type": "string", "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max"], "description": "Optional reasoning override. none disables thinking; omit to inherit."},
                "soul": {"type": "boolean", "default": True, "description": "Whether to use the Agent's Soul for this trigger."},
                "memory": {"type": "boolean", "default": True, "description": "Whether to use the Agent's memory for this trigger."},
                "webhook_mode": {
                    "type": "string",
                    "enum": ["legacy", "queue", "merge"],
                    "description": "Webhook processing mode (type=webhook only). Every authenticated submission accepted by the endpoint is stored byte-for-byte in this agent's webhook/ inbox; the wake context provides its event ID, millisecond timestamp, file path, size, and SHA-256, and you should read the referenced file before processing it. legacy (default) wakes from only the newest event while older inbox files remain discoverable. queue wakes once per event in FIFO order. merge wakes once for the batch captured when execution starts. Choose the mode when creating the trigger; if changing it later, briefly pause upstream submissions and do not switch during an active webhook run.",
                },
            },
            "required": ["name", "type", "config", "reason"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "update_trigger",
        "display_name": "Update Trigger",
        "description": "Update an existing trigger's configuration or reason. Use this to adjust timing, change parameters, etc. For example, change interval from 5 minutes to 30 minutes.",
        "category": "aware",
        "icon": "🔄",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name of the trigger to update"},
                "config": {"type": "object", "description": "New config. once.at accepts ISO 8601; values without an offset use the effective Agent timezone. For webhook triggers this is a partial patch: omitted URL token, secret, webhook mode, and internal queue state remain unchanged."},
                "reason": {"type": "string", "description": "New reason text"},
                "model": {"type": "string", "description": "Optional model UUID, key, or unique label. Empty means inherit the Agent model."},
                "temperature": {"type": "number", "minimum": 0, "maximum": 2, "description": "Optional imagination override."},
                "reasoning_effort": {"type": "string", "enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max"], "description": "Optional reasoning override. none disables thinking."},
                "soul": {"type": "boolean", "description": "Whether to use the Agent's Soul."},
                "memory": {"type": "boolean", "description": "Whether to use the Agent's memory."},
                "webhook_mode": {
                    "type": "string",
                    "enum": ["legacy", "queue", "merge"],
                    "description": "For an existing webhook trigger only. Briefly pause upstream submissions and switch only when no webhook run is active and no event is pending or queued. The change is immediate and affects subsequent scheduling; it does not convert or drain in-flight work. The existing URL token, secret, and stored webhook inbox files remain unchanged. legacy uses the newest event, queue processes FIFO, and merge processes the batch captured when execution starts.",
                },
            },
            "required": ["name"],
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
            },
            "required": ["name"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "list_triggers",
        "display_name": "List Triggers",
        "description": "List all your triggers, including each trigger's creator and execution user IDs.",
        "category": "aware",
        "icon": "📋",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {},
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "send_channel_file",
        "display_name": "Send File",
        "description": "Send a workspace file through an existing conversation or to a person. Omit all targets only for the current conversation. Use exact session_id for another existing person/group Session, or canonical user_id (and channel when needed) for direct person delivery. Never provide both session_id and user_id.",
        "category": "communication",
        "icon": "📎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Workspace-relative path to the file, e.g. workspace/report.md"},
                "user_id": {"type": "string", "description": "Canonical platform user_id for direct person delivery. Mutually exclusive with session_id."},
                "session_id": {"type": "string", "description": "Exact existing Session UUID for person or group delivery. Mutually exclusive with user_id."},
                "channel": {"type": "string", "enum": ["feishu", "slack"], "description": "Executable explicit file route chosen by the Agent."},
                "message": {"type": "string", "description": "Optional message to accompany the file"},
            },
            "required": ["file_path"],
        },
        "config": {},
        "config_schema": {},
    },
    SEND_MEDIA_TOOL_SEED,
    # NOTE: send_feishu_message is defined in the 'feishu' category section below.
    # It was previously duplicated here under 'communication', which could cause
    # 'Tool names must be unique' errors when the DB lacked a UNIQUE constraint.
    {
        "name": "send_platform_message",
        "display_name": "Platform Message",
        "description": "Send a proactive message to a first-party platform user (web or app). The message appears in their platform chat history and is pushed in real-time if they are online.",
        "category": "communication",
        "icon": "🌐",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Canonical platform user_id of the recipient."},
                "message": {"type": "string", "description": "Message content to send"},
            },
            "required": ["user_id", "message"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "send_channel_message",
        "display_name": "Channel Message",
        "description": "Send a message to a related person through an external IM route. Address the person only by canonical user_id. If several routes are valid, the Agent must choose channel; the platform never selects a first match.",
        "category": "communication",
        "icon": "💬",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Recipient's canonical platform user_id from Relationships/search results."},
                "message": {"type": "string", "description": "Message content to send"},
                "channel": {"type": "string", "enum": ["feishu", "dingtalk", "wecom", "slack", "teams", "wechat"], "description": "External route chosen by the Agent when multiple valid routes exist."},
            },
            "required": ["user_id", "message"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "recall_message",
        "display_name": "Recall Message",
        "description": (
            "Recall one message previously sent by this digital employee through any IM channel. "
            "Use only the exact local message_id returned by send_session_message/send_channel_message "
            "or shown by read_session_messages. Never guess a message ID. Unsupported provider paths "
            "return a normalized unsupported result without deleting local audit history."
        ),
        "category": "communication",
        "icon": "undo",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Exact local ChatMessage UUID of the outbound assistant message.",
                },
            },
            "required": ["message_id"],
            "additionalProperties": False,
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "send_session_message",
        "display_name": "Session Message",
        "description": (
            "Send text only to one human conversation that already exists on the platform. "
            "Provide the exact session_id returned by list_sessions/search_sessions; the existing "
            "Session's bound platform/IM route is used unchanged. This tool never creates a Session, "
            "discovers a person, selects or changes a channel, sends files, or contacts another "
            "digital employee. If no suitable Session exists, use send_channel_message for an external-IM "
            "person or send_platform_message for a platform user. For a native group mention, you MUST call "
            "this tool with mention_user_ids or mention_all=true; writing @name or @everyone in a normal "
            "assistant reply is plain text and does not create a native mention."
        ),
        "category": "communication",
        "icon": "✉️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Exact human ChatSession UUID returned by list_sessions/search_sessions.",
                },
                "message": {
                    "type": "string",
                    "description": (
                        "Business text to send. When a native mention option is present, do not prefix "
                        "@names, @everyone, or external IDs; the transport renders the mention exactly once."
                    ),
                },
                "mention_user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                    "description": (
                        "Optional canonical platform user_ids to mention natively in the target group. "
                        "Each person must be a member of the target group, and the bound channel must "
                        "support native group mentions."
                    ),
                },
                "mention_all": {
                    "type": "boolean",
                    "description": (
                        "Optionally mention all members of the target group natively. "
                        "Cannot be combined with mention_user_ids."
                    ),
                },
            },
            "required": ["session_id", "message"],
            "additionalProperties": False,
        },
        "config": {},
        # The canonical Session-message capability owns the DingTalk mention-card
        # card template. The compatibility group-only wrapper reads this same
        # config through the shared delivery runtime, so administrators configure
        # one value only. As with request_confirmation, standard tool-config
        # resolution provides agent override -> tenant default -> tool default.
        "config_schema": {
            "fields": [
                {
                    "key": "card_template_id",
                    "label": "agent.tools.sessionMessage.cardTemplateId",
                    "type": "string",
                    "placeholder": "agent.tools.sessionMessage.cardTemplateIdPlaceholder",
                    "help_text": "agent.tools.sessionMessage.cardTemplateIdHelp",
                    "description": (
                        "用于钉钉群原生 @ 投递的互动卡片模板 ID。模板必须包含唯一的 "
                        "content 动态 Markdown 字段。可配置企业默认值并按数字员工覆盖；"
                        "不配置时，带 @ 的钉钉群消息明确失败且不会降级为普通消息。"
                    ),
                },
            ]
        },
    },
    {
        "name": "send_group_session_message",
        "display_name": "Group Session Message",
        "description": (
            "Compatibility tool for sending text to an existing external-IM group by exact "
            "session_id. Prefer send_session_message for new work; this tool remains available "
            "for existing workflows and accepts group Sessions only. For a native group mention, you MUST "
            "call this tool with mention_user_ids or mention_all=true; writing @name or @everyone in a normal "
            "assistant reply is plain text and does not create a native mention."
        ),
        "category": "communication",
        "icon": "📣",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Exact group ChatSession UUID returned by list_sessions/search_sessions.",
                },
                "message": {
                    "type": "string",
                    "description": (
                        "Business text to send. When a native mention option is present, do not prefix "
                        "@names, @everyone, or external IDs; the transport renders the mention exactly once."
                    ),
                },
                "mention_user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                    "description": (
                        "Optional canonical platform user_ids to mention natively in the target group. "
                        "Each person must be a member of the target group, and the bound channel must "
                        "support native group mentions."
                    ),
                },
                "mention_all": {
                    "type": "boolean",
                    "description": (
                        "Optionally mention all members of the target group natively. "
                        "Cannot be combined with mention_user_ids."
                    ),
                },
            },
            "required": ["session_id", "message"],
            "additionalProperties": False,
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "search_contacts",
        "display_name": "Search Contacts",
        "description": "Search people and digital employees as candidates for relationship network editing. Only use this tool when the user explicitly asks you to search, review, or edit the relationship network. Do not use it proactively just because you want to contact someone. Human results contain user_id; digital employee results contain agent_id. Names are display-only.",
        "category": "communication",
        "icon": "🔎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name, department, email, pinyin, or exact phone number to search."},
                "type": {"type": "string", "enum": ["all", "human", "agent"], "description": "Optional contact type filter."},
                "limit": {"type": "integer", "description": "Maximum number of results to return, from 1 to 50."},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "add_contact",
        "display_name": "Add Contact",
        "description": "Add a person or digital employee using exactly one canonical user_id or agent_id returned by search_contacts. Only call this tool after the user explicitly asked to edit the relationship network and the agent creator confirmed the selected target. Do not add contacts proactively.",
        "category": "communication",
        "icon": "➕",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Canonical natural-person user_id."},
                "agent_id": {"type": "string", "description": "Canonical digital-employee agent_id."},
                "relation": {"type": "string", "description": "Relationship label, such as collaborator, stakeholder, peer, team_member, or other."},
                "description": {"type": "string", "description": "Optional short note explaining why this contact is needed."},
            },
            "oneOf": [
                {"required": ["user_id"], "not": {"required": ["agent_id"]}},
                {"required": ["agent_id"], "not": {"required": ["user_id"]}},
            ],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "remove_contact",
        "display_name": "Remove Contact",
        "description": "Remove a person or digital employee using exactly one canonical user_id or agent_id returned by search_contacts. Only call this tool after the user explicitly asked to edit the relationship network and the agent creator confirmed the selected target. Do not remove contacts proactively.",
        "category": "communication",
        "icon": "minus",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Canonical natural-person user_id."},
                "agent_id": {"type": "string", "description": "Canonical digital-employee agent_id."},
            },
            "oneOf": [
                {"required": ["user_id"], "not": {"required": ["agent_id"]}},
                {"required": ["agent_id"], "not": {"required": ["user_id"]}},
            ],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "send_message_to_agent",
        "display_name": "Agent Message",
        "description": "Send a message to a digital employee colleague. Decision guide: target needs to DO WORK and return results? → task_delegate. Just FYI? → notify. Quick factual question? → consult. When unsure, prefer task_delegate.\n\nRESET: If an ongoing conversation with a colleague gets stuck — the same tool failing over and over, repeated identical errors, looping, or visibly corrupted/garbled context — set new_conversation=true to discard the stale history and start a fresh, clean thread, then continue.",
        "category": "communication",
        "icon": "🤖",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Target digital employee's canonical agent_id from Relationships/search results."},
                "message": {"type": "string", "description": "Message content to send"},
                "msg_type": {"type": "string", "enum": ["notify", "consult", "task_delegate"], "description": "Decision guide: (1) Will the target need to DO WORK and return results? → task_delegate. (2) Is this just a one-way FYI? → notify. (3) Quick factual question needing immediate answer? → consult. When unsure, prefer task_delegate."},
                "new_conversation": {"type": "boolean", "description": "默认 false。仅当当前与该同事的对话明显异常时设为 true 来主动重置 —— 例如对话反复报同一个错、陷入循环、或历史上下文看起来已损坏/混乱。设为 true 会开启一条全新对话线程,丢弃旧的(可能已损坏的)历史,从干净状态重新开始。正常往来请保持 false 或省略。"},
            },
            "required": ["agent_id", "message", "msg_type"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "start_dingtalk_channel_provisioning",
        "display_name": "配置钉钉数字员工",
        "description": (
            "为当前数字员工配置钉钉机器人通道。已配置时默认直接告知用户通道已经可用，"
            "无需再次授权。只有用户明确要求强制重配时才设置 force_reconfigure=true；"
            "强制重配会创建新的钉钉机器人应用，原应用需要用户在钉钉后台自行清理。"
            "未配置或明确强制重配时，工具返回授权链接，用户授权后平台自动完成配置。"
        ),
        "category": "communication",
        "icon": "link",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "force_reconfigure": {
                    "type": "boolean",
                    "description": (
                        "默认 false。仅当用户明确要求强制覆盖当前钉钉通道时设为 true；"
                        "普通配置请求保持 false。"
                    ),
                    "default": False,
                },
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "get_dingtalk_channel_provisioning_status",
        "display_name": "查询钉钉配置状态",
        "description": "查询当前数字员工钉钉机器人通道自动配置流程的状态。当用户询问钉钉授权是否完成、链接是否过期、或配置是否已经生效时调用。",
        "category": "communication",
        "icon": "clock",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "provisioning_id": {"type": "string", "description": "start_dingtalk_channel_provisioning 返回的配置编号。"},
            },
            "required": ["provisioning_id"],
        },
        "config": {},
        "config_schema": {},
    },
]
