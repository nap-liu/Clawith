"""Builtin tool seed catalog part 6."""

from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_SEED
from app.services.media_tool_contract import SEND_MEDIA_TOOL_SEED


BUILTIN_TOOLS_PART_6 = [
    {
        "name": "feishu_doc_create",
        "display_name": "Feishu Doc Create",
        "description": "Create a new Feishu document with a given title. Returns the new document token and URL.",
        "category": "feishu",
        "icon": "📝",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Document title"},
                "folder_token": {"type": "string", "description": "Optional: parent folder token"},
            },
            "required": ["title"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_doc_append",
        "display_name": "Feishu Doc Append",
        "description": "Append text content to an existing Feishu document as new paragraphs at the end.",
        "category": "feishu",
        "icon": "📎",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "document_token": {"type": "string", "description": "Feishu document token"},
                "content": {"type": "string", "description": "Text content to append"},
            },
            "required": ["document_token", "content"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_drive_share",
        "display_name": "Feishu Drive Share",
        "description": "Manage collaborators for any Feishu Drive file (docx, bitable, sheet, etc.). Add, remove, or list collaborators with view/edit/full_access permissions.",
        "category": "feishu",
        "icon": "🔗",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "document_token": {"type": "string", "description": "File token (from URL or previous tool output)"},
                "doc_type": {"type": "string", "enum": ["docx", "bitable", "sheet", "doc", "folder", "mindnote", "slides"], "description": "File type. Default: 'docx'"},
                "action": {"type": "string", "enum": ["add", "remove", "list"], "description": "'add' to grant, 'remove' to revoke, 'list' to view"},
                "user_ids": {"type": "array", "items": {"type": "string"}, "description": "Canonical platform user_ids to add/remove; Feishu IDs are internal."},
                "permission": {"type": "string", "enum": ["view", "edit", "full_access"], "description": "Permission level. Default: 'edit'"},
            },
            "required": ["document_token", "action"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_drive_delete",
        "display_name": "Feishu Drive Delete",
        "description": "Delete a file or folder from Feishu Drive. The file is moved to the recycle bin. Supports all file types: docx, bitable, sheet, folder, etc.",
        "category": "feishu",
        "icon": "🗑️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_token": {"type": "string", "description": "Token of the file to delete"},
                "file_type": {"type": "string", "enum": ["file", "docx", "bitable", "folder", "doc", "sheet", "mindnote", "shortcut", "slides"], "description": "Type of the file to delete"},
            },
            "required": ["file_token", "file_type"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_calendar_list",
        "display_name": "Feishu Calendar List",
        "description": "List Feishu calendar events and optionally query one related user by canonical platform user_id.",
        "category": "feishu",
        "icon": "📅",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "start_time": {"type": "string", "description": "Query start in ISO 8601. A value without an offset uses the Agent effective timezone; an offset-bearing value keeps its absolute instant. Defaults to now."},
                "end_time": {"type": "string", "description": "Query end in ISO 8601. A value without an offset uses the Agent effective timezone; an offset-bearing value keeps its absolute instant. Defaults to 7 days from now."},
                "user_id": {"type": "string", "description": "要查询 freebusy 的 canonical platform user_id。不填则自动使用当前飞书对话发送者。"},
                "max_results": {"type": "integer", "description": "Max events to return (default 20)"},
            },
            "required": [],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_calendar_create",
        "display_name": "Feishu Calendar Create",
        "description": "Create a Feishu calendar event. Invite colleagues only by canonical platform user_id.",
        "category": "feishu",
        "icon": "📅",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start_time": {"type": "string", "description": "Event start in ISO 8601. A value without an offset uses timezone (or the Agent effective timezone); an offset-bearing value keeps its absolute instant."},
                "end_time": {"type": "string", "description": "Event end in ISO 8601. A value without an offset uses timezone (or the Agent effective timezone); an offset-bearing value keeps its absolute instant."},
                "description": {"type": "string", "description": "Event description or agenda"},
                "attendee_user_ids": {"type": "array", "items": {"type": "string"}, "description": "Canonical platform user_ids to invite. Use feishu_user_search to discover exact IDs."},
                "location": {"type": "string", "description": "Event location or meeting room"},
                "timezone": {"type": "string", "description": "Optional IANA timezone for event display and naive time inputs. Defaults to the Agent effective timezone (Agent override, then tenant, then UTC)."},
            },
            "required": ["summary", "start_time", "end_time"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_calendar_update",
        "display_name": "Feishu Calendar Update",
        "description": "Update an existing Feishu calendar event. Provide only the fields you want to change.",
        "category": "feishu",
        "icon": "📅",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Event ID from feishu_calendar_list"},
                "summary": {"type": "string", "description": "New title"},
                "description": {"type": "string", "description": "New description"},
                "start_time": {"type": "string", "description": "New start in ISO 8601. A value without an offset uses timezone (or the Agent effective timezone); an offset-bearing value keeps its absolute instant."},
                "end_time": {"type": "string", "description": "New end in ISO 8601. A value without an offset uses timezone (or the Agent effective timezone); an offset-bearing value keeps its absolute instant."},
                "location": {"type": "string", "description": "New location"},
                "timezone": {"type": "string", "description": "Optional IANA timezone for event display and naive time inputs. Defaults to the Agent effective timezone (Agent override, then tenant, then UTC)."},
            },
            "required": ["event_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_calendar_delete",
        "display_name": "Feishu Calendar Delete",
        "description": "Delete (cancel) a Feishu calendar event.",
        "category": "feishu",
        "icon": "🗑️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Event ID to delete"},
            },
            "required": ["event_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_approval_create",
        "display_name": "Feishu Approval Create",
        "description": "发起一个飞书审批流实例。你需要知道审批定义的 approval_code 和表单对应字段的内容。",
        "category": "feishu",
        "icon": "📝",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "approval_code": {"type": "string", "description": "审批定义的唯一代码 (approval_code)"},
                "user_id": {"type": "string", "description": "发起人的 canonical platform user_id，可通过 feishu_user_search 获取。"},
                "form_data": {"type": "string", "description": "表单内容的 JSON 字符串，例如 '[{\"id\":\"widget1\",\"type\":\"input\",\"value\":\"这是内容\"}]'"},
            },
            "required": ["approval_code", "user_id", "form_data"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_approval_query",
        "display_name": "Feishu Approval Query",
        "description": "查询指定的飞书审批实例列表。可以支持按状态查询（PENDING, APPROVED, REJECTED, CANCELED, DELETED）。",
        "category": "feishu",
        "icon": "📋",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "approval_code": {"type": "string", "description": "审批定义的唯一代码 (approval_code)"},
                "status": {"type": "string", "description": "可选过滤状态：PENDING, APPROVED, REJECTED, CANCELED, DELETED"},
            },
            "required": ["approval_code"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "feishu_approval_get",
        "display_name": "Feishu Approval Get",
        "description": "获取指定飞书审批实例的详细信息与当前审批状态。",
        "category": "feishu",
        "icon": "📊",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "审批实例的 instance_id"},
            },
            "required": ["instance_id"],
        },
        "config": {},
        "config_schema": {},
    },
    # --- Pages: public HTML hosting ---
    {
        "name": "publish_page",
        "display_name": "Publish Page",
        "description": (
            "Publish an HTML file from this Agent's workspace. New pages require login by default: omit access_mode "
            "for authenticated access, use public only when the user explicitly wants anyone with the link to open it, "
            "and use restricted for specified users. Before restricted publishing, use search_page_viewers to obtain "
            "user IDs and pass them in allowed_user_ids. Republishing the same path updates the existing page at the "
            "same URL and preserves its current permissions unless access_mode is explicitly supplied. Non-public pages "
            "receive the platform watermark automatically. Automatic SSO is opt-in only: append auto_login=1 to the Page URL only "
            "when the user explicitly requests automatic login; optionally add sso=<provider_type>, otherwise "
            "the first enabled SSO provider is used. The result includes the publication actor and exact publication time. "
            "Always give the user both the Page URL and Management URL exactly as returned."
        ),
        "category": "pages",
        "icon": "🌐",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path in workspace, e.g. 'workspace/output.html'"},
                "access_mode": {
                    "type": "string", "enum": ["public", "authenticated", "restricted"], "default": "authenticated",
                    "description": (
                        "Optional. Defaults to authenticated for a new page. public = anyone with the link, "
                        "authenticated = any logged-in user in the page's company, restricted = only the publisher, "
                        "Agent creator, and users listed in allowed_user_ids. Omit when republishing to preserve existing permissions."
                    ),
                },
                "allowed_user_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Required only for restricted access. Get IDs with search_page_viewers; use [] when nobody else should be allowed.",
                },
            },
            "required": ["path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "search_page_viewers", "display_name": "Search Page Viewers",
        "description": "Search active users in this Agent's company by display name or email. Returns user IDs for allowed_user_ids. Call this before publish_page or update_published_page_access when the user requests restricted access for named people.",
        "category": "pages", "icon": "🔎", "is_default": True,
        "parameters_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        "config": {}, "config_schema": {},
    },
    {
        "name": "update_published_page_access", "display_name": "Update Page Access",
        "description": "Change an existing published page. Company and platform administrators may change any page in their current company; other users may only change a page published by this Agent that they manage. Use its short_id from publish_page or list_published_pages. For restricted access, first call search_page_viewers and pass the complete replacement allowed_user_ids list; [] allows only the publisher and Agent creator. For public or authenticated access, pass allowed_user_ids as [].",
        "category": "pages", "icon": "🔐", "is_default": True,
        "parameters_schema": {"type": "object", "properties": {
            "short_id": {"type": "string"},
            "access_mode": {"type": "string", "enum": ["public", "authenticated", "restricted"]},
            "allowed_user_ids": {"type": "array", "items": {"type": "string"}},
        }, "required": ["short_id", "access_mode", "allowed_user_ids"]},
        "config": {}, "config_schema": {},
    },
    {
        "name": "list_published_pages",
        "display_name": "List Published Pages",
        "description": "List pages published by this Agent, including Page URL, Management URL, creator and creation time, most recent publisher and publication time, access mode, views, and pending access-request count. Historical pages explicitly report when their most recent publication actor or time was not recorded. Use list_page_access_requests when request details or statuses are needed.",
        "category": "pages",
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
        "name": "list_page_access_requests",
        "display_name": "List Page Access Requests",
        "description": (
            "List real user-initiated access requests for one page published by this Agent. Use the short_id returned by "
            "publish_page or list_published_pages. Returns requester identity, pending/approved/rejected status, request "
            "time, resolution time, totals, and the Management URL. This tool only reads request status; it does not "
            "approve, reject, or change page permissions."
        ),
        "category": "pages",
        "icon": "🛂",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "short_id": {"type": "string", "description": "Published page short ID, without the /p/ prefix."},
                "status": {
                    "type": "string", "enum": ["all", "pending", "approved", "rejected"], "default": "all",
                    "description": "Optional status filter. Defaults to all request statuses.",
                },
                "page": {"type": "integer", "minimum": 1, "default": 1, "description": "Result page number."},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20, "description": "Requests per page."},
            },
            "required": ["short_id"],
        },
        "config": {},
        "config_schema": {},
    },
    # --- Skill Management ---
    {
        "name": "search_clawhub",
        "display_name": "Search ClawHub",
        "description": "Search the ClawHub skill registry for skills matching a query. Returns a list of available skills with name, description, and last updated date.",
        "category": "discovery",
        "icon": "🔎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query, e.g. 'research', 'code review', 'market analysis'"},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "install_skill",
        "display_name": "Install Skill",
        "description": "Install a skill into this agent's workspace. Accepts a ClawHub slug (e.g. 'market-research') or a GitHub URL.",
        "category": "discovery",
        "icon": "📥",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "ClawHub skill slug (e.g. 'market-research') or GitHub URL"},
            },
            "required": ["source"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "search_skill_market",
        "display_name": "Search Skill Market",
        "description": (
            "Search the first-party Skill market. Use automatically when installed Skills do not clearly cover "
            "a specialized request. Returns visible company and public Skills with IDs, versions, publishers, and "
            "unique Agent install counts."
        ),
        "category": "discovery",
        "icon": "🔎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Short capability query."},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "install_skill_from_market",
        "display_name": "Install Market Skill",
        "description": (
            "Install a market Skill into this Agent by Skill ID. The human speaking in the current conversation "
            "must have Agent manage access; no additional administrator approval is required."
        ),
        "category": "discovery",
        "icon": "📥",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string", "description": "Skill UUID returned by search_skill_market."},
            },
            "required": ["skill_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "publish_skill_to_market",
        "display_name": "Publish Skill to Market",
        "description": (
            "Publish skills/<folder> from this Agent to the company or public Skill market. The platform creates an "
            "L3 approval before publication, and the approving user must have Agent manage access."
        ),
        "category": "discovery",
        "icon": "📤",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Agent path such as skills/sales-analysis."},
                "name": {"type": "string", "description": "Market display name."},
                "description": {"type": "string", "description": "Short capability description."},
                "category": {"type": "string", "default": "general"},
                "visibility": {"type": "string", "enum": ["tenant", "public"], "default": "tenant"},
            },
            "required": ["path", "name", "description", "visibility"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "withdraw_skill_from_market",
        "display_name": "Withdraw Skill from Market",
        "description": (
            "Withdraw a Skill previously published by this Agent. Existing installs remain available. The platform "
            "creates an L3 approval before withdrawal, and the approving user must have Agent manage access."
        ),
        "category": "discovery",
        "icon": "📤",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "skill_id": {"type": "string", "description": "Skill UUID returned after publication or search."},
            },
            "required": ["skill_id"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "sql_execute",
        "display_name": "SQL Execute",
        "description": (
            "Connect to any SQL database and execute queries or statements. "
            "Supports MySQL/StarRocks, PostgreSQL, SQLite via a standard connection URI. "
            "结果有硬上限:默认返回最多 5000 行(可用 max_rows 调到 50000),超出会被截断并提示。"
            "优先用聚合(COUNT/SUM/AVG/GROUP BY)在 SQL 内完成计算,不要拉取大量原始行在外部统计;"
            "需要明细时加 WHERE/LIMIT 缩小范围。"
        ),
        "category": "database",
        "icon": "🗄️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "connection_string": {
                    "type": "string",
                    "description": "Database connection URI, e.g. mysql://user:pass@host:9030/db (StarRocks FE port), postgresql://user:pass@host:5432/db, sqlite:///path/to/file.db",
                },
                "sql": {
                    "type": "string",
                    "description": "SQL statement to execute (SELECT, INSERT, UPDATE, DELETE, DDL, etc.)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Query timeout in seconds (default 30, max 120)",
                },
                "max_rows": {
                    "type": "integer",
                    "description": "Max detail rows to return (default 5000, hard cap 50000). 超出会截断并提示改用聚合。",
                },
            },
            "required": ["connection_string", "sql"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "update_kr_content",
        "display_name": "Update KR Content",
        "description": (
            "Update the content fields of one of YOUR OWN Key Results. "
            "Call get_my_okr first to obtain the kr_id, then change title, target_value, unit, "
            "focus_ref, or status as needed. This does not record a progress update."
        ),
        "category": "okr",
        "icon": "✏️",
        "is_default": True,
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
                    "description": "Optional new focus reference.",
                },
                "status": {
                    "type": "string",
                    "enum": ["on_track", "at_risk", "behind", "completed"],
                    "description": "Optional explicit status value.",
                },
            },
            "required": ["kr_id"],
        },
        "config": {},
        "config_schema": {},
    },
]
