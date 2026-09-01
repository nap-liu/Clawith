"""Messaging, web, document, code, and image tool schemas."""


AGENT_TOOL_RUNTIME = [
    {
        "type": "function",
        "function": {
            "name": "send_feishu_message",
            "description": (
                "Send a Feishu IM message to a colleague. "
                "Provide the colleague's canonical platform user_id. "
                "To contact digital employees use send_message_to_agent instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "Recipient's canonical platform user_id. Provider IDs are resolved internally.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message content to send",
                    },
                },
                "required": ["user_id", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_contacts",
            "description": (
                "Search people and digital employees as candidates for relationship network editing. "
                "Only use this tool when the user explicitly asks you to search, review, or edit the relationship network. "
                "Do not use it proactively just because you want to contact someone. "
                "Human results contain user_id; digital employee results contain agent_id. Names are display-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Name, department, email, pinyin, or exact phone number to search.",
                    },
                    "type": {
                        "type": "string",
                        "description": "Optional contact type filter.",
                        "enum": ["all", "human", "agent"],
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return, from 1 to 50.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_contact",
            "description": (
                "Add a person or digital employee to your relationship network using exactly one canonical ID returned by search_contacts. "
                "Only call this tool after the user explicitly asked to edit the relationship network and the agent creator has clearly confirmed the selected target in the conversation or a confirmation card. "
                "Do not add contacts proactively or based only on your own intent to contact someone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "Canonical natural-person user_id."},
                    "agent_id": {"type": "string", "description": "Canonical digital-employee agent_id."},
                    "relation": {
                        "type": "string",
                        "description": "Relationship label, such as collaborator, stakeholder, peer, team_member, or other.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional short note explaining why this contact is needed.",
                    },
                },
                "oneOf": [
                    {"required": ["user_id"], "not": {"required": ["agent_id"]}},
                    {"required": ["agent_id"], "not": {"required": ["user_id"]}},
                ],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_contact",
            "description": (
                "Remove a person or digital employee from your relationship network using exactly one canonical ID returned by search_contacts. "
                "Only call this tool after the user explicitly asked to edit the relationship network and the agent creator has clearly confirmed the selected target in the conversation or a confirmation card. "
                "Do not remove contacts proactively or based only on your own intent to stop messaging someone."
            ),
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_channel_message",
            "description": (
                "Send a message to a related person through an external IM route. "
                "Address the person only by canonical user_id. If several routes are valid, "
                "choose channel explicitly; the platform never selects a first match."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "Recipient's canonical platform user_id from Relationships/search results.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message content to send",
                    },
                    "channel": {
                        "type": "string",
                        "description": "External route chosen by the Agent when multiple valid routes exist.",
                        "enum": ["feishu", "dingtalk", "wecom", "slack", "teams", "wechat"],
                    },
                },
                "required": ["user_id", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_session_message",
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
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_group_session_message",
            "description": (
                "Compatibility tool for sending text to an existing external-IM group by exact "
                "session_id. Prefer send_session_message for new work; this tool remains available "
                "for existing workflows and accepts group Sessions only. For a native group mention, you MUST "
                "call this tool with mention_user_ids or mention_all=true; writing @name or @everyone in a normal "
                "assistant reply is plain text and does not create a native mention."
            ),
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_message",
            "description": (
                "Recall one message previously sent by this digital employee through any IM channel. "
                "Use only the exact local message_id returned by send_session_message/send_channel_message "
                "or shown by read_session_messages. Never guess a message ID. Unsupported provider paths "
                "return a normalized unsupported result without deleting local audit history."
            ),
            "parameters": {
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
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_platform_message",
            "description": "Send a message to a first-party platform user (web or app). The message will appear in their platform chat history and be pushed in real-time if they are online. Use this to proactively notify platform users.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {
                        "type": "string",
                        "description": "Canonical platform user_id of the recipient.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message content to send",
                    },
                },
                "required": ["user_id", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_dingtalk_channel_provisioning",
            "description": (
                "为当前数字员工配置钉钉机器人通道。已配置时默认直接告知用户通道已经可用，"
                "无需再次授权。只有用户明确要求强制重配时才设置 force_reconfigure=true；"
                "强制重配会创建新的钉钉机器人应用，原应用需要用户在钉钉后台自行清理。"
                "未配置或明确强制重配时，工具返回授权链接，用户授权后平台自动完成配置。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "force_reconfigure": {
                        "type": "boolean",
                        "description": (
                            "默认 false。仅当用户明确要求强制覆盖当前钉钉通道时设为 true；普通配置请求保持 false。"
                        ),
                        "default": False,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dingtalk_channel_provisioning_status",
            "description": (
                "查询当前数字员工钉钉机器人通道自动配置流程的状态。"
                "当用户询问钉钉授权是否完成、链接是否过期、或配置是否已经生效时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provisioning_id": {
                        "type": "string",
                        "description": "start_dingtalk_channel_provisioning 返回的配置编号。",
                    },
                },
                "required": ["provisioning_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to_agent",
            "description": "Send a message to a digital employee colleague. The recipient is another AI agent, not a human. Refer to the 'Relationships' section in your system prompt for available digital employees.\n\nDECISION GUIDE for msg_type:\nAsk yourself: does the target agent need to DO WORK (analyze, research, summarize, write, compare, plan, etc.) and RETURN RESULTS to you or the user?\n\n- If YES, the target needs to do work → use task_delegate. Examples: 'summarize X', 'analyze Y', 'check Z', 'prepare a report', 'review and give feedback', 'find out X', 'confirm with X and report back'. The target works asynchronously and you will be woken when they finish.\n\n- If the target just needs to KNOW something → use notify. Examples: 'meeting cancelled', 'I updated the doc', 'heads up about X', 'FYI'. No reply expected.\n\n- If you need a quick factual answer right now → use consult. Examples: 'what is X?', 'do you know Y?'. Synchronous, blocks until reply.\n\nWhen in doubt between notify and task_delegate, prefer task_delegate — it is safer because it guarantees the user gets a result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Target digital employee's canonical agent_id from Relationships/search results.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Message content to send",
                    },
                    "msg_type": {
                        "type": "string",
                        "enum": ["notify", "consult", "task_delegate"],
                        "description": "Decision guide: (1) Will the target need to DO WORK and return results? → task_delegate. (2) Is this just a one-way FYI? → notify. (3) Quick factual question needing immediate answer? → consult. When unsure, prefer task_delegate.",
                    },
                    "new_conversation": {
                        "type": "boolean",
                        "description": (
                            "默认 false。仅当当前与该同事的对话明显异常时设为 true 来主动重置 —— "
                            "例如对话反复报同一个错、陷入循环、或历史上下文看起来已损坏/混乱。"
                            "设为 true 会开启一条全新对话线程,丢弃旧的(可能已损坏的)历史,从干净状态重新开始。"
                            "正常往来请保持 false 或省略。"
                        ),
                    },
                },
                "required": ["agent_id", "message", "msg_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file_to_agent",
            "description": "Send a workspace file to another digital employee. The file is copied into the target agent's workspace/inbox/files/ directory and a delivery note is created in their inbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Target digital employee's canonical agent_id from Relationships/search results.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Workspace-relative path of the source file, e.g. workspace/report.md",
                    },
                    "message": {
                        "type": "string",
                        "description": "Optional delivery note for the target digital employee",
                    },
                },
                "required": ["agent_id", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "jina_search",
            "description": "Search the internet using Jina AI Search (s.jina.ai). Returns high-quality search results with full page content, not just snippets. Ideal for research, news, technical docs, and any real-time information lookup.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'Python asyncio best practices' or '苏州通道人工智能科技有限公司'",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return, default 5, max 10",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "jina_read",
            "description": "Read and extract the full content from a web page URL using Jina AI Reader (r.jina.ai). Returns clean, well-structured markdown including article text, tables, and key information. Better than jina_search when you already have a specific URL to read.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL of the web page to read, e.g. 'https://example.com/article'",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters to return (default 8000, max 20000)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_webpage",
            "description": "Fetch a public HTTP/HTTPS URL directly and extract readable webpage text. Use this when you already have a specific link and need the page content without relying on an external reader service. Private, local, and internal network URLs are blocked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full public HTTP/HTTPS URL of the web page to read, e.g. 'https://example.com/article'",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters to return (default 12000, max 50000)",
                    },
                    "include_links": {
                        "type": "boolean",
                        "description": "Include up to 30 extracted page links (default false)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Extract text from an office document (PDF, Word, Excel, PPT). The path is matched exactly and is never corrected to a similar filename, so copy the path from the attachment context or list_files output. Storage, materialization, size-limit, timeout, and parser failures are reported as distinct results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Document file path, e.g.: workspace/knowledge_base/report.pdf, enterprise_info/knowledge_base/policy.docx",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_image",
            "description": (
                "Read an image: transcribe all visible text (preserving headers, bullets, tables). "
                "If the image is not text-dominant (charts, scenes, photos), briefly describe what is seen. "
                "Useful for: image-only PPTX slides, screenshots, scanned documents, reading chart data. "
                "Inputs: workspace relative path, http(s):// URL, or data:image/*;base64,… "
                "(exactly which modes are available is controlled by the admin). "
                "Output is a plain-text string with per-image blocks separated by '--- Image N: <ref> ---'; "
                "some blocks may be ❌ error lines — inspect each and decide whether to retry or skip. "
                "Do NOT call this tool on text you just generated yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 6,
                        "description": (
                            "要识别的图片列表。每项可以是 (1) workspace 相对路径；(2) http(s):// URL "
                            "(仅当管理员启用 URL 模式时)；(3) data:image/<fmt>;base64,<payload> "
                            "(仅当管理员启用 base64 模式时)。支持 jpeg/png/webp/gif。未启用的输入模式会直接报错。"
                            "单次最多 6 张，超过请分多次调用。"
                        ),
                    }
                },
                "required": ["image_paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_code",
            "description": "Execute code (Python, Bash, or Node.js) in a local sandboxed subprocess within the agent's root directory. Useful for data processing, calculations, file transformations, and automation scripts. Code runs with the agent root as the working directory, so you can access skills/, workspace/, memory/ etc. directly. Security restrictions apply: no system-level operations, 30-second default timeout.",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["python", "bash", "node"],
                        "description": "Programming language to execute",
                    },
                    "code": {
                        "type": "string",
                        "description": "Code to execute. If a Python import fails due to a missing package, install it first via execute_code with language='bash' and code='pip install <package>'. Working directory is the agent root (skills/, workspace/, memory/ are accessible).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max execution time in seconds (default 60, max 3600)",
                    },
                },
                "required": ["language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_code_e2b",
            "description": "Execute code (Python, Bash, or Node.js) in a secure E2B cloud sandbox. The sandbox has full network access and is fully isolated from the server. Use this when local execution is insufficient or when network access is required inside the code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["python", "bash", "node"],
                        "description": "Programming language to execute",
                    },
                    "code": {
                        "type": "string",
                        "description": "Code to execute in the E2B cloud sandbox.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max execution time in seconds (default 30, max 60)",
                    },
                },
                "required": ["language", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upload_image",
            "description": "Upload an image file from your workspace (or from a public URL) to a cloud CDN and get a permanent public URL. Use this when you need to share images externally, embed them in messages/reports, or make workspace images accessible via URL. Supports common formats: PNG, JPG, GIF, WebP, SVG.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Workspace-relative path to the image file, e.g. workspace/chart.png or workspace/knowledge_base/diagram.jpg",
                    },
                    "url": {
                        "type": "string",
                        "description": "Alternative: a public URL of an image to upload (e.g. https://example.com/photo.jpg). Use this instead of file_path when the image is not in your workspace.",
                    },
                    "file_name": {
                        "type": "string",
                        "description": "Optional custom filename for the uploaded image. If omitted, the original filename is used.",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Optional CDN folder path, e.g. /agents/reports.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image_siliconflow",
            "description": "Generate an image via SiliconFlow (FLUX). Save to workspace. Fast and China-friendly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed image description in English.",
                    },
                    "size": {
                        "type": "string",
                        "description": "Image size. Default: 1024x1024. Options: 1024x1024, 1024x768, 768x1024",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Workspace path to save the image (e.g. workspace/images/sunset.png).",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image_openai",
            "description": "Generate an image via OpenAI. Save to workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed image description in English.",
                    },
                    "size": {
                        "type": "string",
                        "description": "Image size. Default: 1024x1024.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Workspace path to save the image.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image_google",
            "description": "Generate an image via Google Gemini Image (Nano Banana) or Vertex AI. Save to workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed image description in English.",
                    },
                    "size": {
                        "type": "string",
                        "description": "Image size. Default: 1024x1024.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Workspace path to save the image.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image_custom",
            "description": "Generate an image via the company-configured custom image API. Save to workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed image description in English.",
                    },
                    "size": {
                        "type": "string",
                        "description": "Image size. Default: 1024x1024.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Workspace path to save the image.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "discover_resources",
            "description": "Search public MCP registries (Smithery) for tools and capabilities that can extend your abilities. Use this when you encounter a task you cannot handle with your current tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Semantic description of the capability needed, e.g. 'send email', 'query SQL database', 'generate images'",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results to return (default 5, max 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
]

__all__ = ["AGENT_TOOL_RUNTIME"]
