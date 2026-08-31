"""Builtin tool seed catalog part 5."""

from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_SEED
from app.services.media_tool_contract import SEND_MEDIA_TOOL_SEED


BUILTIN_TOOLS_PART_5 = [
    {
        "name": "get_my_okr",
        "display_name": "My OKR",
        "description": (
            "Get your own OKR Objectives and Key Results for the current period. "
            "Returns a structured view of your goals, current progress values, plus objective_id and kr_id references "
            "you need to update existing OKRs correctly. Call this before changing progress, KR content, "
            "or Objective text so you reuse the current records instead of creating duplicates."
        ),
        "category": "okr",
        "icon": "🎯",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "period_start": {
                    "type": "string",
                    "description": "Optional: ISO date string (YYYY-MM-DD). Defaults to current period.",
                },
                "period_end": {
                    "type": "string",
                    "description": "Optional: ISO date string (YYYY-MM-DD).",
                },
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "update_kr_progress",
        "display_name": "Update KR Progress",
        "description": (
            "Update the current progress value for a Key Result. Use get_my_okr first to obtain "
            "the kr_id. The status (on_track / at_risk / behind / completed) is automatically "
            "computed from the progress ratio, or you can override it explicitly. "
            "A progress log entry is recorded for full audit history."
        ),
        "category": "okr",
        "icon": "📈",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "kr_id": {
                    "type": "string",
                    "description": "UUID of the Key Result to update. Get this from get_my_okr.",
                },
                "value": {
                    "type": "number",
                    "description": "New current value (e.g. 4.2 for a KR with target 5.0).",
                },
                "note": {
                    "type": "string",
                    "description": "Optional note explaining the progress update (e.g. 'Completed weekly review session').",
                },
                "status": {
                    "type": "string",
                    "enum": ["on_track", "at_risk", "behind", "completed"],
                    "description": "Optional: override the auto-computed status.",
                },
            },
            "required": ["kr_id", "value"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "update_kr_content",
        "display_name": "Update KR Content",
        "description": (
            "Update the content fields of one of YOUR OWN Key Results, such as title, target value, unit, "
            "focus reference, or status. Use get_my_okr first to obtain the kr_id. "
            "This tool is for changing KR definition/content, not reporting progress. "
            "If the user says to change, revise, adjust, or replace an existing KR target or wording, "
            "prefer this tool instead of create_key_result."
        ),
        "category": "okr",
        "icon": "✏️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "kr_id": {
                    "type": "string",
                    "description": "UUID of the Key Result to update (from get_my_okr).",
                },
                "title": {
                    "type": "string",
                    "description": "Optional new KR title.",
                },
                "target_value": {
                    "type": "number",
                    "description": "Optional new target value.",
                },
                "unit": {
                    "type": "string",
                    "description": "Optional new unit label.",
                },
                "focus_ref": {
                    "type": "string",
                    "description": "Optional new focus file reference.",
                },
                "status": {
                    "type": "string",
                    "enum": ["on_track", "at_risk", "behind", "completed"],
                    "description": "Optional explicit status override.",
                },
            },
            "required": ["kr_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        # collect_okr_progress — legacy OKR Agent heartbeat collection path.
        # This replaces the need to contact each member individually.
        "name": "collect_okr_progress",
        "display_name": "Collect OKR Progress",
        "description": (
            "Legacy batch sync for reported KR progress. Prefer direct OKR tools such as "
            "get_my_okr and update_kr_progress for new work. Returns a summary of how many "
            "KRs were updated."
        ),
        "category": "okr",
        "icon": "📊",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # generate_okr_report — OKR Agent calls this to produce the structured report.
        # The tool writes the report to WorkReport and returns the markdown content
        # so the digital employee can deliver it through an appropriate channel.
        "name": "generate_okr_report",
        "display_name": "Generate OKR Report",
        "description": (
            "Generate a structured OKR progress report (daily or weekly) for the current "
            "period. The report summarizes all Objectives and Key Results, highlights items "
            "at risk or behind, and shows overall team health metrics. The report is saved "
            "to the database and to your workspace/reports/ folder. Returns the full report "
            "markdown so you can share it with the team through an appropriate channel."
        ),
        "category": "okr",
        "icon": "📋",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "report_type": {
                    "type": "string",
                    "enum": ["daily", "weekly"],
                    "description": "Whether to generate a daily or weekly report.",
                },
            },
            "required": ["report_type"],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # get_okr_settings — lets OKR Agent read the tenant's OKR configuration so it
        # can determine whether reports are due, what time they're scheduled, etc.
        "name": "get_okr_settings",
        "display_name": "Get OKR Settings",
        "description": (
            "Read the OKR configuration for this team, including whether daily/weekly "
            "reports are enabled, the configured report time, period frequency, and more. "
            "Use this at the start of your heartbeat to decide whether a report is due today."
        ),
        "category": "okr",
        "icon": "⚙️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # create_objective — OKR Agent uses this after conversation-based confirmation
        # to create an O for the company, a user, or an agent. Only OKR Agent has this tool.
        "name": "create_objective",
        "display_name": "Create Objective",
        "description": (
            "Create an OKR Objective for the company, a specific user, or a specific agent. "
            "Call this after confirming the objective with the relevant person through conversation. "
            "Use this only when a new Objective needs to be created for the period. "
            "If the person already has a matching Objective and just wants to revise it, use update_objective instead. "
            "For a company-level objective, omit both user_id and agent_id. "
            "For an individual objective, pass exactly one canonical user_id or agent_id from context. "
            "period_start and period_end must be ISO date strings (YYYY-MM-DD)."
        ),
        "category": "okr",
        "icon": "🎯",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "The objective title (concise, inspiring, directional).",
                },
                "description": {
                    "type": "string",
                    "description": "Optional detailed description of the objective.",
                },
                "user_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Canonical platform User UUID. Omit for company or agent objectives.",
                },
                "agent_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Canonical platform Agent UUID. Omit for company or user objectives.",
                },
                "period_start": {
                    "type": "string",
                    "description": "ISO date string for the start of the OKR period (e.g. '2026-04-01').",
                },
                "period_end": {
                    "type": "string",
                    "description": "ISO date string for the end of the OKR period (e.g. '2026-06-30').",
                },
            },
            "required": ["title", "period_start", "period_end"],
            "not": {"required": ["user_id", "agent_id"]},
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # create_key_result — OKR Agent creates a measurable KR under a confirmed objective.
        "name": "create_key_result",
        "display_name": "Create Key Result",
        "description": (
            "Create a Key Result (KR) under an existing Objective. "
            "Get the objective_id first using get_okr. "
            "Use this only for a brand-new KR. If the user is revising the wording, target value, unit, "
            "or focus reference of an existing KR, use update_kr_content instead. "
            "target_value is the goal number (e.g. 50000 for 50000 followers). "
            "unit is optional but recommended for clarity (e.g. '%', 'NPS', '万元', 'followers')."
        ),
        "category": "okr",
        "icon": "🔑",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "objective_id": {
                    "type": "string",
                    "description": "UUID of the parent Objective.",
                },
                "title": {
                    "type": "string",
                    "description": "The KR title (specific, measurable outcome).",
                },
                "target_value": {
                    "type": "number",
                    "description": "The target number to achieve (e.g. 50000).",
                },
                "unit": {
                    "type": "string",
                    "description": "Optional unit label (e.g. '%', 'followers', '万元', 'NPS score').",
                },
                "focus_ref": {
                    "type": "string",
                    "description": "Optional: basename of the focus file that tracks this KR (e.g. 'content_quality').",
                },
            },
            "required": ["objective_id", "title", "target_value"],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # update_objective — available to ALL agents, but with ownership enforcement:
        # regular agents can only modify their own O; OKR Agent can modify any O.
        "name": "update_objective",
        "display_name": "Update Objective",
        "description": (
            "Modify an Objective's title, description, status, or period dates. "
            "Regular agents can only update their own Objectives — call get_my_okr first "
            "to get your objective_id. The OKR Agent can update any member's Objective. "
            "Only provide the fields you want to change. If the request is to revise an existing OKR's "
            "goal text rather than create a new one, prefer this tool over create_objective."
        ),
        "category": "okr",
        "icon": "✏️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "objective_id": {
                    "type": "string",
                    "description": "UUID of the Objective to update. Get from get_my_okr (own) or get_okr (any).",
                },
                "title": {
                    "type": "string",
                    "description": "New title for the objective.",
                },
                "description": {
                    "type": "string",
                    "description": "New description.",
                },
                "status": {
                    "type": "string",
                    "enum": ["draft", "active", "completed", "archived"],
                    "description": "New status for the objective.",
                },
                "period_start": {
                    "type": "string",
                    "description": "New period start date (YYYY-MM-DD).",
                },
                "period_end": {
                    "type": "string",
                    "description": "New period end date (YYYY-MM-DD).",
                },
            },
            "required": ["objective_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        # update_any_kr_progress — OKR Agent exclusive: update KR for any member.
        # Unlike update_kr_progress (self-report), this can update anyone's KR.
        # Used after collecting progress data through conversation.
        "name": "update_any_kr_progress",
        "display_name": "Update Any KR Progress",
        "description": (
            "Update the progress value of any team member's Key Result. "
            "This is the OKR Agent's exclusive version of update_kr_progress — it can update "
            "KRs belonging to any user or agent, not just the caller's own. "
            "Use this ONLY after confirming the value with the KR owner through conversation. "
            "Get kr_id from get_okr. Optionally provide a note explaining the source."
        ),
        "category": "okr",
        "icon": "📈",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "kr_id": {
                    "type": "string",
                    "description": "UUID of the Key Result to update. Get from get_okr.",
                },
                "value": {
                    "type": "number",
                    "description": "New current value for this KR.",
                },
                "note": {
                    "type": "string",
                    "description": "Source or context note (e.g. 'Reported by user in weekly check-in').",
                },
                "status": {
                    "type": "string",
                    "enum": ["on_track", "at_risk", "behind", "completed"],
                    "description": "Optional: override the auto-computed status.",
                },
            },
            "required": ["kr_id", "value"],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # generate_monthly_okr_report — OKR Agent exclusive: produce the monthly summary report.
        # Called automatically by the monthly_okr_report system cron trigger, or on-demand.
        "name": "generate_monthly_okr_report",
        "display_name": "Generate Monthly OKR Report",
        "description": (
            "Generate the monthly OKR progress summary report. Covers all Objectives and Key "
            "Results for the current period, highlights completed and at-risk items, and provides "
            "a closing action note. Saved to WorkReport (report_type='monthly') and "
            "workspace/reports/. Returns the full Markdown so you can send it to admins."
        ),
        "category": "okr",
        "icon": "📅",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    {
        # upsert_member_daily_report — OKR Agent exclusive: create or revise a member daily report.
        "name": "upsert_member_daily_report",
        "display_name": "Upsert Member Daily Report",
        "description": (
            "Create or update the final normalized daily report for any member in the company. "
            "Use this after discussing progress with the member and distilling their update into "
            "one concise final report. Pass exactly one canonical user_id or agent_id. "
            "The stored content should stay within 2000 characters."
        ),
        "category": "okr",
        "icon": "📝",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "report_date": {
                    "type": "string",
                    "description": "Report date in YYYY-MM-DD format.",
                },
                "content": {
                    "type": "string",
                    "description": "Final concise daily report content. Keep it within 2000 characters.",
                },
                "user_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Canonical platform User UUID for a natural person.",
                },
                "agent_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Canonical platform Agent UUID for a digital employee.",
                },
                "source": {
                    "type": "string",
                    "description": "Optional source tag such as okr_agent_assisted or manual.",
                },
            },
            "required": ["report_date", "content"],
            "oneOf": [
                {"required": ["user_id"], "not": {"required": ["agent_id"]}},
                {"required": ["agent_id"], "not": {"required": ["user_id"]}},
            ],
        },
        "config": {"okr_agent_only": True},
        "config_schema": {},
    },
    # --- Feishu Integration Tools ---
    # These tools require a configured Feishu channel to function.
    # They are NOT enabled by default — agents with Feishu channels should enable them.
    {
        "name": "send_feishu_message",
        "display_name": "Feishu Message",
        "description": "Send a message to a human colleague via Feishu. Can only message people in your relationships.",
        "category": "feishu",
        "icon": "💬",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Recipient's canonical platform user_id. Provider IDs are resolved internally."},
                "message": {"type": "string", "description": "Message content to send"},
            },
            "required": ["user_id", "message"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_user_search",
        "display_name": "Feishu User Search",
        "description": "Search related Feishu colleagues by name. Returns canonical platform user_id, display name, and department; names are discovery-only.",
        "category": "feishu",
        "icon": "🔍",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The colleague's name to search for, e.g. '覃睿' or '张三'"},
            },
            "required": ["name"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_create_app",
        "display_name": "Bitable Create",
        "description": "在飞书云盘中新建一个多维表格（Bitable）应用。创建后返回可直接访问的链接和 App Token，下一步可以通过 bitable_list_tables 查看初始数据表。",
        "category": "feishu",
        "icon": "📊",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "新多维表格的名称，例如「项目追踪表」"},
                "folder_token": {"type": "string", "description": "可选：父文件夹的 folder_token。不填则创建到「我的空间」根目录。"},
            },
            "required": ["name"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_list_tables",
        "display_name": "Bitable List Tables",
        "description": "列出飞书多维表格内的所有数据表 (Tables)。url 支持表格链接或 Wiki 链接。使用此工具了解请求的多维表格中有哪些表。",
        "category": "feishu",
        "icon": "📊",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "多维表格的 URL 链接。"},
            },
            "required": ["url"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_list_fields",
        "display_name": "Bitable List Fields",
        "description": "列出飞书多维表格指定数据表中的所有字段 (Fields)。url 支持表格链接或 Wiki 链接。在查询或修改数据前，必须先调用此工具了解字段名称和类型。",
        "category": "feishu",
        "icon": "⌨️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "多维表格的 URL 链接。"},
                "table_id": {"type": "string", "description": "具体的数据表 ID，如果 url 中包含 tbl 则可以不填。"},
            },
            "required": ["url"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_query_records",
        "display_name": "Bitable Query Records",
        "description": "查询飞书多维表格中的数据行。可以提供过滤条件 (filter)。",
        "category": "feishu",
        "icon": "🔍",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "多维表格的 URL 链接。"},
                "table_id": {"type": "string", "description": "具体的数据表 ID，如果 url 中包含 tbl 则可以不填。"},
                "filter_info": {"type": "string", "description": "可选，FQL 语法的过滤条件，例如 'CurrentValue.[Status]=\"Done\"'。如不确定过滤语法，可以不填，由你臺己在本地过滤返回的所有数据。"},
                "max_results": {"type": "integer", "description": "最大返回条数 (默认 100)"},
            },
            "required": ["url"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_create_record",
        "display_name": "Bitable Create Record",
        "description": "在飞书多维表格中新增一行数据。fields 参数是一个字典，key 是字段名 (需要先通过 bitable_list_fields 获取)，value 是对应的值。",
        "category": "feishu",
        "icon": "➕",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "多维表格的 URL 链接。"},
                "table_id": {"type": "string", "description": "具体的数据表 ID，如果 url 中包含 tbl 则可以不填。"},
                "fields": {"type": "string", "description": "一个 JSON 字符串，代表要插入的 fields。例如：'{\"Name\": \"张三\", \"Age\": 30}'"},
            },
            "required": ["url", "fields"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_update_record",
        "display_name": "Bitable Update Record",
        "description": "更新飞书多维表格中的指定行数据。",
        "category": "feishu",
        "icon": "✏️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "多维表格的 URL 链接。"},
                "table_id": {"type": "string", "description": "具体的数据表 ID，如果 url 中包含 tbl 则可以不填。"},
                "record_id": {"type": "string", "description": "要更新的 record_id，通过 bitable_query_records 获取。"},
                "fields": {"type": "string", "description": "一个 JSON 字符串，代表要更新的 fields。例如：'{\"Status\": \"Done\"}'"},
            },
            "required": ["url", "record_id", "fields"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "bitable_delete_record",
        "display_name": "Bitable Delete Record",
        "description": "删除飞书多维表格中的指定行数据。",
        "category": "feishu",
        "icon": "🗑️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "多维表格的 URL 链接。"},
                "table_id": {"type": "string", "description": "具体的数据表 ID，如果 url 中包含 tbl 则可以不填。"},
                "record_id": {"type": "string", "description": "要删除的 record_id，通过 bitable_query_records 获取。"},
            },
            "required": ["url", "record_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_doc_search",
        "display_name": "Feishu Doc Search",
        "description": "Search Feishu cloud documents by keyword using the official document search API. Useful when a wiki or knowledge base has too many files to browse manually.",
        "category": "feishu",
        "icon": "🔎",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keyword, e.g. '恩菲' or '客户周报'"},
                "docs_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["doc", "docx", "sheet", "bitable", "file", "folder", "mindnote", "slides"]},
                    "description": "Optional file type filter.",
                },
                "count": {"type": "integer", "description": "Number of results to return (default 10, max 50)."},
                "offset": {"type": "integer", "description": "Result offset for pagination (default 0)."},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_doc_read",
        "display_name": "Feishu Doc Read",
        "description": "Read the text content of a Feishu document (Docx). Provide the document token from its URL.",
        "category": "feishu",
        "icon": "📄",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "document_token": {"type": "string", "description": "Feishu document token (from document URL)"},
                "max_chars": {"type": "integer", "description": "Max characters to return (default 6000, max 20000)"},
            },
            "required": ["document_token"],
        },
        "config": {},
        "config_schema": {},
    },
]
