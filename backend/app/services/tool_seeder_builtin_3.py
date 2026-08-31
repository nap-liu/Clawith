"""Builtin tool seed catalog part 3."""

from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_SEED
from app.services.media_tool_contract import SEND_MEDIA_TOOL_SEED


BUILTIN_TOOLS_PART_3 = [
    {
        "name": "send_file_to_agent",
        "display_name": "Agent File Transfer",
        "description": "Send a workspace file to another digital employee. The file is copied to the target agent's workspace/inbox/files/ and an inbox note is created.",
        "category": "communication",
        "icon": "📤",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Target digital employee's canonical agent_id from Relationships/search results."},
                "file_path": {"type": "string", "description": "Workspace-relative path of the source file, e.g. workspace/report.md"},
                "message": {"type": "string", "description": "Optional delivery note for the target digital employee"},
            },
            "required": ["agent_id", "file_path"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "web_search",
        "display_name": "Web Search",
        "description": "[Deprecated] Unified search tool with engine selector. Use the dedicated tools (DuckDuckGo Search, Tavily Search, Google Search, Bing Search, Exa Search) instead for better control per engine.",
        "category": "search",
        "icon": "🔍",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Number of results to return"},
            },
            "required": ["query"],
        },
        "config": {
            "search_engine": "duckduckgo",
            "max_results": 5,
            "language": "en",
            "api_key": "",
        },
        "config_schema": {
            "fields": [
                {
                    "key": "search_engine",
                    "label": "Search Engine",
                    "type": "select",
                    "options": [
                        {"value": "duckduckgo", "label": "DuckDuckGo (free, no API key)"},
                        {"value": "tavily", "label": "Tavily (AI search, needs API key)"},
                        {"value": "google", "label": "Google Custom Search (needs API key)"},
                        {"value": "bing", "label": "Bing Search API (needs API key)"},
                        {"value": "exa", "label": "Exa (AI-powered search, needs API key)"},
                    ],
                    "default": "duckduckgo",
                },
                {
                    "key": "api_key",
                    "label": "API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Required for engines that need an API key",
                    "depends_on": {"search_engine": ["tavily", "google", "bing", "exa"]},
                },
                {
                    "key": "max_results",
                    "label": "Default results count",
                    "type": "number",
                    "default": 5,
                    "min": 1,
                    "max": 20,
                },
                {
                    "key": "language",
                    "label": "Search language",
                    "type": "select",
                    "options": [
                        {"value": "en", "label": "English"},
                        {"value": "zh-CN", "label": "中文"},
                        {"value": "ja", "label": "日本語"},
                    ],
                    "default": "en",
                },
            ]
        },
    },
    {
        "name": "jina_search",
        "display_name": "Jina Search",
        "description": "Search the internet using Jina AI (s.jina.ai). Returns high-quality results with full content. Requires Jina AI API key for higher rate limits.",
        "category": "search",
        "icon": "🔮",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Number of results (default 5, max 10)"},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "Jina AI API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "jina_xxxxxxxxxxxxxxxx (get one at jina.ai)",
                },
            ]
        },
    },
    {
        "name": "jina_read",
        "display_name": "Jina Read",
        "description": "Read and extract full content from a URL using Jina AI Reader (r.jina.ai). Returns clean markdown. Requires Jina AI API key for higher rate limits.",
        "category": "search",
        "icon": "📖",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to read"},
                "max_chars": {"type": "integer", "description": "Max characters to return (default 8000)"},
            },
            "required": ["url"],
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "Jina AI API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "jina_xxxxxxxxxxxxxxxx (get one at jina.ai)",
                },
            ]
        },
    },
    {
        "name": "read_webpage",
        "display_name": "Read Webpage",
        "description": "Fetch a public HTTP/HTTPS URL directly and extract readable webpage text. Use this when you already have a specific link and need its page content without relying on an external reader service.",
        "category": "search",
        "icon": "🌐",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full public HTTP/HTTPS URL to read"},
                "max_chars": {"type": "integer", "description": "Max characters to return (default 12000, max 50000)"},
                "include_links": {"type": "boolean", "description": "Whether to include extracted page links (default false)"},
            },
            "required": ["url"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "exa_search",
        "display_name": "Exa Search",
        "description": "AI-powered web search using Exa (exa.ai). Supports semantic search, category filtering, domain filtering, and multiple content modes (text, highlights, summary). Requires an Exa API key.",
        "category": "search",
        "icon": "🔎",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "description": "Number of results (default 5, max 10)"},
                "search_type": {
                    "type": "string",
                    "description": "Search type: auto (default), neural, or fast",
                    "enum": ["auto", "neural", "fast"],
                },
                "category": {
                    "type": "string",
                    "description": "Filter by category: company, research paper, news, personal site, financial report, or people",
                },
                "include_domains": {
                    "type": "string",
                    "description": "Comma-separated domains to restrict results to (e.g. 'arxiv.org, github.com')",
                },
                "exclude_domains": {
                    "type": "string",
                    "description": "Comma-separated domains to exclude from results",
                },
                "content_mode": {
                    "type": "string",
                    "description": "Content retrieval mode: text (default), highlights, or summary",
                    "enum": ["text", "highlights", "summary"],
                },
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "Exa API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get your API key at exa.ai",
                },
            ]
        },
    },
    # ── Standalone search engines (each engine as its own tool) ──────────────
    # These complement web_search (which remains for backward compatibility).
    # Each tool wraps a single engine so agents can pick the right one for the
    # task without going through the unified engine-selector flow.
    {
        "name": "duckduckgo_search",
        "display_name": "DuckDuckGo Search",
        "description": "Search the internet using DuckDuckGo. Free, no API key required. Returns titles, URLs, and snippets.",
        "category": "search",
        "icon": "🦆",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Number of results to return (default 5, max 10)"},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {"fields": []},
    },
    {
        "name": "tavily_search",
        "display_name": "Tavily Search",
        "description": "AI-optimized web search using Tavily. Returns high-quality results with summaries. Requires a Tavily API key.",
        "category": "search",
        "icon": "🔍",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Number of results to return (default 5, max 10)"},
            },
            "required": ["query"],
        },
        "config": {},
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "Tavily API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "tvly-xxxxxxxxxxxxxxxx (get one at tavily.com)",
                },
            ]
        },
    },
    {
        "name": "google_search",
        "display_name": "Google Search",
        "description": "Search using Google Custom Search JSON API. Returns titles, URLs, and snippets. Requires a Google API key and Custom Search Engine ID (format: API_KEY:CX_ID).",
        "category": "search",
        "icon": "🔍",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Number of results to return (default 5, max 10)"},
                "language": {"type": "string", "description": "Search language code (e.g. 'en', 'zh')"},
            },
            "required": ["query"],
        },
        "config": {"language": "en"},
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "API Key & Search Engine ID",
                    "type": "password",
                    "default": "",
                    "placeholder": "API_KEY:SEARCH_ENGINE_ID (get at console.cloud.google.com)",
                },
                {
                    "key": "language",
                    "label": "Search language",
                    "type": "select",
                    "options": [
                        {"value": "en", "label": "English"},
                        {"value": "zh-CN", "label": "Chinese"},
                        {"value": "ja", "label": "Japanese"},
                    ],
                    "default": "en",
                },
            ]
        },
    },
    {
        "name": "bing_search",
        "display_name": "Bing Search",
        "description": "Search using Bing Web Search API. Returns titles, URLs, and snippets. Requires a Bing Search API key from Microsoft Azure.",
        "category": "search",
        "icon": "🔍",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keywords"},
                "max_results": {"type": "integer", "description": "Number of results to return (default 5, max 10)"},
                "language": {"type": "string", "description": "Market language code (e.g. 'en-US', 'zh-CN')"},
            },
            "required": ["query"],
        },
        "config": {"language": "en-US"},
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "Bing Search API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get from Azure Cognitive Services (Bing Search v7)",
                },
                {
                    "key": "language",
                    "label": "Market language",
                    "type": "select",
                    "options": [
                        {"value": "en-US", "label": "English (US)"},
                        {"value": "zh-CN", "label": "Chinese (Simplified)"},
                        {"value": "ja-JP", "label": "Japanese"},
                    ],
                    "default": "en-US",
                },
            ]
        },
    },
    {
        "name": "plaza_get_new_posts",
        "display_name": "Plaza: Browse",
        "description": "Get recent posts from the Agent Plaza (shared social feed). Returns posts and comments since a given timestamp.",
        "category": "social",
        "icon": "🏛️",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max number of posts to return (default 10)", "default": 10},
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "plaza_create_post",
        "display_name": "Plaza: Post",
        "description": "Publish a new post to the Agent Plaza. Share work insights, tips, or interesting discoveries. Do NOT share private information.",
        "category": "social",
        "icon": "📝",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Post content (max 500 chars). Must be public-safe."},
            },
            "required": ["content"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "plaza_add_comment",
        "display_name": "Plaza: Comment",
        "description": "Add a comment to an existing plaza post. Engage with colleagues' posts.",
        "category": "social",
        "icon": "💬",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "post_id": {"type": "string", "description": "The UUID of the post to comment on"},
                "content": {"type": "string", "description": "Comment content (max 300 chars)"},
            },
            "required": ["post_id", "content"],
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "execute_code",
        "display_name": "Code Executor",
        "description": (
            "Execute code (Python, Bash, Node.js) in a local sandboxed subprocess. "
            "Working directory is the agent's root (contains soul.md, memory/, "
            "skills/, workspace/, enterprise_info/). To save files for the user "
            "write them under workspace/ (e.g. workspace/report.md). Use relative "
            "paths from the agent root. EACH CALL IS A FRESH PROCESS — variables, "
            "imports, environment variables, and `cd` do NOT carry over between "
            "calls; include all setup in every call. For long outputs (e.g. "
            "pip list) the stdout is truncated to 10000 chars; prefer narrow "
            "queries (pip show <pkg>, ls workspace) over full listings. "
            "Timeouts: pass `timeout=<seconds>` per call to pick your own "
            "budget (default 30s, platform safety cap 300s). Pick short for "
            "echo/ls (30s), medium for pip install of small pkgs (60s), "
            "longer for large compiles or big data work (180-300s). "
            "Subprocess has no internet by default."
        ),
        "category": "code",
        "icon": "💻",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "language": {"type": "string", "enum": ["python", "bash", "node"], "description": "Programming language"},
                "code": {"type": "string", "description": "Code to execute"},
                "timeout": {"type": "integer", "description": "Per-call execution timeout in seconds. Choose based on task complexity (default 60, platform cap 300; e.g. 30 for echo/ls, 60-120 for pip/npm install of small pkgs, 180-300 for heavy compiles or git clone of large repos). If a command times out the session is reset and the command's bg processes / exported env vars are lost — pick a generous timeout for long jobs."},
            },
            "required": ["language", "code"],
        },
        "config": {
            "sandbox_type": "subprocess",
            "cpu_limit": "0.5",
            "memory_limit": "256m",
            "allow_network": True,
            "default_timeout": 30,
            "max_timeout": 300,
        },
        "config_schema": {
            "fields": [
                {
                    "key": "cpu_limit",
                    "label": "CPU Limit",
                    "type": "text",
                    "default": "0.5",
                    "placeholder": "e.g., 0.5, 1.0, 2.0",
                },
                {
                    "key": "memory_limit",
                    "label": "Memory Limit",
                    "type": "text",
                    "default": "256m",
                    "placeholder": "e.g., 256m, 512m, 1g",
                },
                {
                    "key": "allow_network",
                    "label": "Allow Network Access",
                    "type": "checkbox",
                    "default": True,
                    "read_only_for_roles": ["agent_admin", "member"],
                },
                {
                    "key": "default_timeout",
                    "label": "Default Timeout (seconds)",
                    "type": "number",
                    "default": 30,
                    "min": 5,
                    "max": 3600,
                },
                {
                    "key": "max_timeout",
                    "label": "Max Timeout (seconds)",
                    "type": "number",
                    "default": 60,
                    "min": 10,
                    "max": 3600,
                },
            ]
        },
    },
    {
        "name": "execute_code_e2b",
        "display_name": "Code Executor (E2B Cloud)",
        "description": "Execute code (Python, Bash, Node.js) in a secure E2B cloud sandbox. Provides full network access and an isolated environment without consuming local resources. Requires an E2B API key.",
        "category": "code",
        "icon": "☁️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "language": {"type": "string", "enum": ["python", "bash", "node"], "description": "Programming language"},
                "code": {"type": "string", "description": "Code to execute"},
                "timeout": {"type": "integer", "description": "Per-call execution timeout in seconds. Choose based on task complexity (default 60, platform cap 300; e.g. 30 for echo/ls, 60-120 for pip/npm install of small pkgs, 180-300 for heavy compiles or git clone of large repos). If a command times out the session is reset and the command's bg processes / exported env vars are lost — pick a generous timeout for long jobs."},
            },
            "required": ["language", "code"],
        },
        "config": {
            "sandbox_type": "e2b",
            "api_key": "",
            "default_timeout": 30,
            "max_timeout": 300,
        },
        "config_schema": {
            "fields": [
                {
                    "key": "api_key",
                    "label": "E2B API Key",
                    "type": "password",
                    "default": "",
                    "placeholder": "Get your API key at https://e2b.dev",
                    "required": True,
                },
                {
                    "key": "default_timeout",
                    "label": "Default Timeout (seconds)",
                    "type": "number",
                    "default": 30,
                    "min": 5,
                    "max": 3600,
                },
                {
                    "key": "max_timeout",
                    "label": "Max Timeout (seconds)",
                    "type": "number",
                    "default": 60,
                    "min": 10,
                    "max": 3600,
                },
            ]
        },
    },

    {
        "name": "execute_code_aio",
        "display_name": "Code Executor (AIO Sandbox)",
        "description": (
            "Execute foreground code or autonomously start and manage background "
            "jobs in a long-lived per-session aio-sandbox environment.\n"
            "\n"
            "Actions:\n"
            "- execute (default): run code. execution_mode is required: choose "
            "foreground for commands expected to finish in the current turn; choose "
            "background for servers, listeners, watch processes, device/OAuth "
            "authorization waits, continuous polling, or any work that must survive "
            "later chat turns. In background mode, submit the command directly and "
            "let the managed AIO job own its lifecycle.\n"
            "- list_jobs: list every background job owned by the current chat session.\n"
            "- job_status / job_logs / job_stop: inspect status, read live output "
            "(including while running), or stop the supplied job_id.\n"
            "If you forget a job_id, call list_jobs. Stop jobs that are no longer needed.\n"
            "\n"
            "Languages and runtimes:\n"
            "- Bash, Python (Jupyter kernel pinned to python3.10), Node.js v22.\n"
            "- Also installed under /opt/: python3.11, python3.12.\n"
            "\n"
            "Pre-installed CLI tools: git, gh, uv, curl, wget, ssh / scp / "
            "ssh-keygen / ssh-agent / rsync, vim, nano, jq, rg, htop, "
            "imagemagick, yt-dlp.\n"
            "\n"
            "Pre-installed Python (3.10) packages: requests, numpy, pandas.\n"
            "\n"
            "Network: outbound internet is available.\n"
            "\n"
            "Working directory:\n"
            "- Each call starts in the agent root, which contains soul.md, "
            "  memory/, skills/, workspace/, enterprise_info/.\n"
            "- `cd` within a single call works for chained commands. The "
            "  working directory is reset on the next call (not carried over).\n"
            "- HOME equals the agent root, so `~/...` paths land in the agent's "
            "  own filesystem.\n"
            "- Files written by this tool are visible via read_file / "
            "  list_files / write_file using relative paths from the agent "
            "  root.\n"
            "\n"
            "Persistence across calls:\n"
            "- Shell (bash/node): exported environment variables and "
            "  foreground shell state persist inside one chat session. Working "
            "  directory does not.\n"
            "- Managed background jobs are isolated for control at chat-session level, "
            "  but share the same AIO PID/network/mount/IPC namespaces, Agent HOME, "
            "  files, localhost ports and Unix sockets with foreground commands and "
            "  sibling jobs.\n"
            "- Python (Jupyter): variables, imports, and global state persist.\n"
            "- Filesystem under the agent root persists.\n"
            "\n"
            "Package install locations:\n"
            "- pip install <pkg>      → $HOME/.local/lib/python3.10/site-packages/\n"
            "- npm install <pkg>      → <cwd>/node_modules/\n"
            "- npm install -g <pkg>   → $HOME/.npm-global/\n"
            "- apt install <pkg>      → container-wide system paths (requires sudo, "
            "  shared across all agents on this container, reset on container "
            "  rebuild).\n"
            "Pip and npm installs survive container restarts (the agent's "
            "filesystem is bind-mounted). Pip-installed packages are visible "
            "to the same agent's jupyter kernel because the kernel's Python "
            "is the same interpreter pip writes to.\n"
            "\n"
            "Pre-exported environment variables (in every call):\n"
            "- HOME=<agent-root>, PIP_USER=1, NPM_CONFIG_PREFIX=$HOME/.npm-global, "
            "  PATH includes $HOME/.npm-global/bin\n"
            "- CI=true, NPM_CONFIG_YES=true, DEBIAN_FRONTEND=noninteractive, "
            "  GIT_TERMINAL_PROMPT=0\n"
            "\n"
            "I/O contract:\n"
            "- stdin is not connected to a TTY; commands that block waiting "
            "  for terminal input receive nothing.\n"
            "- stdout is truncated at ~10 KB, stderr at ~5 KB per call.\n"
            "- Python tracebacks and shell stderr are returned verbatim.\n"
            "\n"
            "Timeout:\n"
            "- Foreground `timeout` is the command lifetime (default 30, cap 300).\n"
            "- Background `timeout` is the managed Job lifetime (default 900, "
            "  configurable cap 3600). A Job timeout terminates only that Job.\n"
            "- Foreground timeout terminates only the active foreground process group; "
            "  it does not delete the shell or managed Jobs.\n"
            "\n"
            "Isolation:\n"
            "- Each agent has its own HOME, its own shell session, its own "
            "  jupyter kernel, and its own pip / npm package locations.\n"
            "- The aio-sandbox container itself is shared across agents on "
            "  the same host; the agent-root and HOME pinning is what keeps "
            "  per-agent state from leaking through default paths."
        ),
        "category": "code",
        "icon": "📦",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["execute", "list_jobs", "job_status", "job_logs", "job_stop"],
                    "default": "execute",
                    "description": "Execute code or manage background jobs in the current chat session",
                },
                "language": {"type": "string", "enum": ["python", "bash", "node"], "description": "Programming language"},
                "code": {"type": "string", "description": "Code to execute; required when action=execute"},
                "execution_mode": {
                    "type": "string",
                    "enum": ["foreground", "background"],
                    "description": "Required. Choose foreground for work that finishes in the current turn. Choose background for servers, listeners, authorization waits, polling, and work that must survive later turns; submit the command directly and let the managed AIO job own its lifecycle.",
                },
                "timeout": {"type": "integer", "description": "Existing timeout. Foreground: command lifetime. Background: managed Job lifetime."},
                "job_id": {"type": "string", "description": "Required for job_status, job_logs and job_stop; obtain it from background execute or list_jobs"},
                "tail_lines": {"type": "integer", "description": "For job_logs, trailing lines to return (default 100, max 500)"},
            },
            "required": ["execution_mode"],
        },
        "config": {
            "toolscall_enabled": True,
            "sandbox_type": "aio_sandbox",
            "api_url": "http://aio-sandbox:8080",
            "api_key": "",
            "default_timeout": 30,
            "max_timeout": 300,
            "background_default_timeout": 900,
            "background_max_timeout": 3600,
        },
        "config_schema": {
            "fields": [
                {
                    "key": "toolscall_enabled",
                    "label": "Enable toolscall",
                    "type": "checkbox",
                    "default": True,
                    "agent_only": True,
                    "help_text": (
                        "Allow this Agent to call its current-turn builtin, MCP, "
                        "and native CLI tools from AIO shell pipelines."
                    ),
                },
                {
                    "key": "api_url",
                    "label": "Sandbox URL",
                    "type": "text",
                    "default": "http://aio-sandbox:8080",
                    "placeholder": "http://aio-sandbox:8080",
                    "required": True,
                },
                {
                    "key": "api_key",
                    "label": "Bearer Token (optional)",
                    "type": "password",
                    "default": "",
                    "placeholder": "Only set if the sandbox is auth-protected",
                    "required": False,
                },
                {
                    "key": "default_timeout",
                    "label": "Default Timeout (seconds)",
                    "type": "number",
                    "default": 30,
                    "min": 5,
                    "max": 300,
                },
                {
                    "key": "max_timeout",
                    "label": "Max Timeout (seconds)",
                    "type": "number",
                    "default": 60,
                    "min": 10,
                    "max": 300,
                },
                {
                    "key": "background_default_timeout",
                    "label": "Background Job Default Lifetime (seconds)",
                    "type": "number",
                    "default": 900,
                    "min": 30,
                    "max": 3600,
                },
                {
                    "key": "background_max_timeout",
                    "label": "Background Job Maximum Lifetime (seconds)",
                    "type": "number",
                    "default": 3600,
                    "min": 60,
                    "max": 86400,
                },
            ]
        },
    },

]
