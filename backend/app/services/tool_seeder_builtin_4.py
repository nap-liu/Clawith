"""Builtin tool seed catalog part 4."""

from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_SEED
from app.services.media_tool_contract import SEND_MEDIA_TOOL_SEED


BUILTIN_TOOLS_PART_4 = [
    {
        "name": "upload_image",
        "display_name": "Upload Image",
        "description": "Upload images from the workspace or a URL to ImageKit CDN and get a public URL. Useful for sharing images externally or embedding them in reports.",
        "category": "code",
        "icon": "🖼️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Workspace-relative path to image file"},
                "url": {"type": "string", "description": "Public URL of image to upload"},
                "file_name": {"type": "string", "description": "Custom filename (optional)"},
                "folder": {"type": "string", "description": "Optional CDN folder path"},
            },
        },
        "config": {"private_key": "", "url_endpoint": ""},
        "config_schema": {
            "fields": [
                {
                    "key": "private_key",
                    "label": "ImageKit Private Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Your ImageKit private API key",
                },
                {
                    "key": "url_endpoint",
                    "label": "ImageKit URL Endpoint",
                    "type": "text",
                    "default": "",
                    "placeholder": "https://ik.imagekit.io/your_imagekit_id",
                },
            ]
        },
    },
    {
        "name": "generate_image_siliconflow",
        "display_name": "Generate Image (SiliconFlow)",
        "description": "Generate an image via SiliconFlow FLUX models. China-friendly and fast.",
        "category": "media",
        "icon": "🎨",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed image description."},
                "size": {"type": "string", "description": "Image size (e.g. 1024x1024, 1024x768). Default 1024x1024."},
                "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
            },
            "required": ["prompt"],
        },
        "config": {
            "model": "",
            "api_key": "",
            "base_url": "",
        },
        "config_schema": {
            "fields": [
                {
                    "key": "model",
                    "label": "Model",
                    "type": "text",
                    "default": "",
                    "placeholder": "e.g. black-forest-labs/FLUX.1-schnell",
                },
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "SiliconFlow API Key",
                },
                {
                    "key": "base_url",
                    "label": "Base URL (optional)",
                    "type": "text",
                    "default": "",
                    "placeholder": "Default: https://api.siliconflow.cn/v1",
                },
            ]
        },
    },
    {
        "name": "generate_image_openai",
        "display_name": "Generate Image (OpenAI)",
        "description": "Generate an image via OpenAI DALL-E models.",
        "category": "media",
        "icon": "🎨",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed image description."},
                "size": {"type": "string", "description": "Image size (e.g. 1024x1024). Default 1024x1024."},
                "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
            },
            "required": ["prompt"],
        },
        "config": {
            "model": "",
            "api_key": "",
            "base_url": "",
        },
        "config_schema": {
            "fields": [
                {
                    "key": "model",
                    "label": "Model",
                    "type": "text",
                    "default": "",
                    "placeholder": "e.g. dall-e-3 or dall-e-2",
                },
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "OpenAI API Key",
                },
                {
                    "key": "base_url",
                    "label": "Base URL (optional)",
                    "type": "text",
                    "default": "",
                    "placeholder": "Default: https://api.openai.com/v1",
                },
            ]
        },
    },
    {
        "name": "generate_image_google",
        "display_name": "Generate Image (Google/Vertex)",
        "description": "Generate an image via Google Gemini Image (Nano Banana) or Vertex AI.",
        "category": "media",
        "icon": "🎨",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed image description."},
                "size": {"type": "string", "description": "Image size (e.g. 1024x1024). Default 1024x1024."},
                "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
            },
            "required": ["prompt"],
        },
        "config": {
            "model": "",
            "api_key": "",
            "base_url": "",
        },
        "config_schema": {
            "fields": [
                {
                    "key": "model",
                    "label": "Model",
                    "type": "text",
                    "default": "",
                    "placeholder": "e.g. gemini-2.5-flash-image",
                },
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Google AI Studio or Vertex API Key",
                },
                {
                    "key": "base_url",
                    "label": "Base URL (optional)",
                    "type": "text",
                    "default": "",
                    "placeholder": "Can be Vertex API URL: https://aiplatform.googleapis.com/...",
                },
            ]
        },
    },
    {
        "name": "generate_image_custom",
        "display_name": "Generate Image (Custom API)",
        "description": "Generate an image through a custom OpenAI-compatible or gateway API. Configure the request body template and response image path for providers such as TokenRouter or OpenRouter.",
        "category": "media",
        "icon": "🎨",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed image description."},
                "size": {"type": "string", "description": "Image size (e.g. 1024x1024). Default 1024x1024."},
                "save_path": {"type": "string", "description": "Save path in workspace. Default: auto."},
            },
            "required": ["prompt"],
        },
        "config": {
            "api_key": "",
            "base_url": "",
            "endpoint_path": "/chat/completions",
            "model": "",
            "request_body_template_json": "{\n  \"model\": \"{model}\",\n  \"messages\": [\n    {\n      \"role\": \"user\",\n      \"content\": \"{prompt}\"\n    }\n  ],\n  \"modalities\": [\"image\", \"text\"],\n  \"stream\": false\n}",
            "response_image_path": "choices.0.message.images.0.image_url.url",
            "extra_headers_json": "",
            "timeout_seconds": 120,
        },
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "API key for your image generation gateway",
                },
                {
                    "key": "model",
                    "label": "Model",
                    "type": "text",
                    "default": "",
                    "placeholder": "e.g. google/gemini-2.5-flash-image",
                },
                {
                    "key": "base_url",
                    "label": "Base URL",
                    "type": "text",
                    "default": "",
                    "placeholder": "e.g. https://api.tokenrouter.com/v1 or https://openrouter.ai/api/v1",
                },
                {
                    "key": "endpoint_path",
                    "label": "Endpoint Path",
                    "type": "text",
                    "default": "/chat/completions",
                    "placeholder": "/chat/completions",
                    "advanced": True,
                },
                {
                    "key": "request_body_template_json",
                    "label": "Request Body Template JSON",
                    "type": "textarea",
                    "default": "{\n  \"model\": \"{model}\",\n  \"messages\": [\n    {\n      \"role\": \"user\",\n      \"content\": \"{prompt}\"\n    }\n  ],\n  \"modalities\": [\"image\", \"text\"],\n  \"stream\": false\n}",
                    "placeholder": "{\n  \"model\": \"{model}\",\n  \"messages\": [{\"role\": \"user\", \"content\": \"{prompt}\"}],\n  \"modalities\": [\"image\", \"text\"],\n  \"stream\": false\n}",
                    "advanced": True,
                },
                {
                    "key": "response_image_path",
                    "label": "Response Image Path",
                    "type": "text",
                    "default": "choices.0.message.images.0.image_url.url",
                    "placeholder": "choices.0.message.images.0.image_url.url",
                    "advanced": True,
                },
                {
                    "key": "extra_headers_json",
                    "label": "Extra Headers JSON",
                    "type": "textarea",
                    "default": "",
                    "placeholder": "{\n  \"HTTP-Referer\": \"https://your-app.example\",\n  \"X-Title\": \"Platform App\"\n}",
                    "advanced": True,
                },
                {
                    "key": "timeout_seconds",
                    "label": "Timeout Seconds",
                    "type": "number",
                    "default": 120,
                    "min": 10,
                    "max": 600,
                    "advanced": True,
                },
            ]
        },
    },
    {
        "name": "discover_resources",
        "display_name": "Resource Discovery",
        "description": "Search public MCP registries (Smithery + ModelScope) for tools and capabilities that can extend your abilities. Use this when you encounter a task you cannot handle with your current tools.",
        "category": "discovery",
        "icon": "🔎",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Semantic description of the capability needed, e.g. 'send email', 'query SQL database', 'generate images'"},
                "max_results": {"type": "integer", "description": "Max results to return (default 5, max 10)"},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "smithery_api_key",
                    "label": "Smithery API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get your key at smithery.ai/account/api-keys",
                },
                {
                    "key": "modelscope_api_token",
                    "label": "ModelScope API Token",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get your token at modelscope.cn → Home → Access Tokens",
                },
            ]
        },
    },
    {
        "name": "list_sessions",
        "display_name": "List Sessions",
        "description": "List the conversation sessions you (this agent) take part in — your chats with people, group chats, agent-to-agent (A2A) threads, your own trigger reflections, and permitted Subagent execution sessions. Read-only. Display timestamps use your effective Agent timezone and include its IANA name and UTC offset. raw=true preserves canonical stored timestamps and adds local projections. Results are automatically scoped by who is talking to you: an admin partner can list every user's sessions with you; a regular user only sees their own; in unattended (A2A/trigger/Subagent) turns only your autonomous-side sessions are visible. You can never see another agent's sessions.",
        "category": "discovery",
        "icon": "🗂️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Optional channel filter, e.g. 'web', 'feishu', 'agent' (A2A), 'trigger', or 'subagent'. Omit or 'all' for every permitted channel."},
                "query": {"type": "string", "description": "Optional case-insensitive substring to match against session title / group name."},
                "counterpart": {"type": "string", "description": "Optional conversation-person filter. Matches a P2P person's display name/login/exact user_id, an A2A peer Agent, or a real human sender in a group."},
                "counterpart_match": {"type": "string", "enum": ["exact", "fuzzy"], "description": "How counterpart is matched. Default fuzzy; exact is case-insensitive and also accepts canonical IDs."},
                "is_group": {"type": "boolean", "description": "Optional exact session-kind filter: true for group chats, false for non-group sessions."},
                "group": {"type": "string", "description": "Optional group-chat filter over group name, title, external conversation ID, or exact session UUID."},
                "group_match": {"type": "string", "enum": ["exact", "fuzzy"], "description": "How group is matched. Default fuzzy; exact is case-insensitive and also accepts the exact session UUID."},
                "limit": {"type": "integer", "description": "Max sessions to return (default 20, max 50)."},
                "offset": {"type": "integer", "description": "Pagination offset (default 0)."},
                "since": {"type": "string", "description": "Optional ISO 8601 lower bound on last activity. Values with an offset keep that absolute instant; values without an offset use your effective Agent timezone."},
                "until": {"type": "string", "description": "Optional ISO 8601 upper bound on last activity. Values with an offset keep that absolute instant; values without an offset use your effective Agent timezone."},
                "scene": {"type": "string", "description": "Optional exact scene_key filter. Matches the active session scene or any recorded turn snapshot."},
                "raw": {"type": "boolean", "description": "Return unabridged ChatSession rows as JSON with cursor pagination. Canonical timestamps remain unchanged and *_local projections are added. Default false keeps the summary format."},
                "cursor": {"type": "string", "description": "Opaque next_cursor from a previous raw response. Used only when raw=true."},
            },
            "required": [],
        },
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "read_session_messages",
        "display_name": "Read Session Messages",
        "description": "Read the messages of one session you have access to (use list_sessions / search_sessions to find its id). Read-only. Display timestamps use your effective Agent timezone and include its IANA name and UTC offset. Returns messages in chronological order with per-message and total-size caps; pass the 'before' cursor from the previous reply to page into older messages. Access is enforced: you can only read a session you are a party to and that the current conversation partner is allowed to see; tool-call and internal rows are excluded by default.",
        "category": "discovery",
        "icon": "💬",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The target session id (from list_sessions / search_sessions)."},
                "limit": {"type": "integer", "description": "Max messages to return (default 30, max 100)."},
                "before": {"type": "string", "description": "Pagination cursor from a previous reply to fetch older messages."},
                "include_tool_calls": {"type": "boolean", "description": "Include raw tool-call rows (default false; noisy)."},
            },
            "required": ["session_id"],
        },
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "search_sessions",
        "display_name": "Search Sessions",
        "description": "Full-text search across the messages of the sessions you are allowed to see, including permitted Subagent execution sessions, returning each exact session_id, source channel, matching snippet, and Agent-local timestamp with IANA timezone and UTC offset. Read-only and permission-scoped exactly like list_sessions (admin partner → all users' sessions; regular user → own; A2A/trigger/Subagent → autonomous-side only; never another agent's). Narrow the keyword if there are too many hits.",
        "category": "discovery",
        "icon": "🔍",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword/substring to search message content for (case-insensitive)."},
                "channel": {"type": "string", "description": "Optional channel filter, e.g. 'web', 'agent', 'trigger'. Omit or 'all' for every permitted channel."},
                "limit": {"type": "integer", "description": "Max hits to return (default 20, max 50)."},
                "since": {"type": "string", "description": "Optional ISO 8601 lower bound on message time. Values without an offset use your effective Agent timezone."},
                "until": {"type": "string", "description": "Optional ISO 8601 upper bound on message time. Values without an offset use your effective Agent timezone."},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "import_mcp_server",
        "display_name": "Import MCP Server",
        "description": (
            "Import an MCP server. Direct import is the primary path and requires no third-party account: "
            "pass `mcp_url` (a single http/https endpoint) OR `mcp_config` (the standard `mcpServers` JSON config). "
            "Use `server_id` only when you specifically want to discover via the Smithery registry."
        ),
        "category": "discovery",
        "icon": "📥",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "mcp_url": {
                    "type": "string",
                    "description": "MCP server URL (http/https). Example: 'https://mcp-gw.dingtalk.com/server/<id>?key=<token>'.",
                },
                "mcp_config": {
                    "description": "Standard MCP `mcpServers` JSON config — accepts either an object or a JSON-stringified object. Example: {\"mcpServers\": {\"my\": {\"url\": \"https://...\", \"headers\": {\"Authorization\": \"Bearer xxx\"}}}}.",
                },
                "server_name": {
                    "type": "string",
                    "description": "Optional display name; auto-derived from the URL host when omitted.",
                },
                "api_key": {
                    "type": "string",
                    "description": "Optional bearer token for `mcp_url`. Sent as Authorization header at runtime.",
                },
                "server_id": {
                    "type": "string",
                    "description": "Smithery registry ID, e.g. '@anthropic/brave-search'. Optional — only when importing via Smithery.",
                },
                "config": {
                    "type": "object",
                    "description": "Legacy: server-specific config when going through Smithery. Prefer `mcp_config` for direct import.",
                },
            },
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "smithery_api_key",
                    "label": "Smithery API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get your key at smithery.ai/account/api-keys",
                },
                {
                    "key": "modelscope_api_token",
                    "label": "ModelScope API Token",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get your token at modelscope.cn → Home → Access Tokens",
                },
            ]
        },
    },
    {
        "name": "list_installed_mcp_servers",
        "display_name": "List Installed MCP Servers",
        "description": (
            "List your installed MCP servers that still have platform-permitted tools. "
            "tool_count counts platform-visible installations; enabled_tool_count counts those enabled "
            "by your current Agent or scene settings. Zero enabled tools means installed but unavailable, "
            "not a working connection. Scene-only tools without an installation are defined separately. "
            "removable only permits uninstalling your own bindings; it does not grant refresh permission. "
            "Server IDs identify installations, not credentials or authentication health."
        ),
        "category": "discovery",
        "icon": "📋",
        "is_default": True,
        "parameters_schema": {"type": "object", "properties": {}, "required": []},
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "refresh_mcp_server",
        "display_name": "Refresh MCP Server",
        "description": (
            "Refresh one MCP server installed exclusively by you, using your Agent configuration. "
            "Use the exact mcp_server_id returned by list_installed_mcp_servers. Inherited enterprise "
            "or shared MCP servers cannot be refreshed here and must use the administrator global "
            "refresh. Existing tool enablement and configuration are preserved; newly discovered "
            "tools become available on your next turn. Refresh discovers tool definitions; it does not "
            "repair CLI credentials or bypass platform disablement."
        ),
        "category": "discovery",
        "icon": "🔄",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "mcp_server_id": {
                    "type": "string",
                    "description": "Exact MCP server UUID. Names and fuzzy identifiers are not accepted.",
                }
            },
            "required": ["mcp_server_id"],
        },
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "uninstall_mcp_server",
        "display_name": "Uninstall MCP Server",
        "description": (
            "Uninstall one MCP server from yourself using the exact mcp_server_id returned by "
            "list_installed_mcp_servers or import_mcp_server. This only removes your self-installed binding; "
            "enterprise/shared MCP definitions and other agents are never affected. The removal is effective immediately."
        ),
        "category": "discovery",
        "icon": "🧹",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "mcp_server_id": {
                    "type": "string",
                    "description": "Exact MCP server UUID. Names and fuzzy identifiers are not accepted.",
                }
            },
            "required": ["mcp_server_id"],
        },
        "config": {},
        "config_schema": {"fields": []},
    },
    # --- Email tools ---
    {
        "name": "send_email",
        "display_name": "Send Email",
        "description": "Send an email to one or more recipients. Supports subject, body text, CC, and file attachments from workspace.",
        "category": "email",
        "icon": "📧",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address(es), comma-separated for multiple"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body text"},
                "cc": {"type": "string", "description": "CC recipients, comma-separated (optional)"},
                "attachments": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of workspace-relative file paths to attach (optional)",
                },
            },
            "required": ["to", "subject", "body"],
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "email_provider",
                    "label": "Email Provider",
                    "type": "select",
                    "options": [
                        {"value": "gmail", "label": "Gmail", "help_text": "Google Account → Security → App passwords → Generate app password", "help_url": "https://support.google.com/accounts/answer/185833"},
                        {"value": "outlook", "label": "Outlook / Microsoft 365", "help_text": "Microsoft Account → Security → App passwords", "help_url": "https://support.microsoft.com/en-us/account-billing/manage-app-passwords-for-two-step-verification-d6dc8c6d-4bf7-4851-ad95-6d07799387e9"},
                        {"value": "qq", "label": "QQ Mail", "help_text": "Settings → Account → POP3/IMAP/SMTP → Enable IMAP → Generate authorization code", "help_url": "https://service.mail.qq.com/detail/0/310"},
                        {"value": "163", "label": "163 Mail", "help_text": "Settings → POP3/SMTP/IMAP → Enable IMAP → Set authorization code", "help_url": "https://help.mail.163.com/faqDetail.do?code=d7a5dc8471cd0c0e8b4b8f4f8e49998b374173cfe9171305fa1ce630d7f67ac2"},
                        {"value": "qq_enterprise", "label": "Tencent Enterprise Mail", "help_text": "Enterprise Mail → Settings → Client-specific password → Generate new password", "help_url": "https://open.work.weixin.qq.com/help2/pc/18624"},
                        {"value": "aliyun", "label": "Alibaba Enterprise Mail", "help_text": "Use your email password directly", "help_url": ""},
                        {"value": "custom", "label": "Custom", "help_text": "Use the authorization code or app password from your email provider", "help_url": ""},
                    ],
                    "default": "gmail",
                },
                {
                    "key": "email_address",
                    "label": "Email Address",
                    "type": "text",
                    "placeholder": "your@email.com",
                },
                {
                    "key": "auth_code",
                    "label": "Authorization Code",
                    "type": "password",
                    "placeholder": "Authorization code (not your login password)",
                },
                {
                    "key": "imap_host",
                    "label": "IMAP Host",
                    "type": "text",
                    "placeholder": "imap.example.com",
                    "depends_on": {"email_provider": ["custom"]},
                },
                {
                    "key": "imap_port",
                    "label": "IMAP Port",
                    "type": "number",
                    "default": 993,
                    "depends_on": {"email_provider": ["custom"]},
                },
                {
                    "key": "smtp_host",
                    "label": "SMTP Host",
                    "type": "text",
                    "placeholder": "smtp.example.com",
                    "depends_on": {"email_provider": ["custom"]},
                },
                {
                    "key": "smtp_port",
                    "label": "SMTP Port",
                    "type": "number",
                    "default": 465,
                    "depends_on": {"email_provider": ["custom"]},
                },
            ]
        },
    },
    {
        "name": "read_emails",
        "display_name": "Read Emails",
        "description": "Read emails from your inbox. Can limit the number returned and search by criteria (e.g. FROM, SUBJECT, SINCE date).",
        "category": "email",
        "icon": "📬",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max number of emails to return (default 10, max 30)", "default": 10},
                "search": {"type": "string", "description": "IMAP search criteria, e.g. 'FROM \"john@example.com\"', 'SUBJECT \"meeting\"', 'SINCE 01-Mar-2026'. Default: all emails."},
                "folder": {"type": "string", "description": "Mailbox folder (default INBOX)", "default": "INBOX"},
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "reply_email",
        "display_name": "Reply Email",
        "description": "Reply to an email by its Message-ID. Maintains the email thread with proper In-Reply-To headers.",
        "category": "email",
        "icon": "↩️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Message-ID of the email to reply to (from read_emails output)"},
                "body": {"type": "string", "description": "Reply body text"},
            },
            "required": ["message_id", "body"],
        },
        "config": {},
        "config_schema": {},
    },
    # --- OKR Tools ---
    # These tools expose the OKR system to agents. Not default — assigned explicitly
    # to the OKR Agent and to other agents that want to self-report progress.
    {
        "name": "get_okr",
        "display_name": "Get OKR Board",
        "description": (
            "Get the full OKR board for the current period. Returns all Objectives and Key Results "
            "for the tenant, organized by company and member level. Includes objective_id values "
            "for every Objective and kr_id values for every Key Result, so you can update existing "
            "Objectives and KRs instead of creating duplicates. Used by the OKR Agent to generate "
            "progress reports and monitor team performance."
        ),
        "category": "okr",
        "icon": "🎯",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "period_start": {
                    "type": "string",
                    "description": "Optional: ISO date string (YYYY-MM-DD) to filter by period start. Defaults to current period.",
                },
                "period_end": {
                    "type": "string",
                    "description": "Optional: ISO date string (YYYY-MM-DD) to filter by period end.",
                },
            },
        },
        "config": {},
        "config_schema": {},
    },
]
