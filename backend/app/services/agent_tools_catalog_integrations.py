"""MCP, email, pages, skills, and AgentBay tool schemas."""


AGENT_TOOL_INTEGRATIONS = [
    {
        "type": "function",
        "function": {
            "name": "import_mcp_server",
            "description": "Import an MCP server so its tools become available to you. Provide ONE of: (1) mcp_config — a standard `mcpServers` JSON config, which works for both HTTP MCP servers (entry has a `url`) and stdio/npx MCP servers (entry has a `command`, e.g. `npx -y <package>`); stdio servers are hosted and started automatically in the sandbox under your own workspace. (2) mcp_url — the full http/https endpoint of a single HTTP MCP server. (3) server_id — a Smithery registry ID (use discover_resources first to find it). If previously imported tools stopped working (e.g. OAuth expired), set reauthorize=true. IMPORTANT: newly imported tools only enter your available-tools list on your NEXT turn — they are NOT callable in the same turn you import them. After a successful import, end your turn and tell the user the tools are ready; have them ask you to use the tools in their next message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mcp_config": {
                        "type": "object",
                        "description": 'Standard MCP config (object or JSON string). HTTP form: {"mcpServers":{"<name>":{"url":"https://...","headers":{...}}}}. stdio/npx form: {"mcpServers":{"<name>":{"command":"npx","args":["-y","<package>"],"env":{"<KEY>":"<value>"}}}}.',
                    },
                    "mcp_url": {
                        "type": "string",
                        "description": "Full http/https endpoint of a single HTTP MCP server (shortcut for the HTTP form of mcp_config).",
                    },
                    "server_id": {
                        "type": "string",
                        "description": "Smithery server ID, e.g. '@anthropic/brave-search' or '@anthropic/fetch' (advanced — only for the Smithery registry path).",
                    },
                    "config": {
                        "type": "object",
                        "description": "Optional server configuration for the Smithery path (e.g. API keys required by the server)",
                    },
                    "reauthorize": {
                        "type": "boolean",
                        "description": "Set to true to force re-authorization of existing tools (e.g. when OAuth token has expired)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_installed_mcp_servers",
            "description": "List your installed MCP servers that still have platform-permitted tools. tool_count counts platform-visible installations; enabled_tool_count counts those enabled by your current Agent or scene settings. Zero enabled tools means installed but unavailable, not a working connection. Scene-only tools without an installation are defined separately. removable only permits uninstalling your own bindings; it does not grant refresh permission. Server IDs identify installations, not credentials or authentication health.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_mcp_server",
            "description": "Refresh one MCP server installed exclusively by you, using your Agent configuration. Use the exact mcp_server_id returned by list_installed_mcp_servers. Inherited enterprise or shared MCP servers cannot be refreshed here and must use the administrator global refresh. Existing tool enablement and configuration are preserved; newly discovered tools become available on your next turn. Refresh discovers tool definitions; it does not repair CLI credentials or bypass platform disablement.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mcp_server_id": {
                        "type": "string",
                        "description": "Exact MCP server UUID. Names and fuzzy identifiers are not accepted.",
                    }
                },
                "required": ["mcp_server_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "uninstall_mcp_server",
            "description": "Uninstall one MCP server from yourself using the exact mcp_server_id returned by list_installed_mcp_servers or import_mcp_server. This only removes your self-installed binding; enterprise/shared MCP definitions and other agents are never affected. The removal is effective immediately.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mcp_server_id": {
                        "type": "string",
                        "description": "Exact MCP server UUID. Names and fuzzy identifiers are not accepted.",
                    }
                },
                "required": ["mcp_server_id"],
            },
        },
    },
    # ─── Email Tools ────────────────────────
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an email to one or more recipients. Supports subject, body text, CC, and file attachments from workspace. Requires email configuration in tool settings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient email address(es), comma-separated for multiple",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject line",
                    },
                    "body": {
                        "type": "string",
                        "description": "Email body text",
                    },
                    "cc": {
                        "type": "string",
                        "description": "CC recipients, comma-separated (optional)",
                    },
                    "attachments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of workspace-relative file paths to attach (optional)",
                    },
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_emails",
            "description": "Read emails from your inbox. Can limit the number returned and search by criteria (e.g. FROM, SUBJECT, SINCE date). Requires email configuration in tool settings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max number of emails to return (default 10, max 30)",
                    },
                    "search": {
                        "type": "string",
                        "description": "IMAP search criteria, e.g. 'FROM \"john@example.com\"', 'SUBJECT \"meeting\"', 'SINCE 01-Mar-2026'. Default: all emails.",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Mailbox folder, default INBOX",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reply_email",
            "description": "Reply to an email by its Message-ID. Maintains the email thread with proper In-Reply-To headers. Requires email configuration in tool settings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Message-ID of the email to reply to (from read_emails output)",
                    },
                    "body": {
                        "type": "string",
                        "description": "Reply body text",
                    },
                },
                "required": ["message_id", "body"],
            },
        },
    },
    # --- Pages: public HTML hosting ---
    {
        "type": "function",
        "function": {
            "name": "publish_page",
            "description": (
                "Publish an HTML file from this Agent's workspace. New pages require login by default: "
                "omit access_mode for authenticated access, use public only when the user explicitly wants "
                "anyone with the link to open it, and use restricted for specified users. Before restricted "
                "publishing, use search_page_viewers to obtain user IDs and pass them in allowed_user_ids. "
                "Republishing the same path updates the existing page at the same URL and preserves its current "
                "permissions unless access_mode is explicitly supplied. Non-public pages receive the platform "
                "watermark automatically. Automatic SSO is opt-in only: append auto_login=1 to the Page URL only "
                "when the user explicitly requests automatic login; optionally add sso=<provider_type>, otherwise "
                "the first enabled SSO provider is used. The result includes the publication actor and exact publication time. "
                "Always give the user both the Page URL and Management URL exactly as returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path in workspace, e.g. 'workspace/output.html'",
                    },
                    "access_mode": {
                        "type": "string",
                        "enum": ["public", "authenticated", "restricted"],
                        "default": "authenticated",
                        "description": (
                            "Optional. Defaults to authenticated for a new page. public = anyone with the link, "
                            "authenticated = any logged-in user in the page's company, restricted = only the publisher, "
                            "Agent creator, and users listed in allowed_user_ids. Omit when republishing to preserve existing permissions."
                        ),
                    },
                    "allowed_user_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Required only for restricted access. Get IDs with search_page_viewers; use [] when nobody else should be allowed.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_page_viewers",
            "description": "Search active users in this Agent's company by display name or email. Returns user IDs for allowed_user_ids. Call this before publish_page or update_published_page_access when the user requests restricted access for named people.",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_published_page_access",
            "description": "Change an existing published page. Company and platform administrators may change any page in their current company; other users may only change a page published by this Agent that they manage. Use its short_id from publish_page or list_published_pages. For restricted access, first call search_page_viewers and pass the complete replacement allowed_user_ids list; [] allows only the publisher and Agent creator. For public or authenticated access, pass allowed_user_ids as [].",
            "parameters": {
                "type": "object",
                "properties": {
                    "short_id": {"type": "string"},
                    "access_mode": {"type": "string", "enum": ["public", "authenticated", "restricted"]},
                    "allowed_user_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["short_id", "access_mode", "allowed_user_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_published_pages",
            "description": "List pages published by this Agent, including Page URL, Management URL, creator and creation time, most recent publisher and publication time, access mode, views, and pending access-request count. Historical pages explicitly report when their most recent publication actor or time was not recorded. Use list_page_access_requests when request details or statuses are needed.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_page_access_requests",
            "description": (
                "List real user-initiated access requests for one page published by this Agent. "
                "Use the short_id returned by publish_page or list_published_pages. Returns requester identity, "
                "pending/approved/rejected status, request time, resolution time, totals, and the Management URL. "
                "This tool only reads request status; it does not approve, reject, or change page permissions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "short_id": {"type": "string", "description": "Published page short ID, without the /p/ prefix."},
                    "status": {
                        "type": "string",
                        "enum": ["all", "pending", "approved", "rejected"],
                        "default": "all",
                        "description": "Optional status filter. Defaults to all request statuses.",
                    },
                    "page": {"type": "integer", "minimum": 1, "default": 1, "description": "Result page number."},
                    "page_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "default": 20,
                        "description": "Requests per page.",
                    },
                },
                "required": ["short_id"],
            },
        },
    },
    # --- Skill Management ---
    {
        "type": "function",
        "function": {
            "name": "search_clawhub",
            "description": "Search the ClawHub skill registry for skills matching a query. Returns a list of available skills with name, description, and last updated date. Use this to help users find skills to install.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'research', 'code review', 'market analysis'",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_skill",
            "description": "Install a skill into this agent's workspace. Accepts either a ClawHub skill slug (e.g. 'market-research') or a GitHub URL (e.g. 'https://github.com/user/repo'). The skill files will be downloaded and saved to skills/<name>/ in your workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "ClawHub skill slug (e.g. 'market-research') or GitHub URL (e.g. 'https://github.com/user/repo')",
                    },
                },
                "required": ["source"],
            },
        },
    },
    # ── AgentBay Tools ────────────────────────────────────────────
    # First-party Skill Market tools
    {
        "type": "function",
        "function": {
            "name": "search_skill_market",
            "description": (
                "Search the first-party Skill market. When installed Skills do not clearly cover a specialized "
                "request, use this tool automatically before improvising. Returns visible company and public Skills "
                "with IDs, versions, publishers, and unique Agent install counts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Short capability query, for example 'Excel sales analysis'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_skill_from_market",
            "description": (
                "Install one market Skill into this Agent by Skill ID. This changes the shared Agent workspace. "
                "The human speaking in the current conversation must have Agent manage access; no additional "
                "administrator approval is required."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string", "description": "Skill UUID returned by search_skill_market."},
                },
                "required": ["skill_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_skill_to_market",
            "description": (
                "Publish a Skill folder from this Agent to the Skill market. The path must be skills/<folder>. "
                "The platform always requires L3 approval before publication."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Agent path such as skills/sales-analysis."},
                    "name": {"type": "string", "description": "Market display name."},
                    "description": {"type": "string", "description": "Short capability description."},
                    "category": {"type": "string", "description": "Simple market category.", "default": "general"},
                    "visibility": {
                        "type": "string",
                        "enum": ["tenant", "public"],
                        "default": "tenant",
                        "description": "Company-only or platform-public visibility.",
                    },
                },
                "required": ["path", "name", "description", "visibility"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "withdraw_skill_from_market",
            "description": (
                "Take one market Skill copy previously published by this Agent offline. The source Skill and "
                "existing installs remain available, and publishing the source folder again relists a fresh copy. "
                "The platform always requires L3 approval before taking it offline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string", "description": "Skill UUID returned after publication or search."},
                },
                "required": ["skill_id"],
            },
        },
    },
    # AgentBay tools
    {
        "type": "function",
        "function": {
            "name": "agentbay_browser_navigate",
            "description": "使用 AgentBay 浏览器环境访问指定 URL。访问后会自动截图以便你观察当前页面状态。Tip: after navigating, use browser_observe to identify elements, then browser_type/browser_click to interact. IMPORTANT: Do NOT call navigate again after clicking or typing — that will refresh the page and lose all your progress. Use agentbay_browser_screenshot instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要访问的网址，如 https://example.com"},
                    "wait_for": {"type": "string", "description": "等待特定元素出现的选择器（可选）"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agentbay_browser_screenshot",
            "description": "Take a screenshot of the CURRENT browser page without navigating anywhere. Use this after clicking, typing, or submitting a form to verify the result — it preserves the current page state. Never call browser_navigate just to take a screenshot.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agentbay_browser_click",
            "description": "在 AgentBay 浏览器中点击指定元素。selector 可以是 CSS 选择器（如 #btn）或自然语言描述（如 'the Send button' 或 '发送验证码按钮'）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector (e.g. #button) or natural language description of the element (e.g. 'the blue Submit button')",
                    },
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agentbay_browser_type",
            "description": "在 AgentBay 浏览器的输入框中输入文本。selector 可以是 CSS 选择器或自然语言描述（如 'phone number input' 或 '手机号输入框'）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "CSS selector or natural language description of the input field (e.g. 'the phone number input' or 'input[type=tel]')",
                    },
                    "text": {"type": "string", "description": "要输入的文本"},
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agentbay_browser_login",
            "description": "Use AgentBay's AI-driven login skill to automate complex login flows (CAPTCHAs, OTP, multi-step auth). Requires a login_config JSON with AgentBay skill credentials. Navigate to the login page and execute the login skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The login page URL to navigate to"},
                    "login_config": {
                        "type": "string",
                        "description": 'JSON string with login config, e.g. \'{"api_key": "xxx", "skill_id": "yyy"}\'',
                    },
                },
                "required": ["url", "login_config"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agentbay_code_execute",
            "description": "在 AgentBay 代码空间中执行代码。支持 Python、Bash、Node.js。需要先配置 AgentBay 通道。",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {"type": "string", "enum": ["python", "bash", "node"], "description": "编程语言"},
                    "code": {"type": "string", "description": "要执行的代码"},
                    "timeout": {"type": "integer", "description": "超时时间（秒，默认 30）", "default": 30},
                },
                "required": ["language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agentbay_code_write_file",
            "description": "[ENV: Code Sandbox] Write a text file inside the AgentBay Code Sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "remote_path": {
                        "type": "string",
                        "description": "Absolute path inside the code sandbox, e.g. /home/wuying/main.py",
                    },
                    "content": {"type": "string", "description": "File content to write."},
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "Write mode. Default: overwrite.",
                        "default": "overwrite",
                    },
                },
                "required": ["remote_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agentbay_code_read_file",
            "description": "[ENV: Code Sandbox] Read a text file from the AgentBay Code Sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "remote_path": {
                        "type": "string",
                        "description": "Absolute path inside the code sandbox, e.g. /home/wuying/main.py",
                    },
                },
                "required": ["remote_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agentbay_code_edit_file",
            "description": "[ENV: Code Sandbox] Edit a text file inside the AgentBay Code Sandbox by replacing exact text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "remote_path": {
                        "type": "string",
                        "description": "Absolute path inside the code sandbox, e.g. /home/wuying/main.py",
                    },
                    "edits": {
                        "type": "array",
                        "description": "List of exact text replacements.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "oldText": {"type": "string", "description": "Exact text to replace."},
                                "newText": {"type": "string", "description": "Replacement text."},
                            },
                            "required": ["oldText", "newText"],
                        },
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview changes without applying them. Default: false.",
                        "default": False,
                    },
                },
                "required": ["remote_path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agentbay_file_transfer",
            "description": (
                "Transfer a file between any two endpoints: the agent workspace, "
                "the AgentBay browser environment, the cloud desktop (computer), or the code sandbox.\n\n"
                "VERIFIED PATH CONVENTIONS (all Linux environments run as user 'wuying', HOME=/home/wuying/):\n"
                "- code env:     use /home/wuying/<filename>  (working directory, e.g. /home/wuying/data.csv)\n"
                "- browser env:  use /home/wuying/下载/<filename>  (download folder, e.g. /home/wuying/下载/file.pdf)\n"
                "- computer env: use /home/wuying/桌面/<filename>  (Desktop, e.g. /home/wuying/桌面/report.xlsx)\n"
                "- workspace:    use relative path, e.g. 'workspace/data.csv'\n\n"
                "Transfer directions:\n"
                "- workspace -> env: upload a workspace file into a cloud environment\n"
                "- env -> workspace: download a file from a cloud environment into the workspace\n"
                "- env A -> env B:   transfer between environments (transparent backend temp, no workspace involvement)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_type": {
                        "type": "string",
                        "enum": ["workspace", "browser", "computer", "code"],
                        "description": "Source endpoint: 'workspace' for agent workspace, or the AgentBay environment name.",
                    },
                    "from_path": {
                        "type": "string",
                        "description": (
                            "Source path. Relative if workspace (e.g. 'workspace/data.csv'). "
                            "Absolute if env: code → /home/wuying/file, "
                            "browser → /home/wuying/下载/file, computer → /home/wuying/桌面/file."
                        ),
                    },
                    "to_type": {
                        "type": "string",
                        "enum": ["workspace", "browser", "computer", "code"],
                        "description": "Destination endpoint: 'workspace' for agent workspace, or the AgentBay environment name.",
                    },
                    "to_path": {
                        "type": "string",
                        "description": (
                            "Destination path. Relative if workspace (e.g. 'workspace/output.csv'). "
                            "Absolute if env: code → /home/wuying/file, "
                            "browser → /home/wuying/下载/file, computer → /home/wuying/桌面/file."
                        ),
                    },
                },
                "required": ["from_type", "from_path", "to_type", "to_path"],
            },
        },
    },
]

__all__ = ["AGENT_TOOL_INTEGRATIONS"]
