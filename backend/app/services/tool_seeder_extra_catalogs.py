"""OKR, deployment, and browser builtin tool seed catalogs."""

OKR_BUILTIN_TOOLS = [
    {
        "name": "get_okr",
        "display_name": "Get OKR",
        "description": (
            "Read the full OKR board for the current period: company-level Objectives and "
            "Key Results, plus every member's (human and agent) individual O and KRs with "
            "current progress values. Includes objective_id for each Objective and kr_id for "
            "each Key Result. Use this to understand company direction, update existing OKRs, "
            "and see how others are tracking before planning your own work."
        ),
        "category": "okr",
        "icon": "🎯",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "period_start": {
                    "type": "string",
                    "description": "Optional ISO date (YYYY-MM-DD). Defaults to the current period start.",
                },
                "period_end": {
                    "type": "string",
                    "description": "Optional ISO date (YYYY-MM-DD). Defaults to the current period end.",
                },
            },
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "get_my_okr",
        "display_name": "Get My OKR",
        "description": (
            "Read your own Objectives and Key Results for the current period, including "
            "kr_id values needed to update progress. Call this before update_kr_progress "
            "to get the correct kr_id."
        ),
        "category": "okr",
        "icon": "🎯",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {},
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "update_kr_progress",
        "display_name": "Update KR Progress",
        "description": (
            "Update the current progress value of one of YOUR OWN Key Results. "
            "Call get_my_okr first to obtain the kr_id. "
            "A progress log entry is created automatically for history tracking."
        ),
        "category": "okr",
        "icon": "📈",
        "is_default": True,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "kr_id": {
                    "type": "string",
                    "description": "UUID of the Key Result to update (from get_my_okr).",
                },
                "value": {
                    "type": "number",
                    "description": "New current value (e.g. 3500 for a follower count, 75 for a percentage).",
                },
                "note": {
                    "type": "string",
                    "description": "Optional note explaining the progress update.",
                },
            },
            "required": ["kr_id", "value"],
        },
        "config": {},
        "config_schema": {},
    },
]

DEPLOY_BUILTIN_TOOLS = [
    {
        "name": "vercel_deploy",
        "display_name": "Deploy to Vercel",
        "description": "Deploy a project from workspace to Vercel. Supports two modes: 'upload' (direct file upload, no GitHub needed) or 'github' (push to GitHub repo, Vercel auto-deploys). Returns the deployment URL.",
        "category": "deploy",
        "icon": "🚀",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Vercel project name (will be created if not exists)"
                },
                "source_dir": {
                    "type": "string",
                    "description": "Directory in workspace containing the project, e.g. 'workspace/my-app'"
                },
                "deploy_method": {
                    "type": "string",
                    "enum": ["upload", "github"],
                    "description": "'upload': direct file upload (simple, no GitHub needed). 'github': push to GitHub repo and let Vercel auto-deploy (better for version control and CI/CD). Default: 'upload'."
                },
                "github_repo": {
                    "type": "string",
                    "description": "GitHub repo in 'owner/repo' format. Required when deploy_method='github'."
                },
                "framework": {
                    "type": "string",
                    "description": "Framework preset: 'nextjs', 'vite', 'static', etc.",
                    "enum": ["nextjs", "vite", "nuxtjs", "static", "remix", "astro"]
                },
                "production": {
                    "type": "boolean",
                    "description": "If true, deploy to production. Default false (preview)."
                }
            },
            "required": ["project_name", "source_dir"]
        },
        "config": {"vercel_token": ""},
        "config_schema": {
            "fields": [
                {
                    "key": "vercel_token",
                    "label": "Vercel Access Token",
                    "type": "password",
                    "default": "",
                    "help_text": "Get from https://vercel.com/account/tokens"
                }
            ]
        }
    },
    {
        "name": "vercel_list_deployments",
        "display_name": "List Vercel Deployments",
        "description": "List recent deployments for a Vercel project. Shows status, URL, and creation time.",
        "category": "deploy",
        "icon": "📋",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Vercel project name"}
            },
            "required": ["project_name"]
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "vercel_get_deploy_logs",
        "display_name": "Get Deploy Logs",
        "description": "Get build logs and runtime logs for a Vercel deployment. Useful for debugging failed deployments.",
        "category": "deploy",
        "icon": "📜",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "deployment_id": {"type": "string", "description": "Deployment ID or URL"}
            },
            "required": ["deployment_id"]
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "vercel_set_env",
        "display_name": "Set Environment Variable",
        "description": "Set an environment variable for a Vercel project. Use for database URLs, API keys, and other secrets.",
        "category": "deploy",
        "icon": "🔐",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string"},
                "key": {"type": "string", "description": "Environment variable name, e.g. DATABASE_URL"},
                "value": {"type": "string", "description": "Environment variable value"},
                "target": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["production", "preview", "development"]},
                    "description": "Deployment targets. Default: all."
                }
            },
            "required": ["project_name", "key", "value"]
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "vercel_manage_domain",
        "display_name": "Manage Domain",
        "description": "Check domain availability/pricing, or bind a custom domain to a Vercel project.",
        "category": "deploy",
        "icon": "🌐",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["check", "bind"],
                    "description": "'check' to check availability/price, 'bind' to add domain to project"
                },
                "domain": {"type": "string", "description": "Domain name, e.g. 'myapp.com'"},
                "project_name": {"type": "string", "description": "Required for 'bind' action"}
            },
            "required": ["action", "domain"]
        },
        "config": {},
        "config_schema": {},
    },
    {
        "name": "neon_create_database",
        "display_name": "Create Postgres Database",
        "description": "Create a new Neon Postgres database. Returns the DATABASE_URL connection string. Use vercel_set_env to inject it into your Vercel project.",
        "category": "deploy",
        "icon": "🐘",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "Name for the Neon project"
                },
                "database_name": {
                    "type": "string",
                    "description": "Database name, default 'neondb'"
                },
                "region": {
                    "type": "string",
                    "description": "Region: 'aws-us-east-1', 'aws-eu-central-1', etc.",
                    "default": "aws-us-east-1"
                },
                "org_id": {
                    "type": "string",
                    "description": "Optional: Neon Organization ID. If not provided and you belong to multiple organizations, the tool will automatically list them for you to choose."
                }
            },
            "required": ["project_name"]
        },
        "config": {"neon_api_key": ""},
        "config_schema": {
            "fields": [
                {
                    "key": "neon_api_key",
                    "label": "Neon API Key",
                    "type": "password",
                    "default": "",
                    "help_text": "Get from https://console.neon.tech/app/settings/api-keys"
                }
            ]
        }
    }
]

BROWSER_BUILTIN_TOOLS = [
    {
        "name": "browse",
        "display_name": "Browse Web Page",
        "description": (
            "Open a web page in an isolated, per-conversation browser running "
            "inside the aio-sandbox container, and return its readable text.\n"
            "Use this to research a URL, read an article, or check a page's "
            "current contents. Each conversation has its own browser context, "
            "so cookies and logins are isolated and persist within the "
            "conversation.\n"
            "Set screenshot=true to also save a PNG of the page to your "
            "workspace. Returns the page title and visible text (long pages "
            "are truncated)."
        ),
        "category": "browser",
        "icon": "🌐",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The full URL to open (must include http:// or https://)."},
                "extract": {"type": "boolean", "description": "Return the page title and visible text. Default true."},
                "screenshot": {"type": "boolean", "description": "Also save a PNG screenshot of the page to your workspace. Default false."},
            },
            "required": ["url"],
        },
        "config": {
            "sandbox_type": "aio_sandbox",
            "api_url": "http://aio-sandbox:8080",
            "api_key": "",
            "default_timeout": 30,
            "max_timeout": 60,
        },
        "config_schema": {
            "fields": [
                {"key": "api_url", "label": "Sandbox URL", "type": "text", "default": "http://aio-sandbox:8080", "placeholder": "http://aio-sandbox:8080", "required": True},
                {"key": "api_key", "label": "Bearer Token (optional)", "type": "password", "default": "", "placeholder": "Only set if the sandbox is auth-protected", "required": False},
                {"key": "default_timeout", "label": "Default Timeout (seconds)", "type": "number", "default": 30, "min": 5, "max": 120},
                {"key": "max_timeout", "label": "Max Timeout (seconds)", "type": "number", "default": 60, "min": 10, "max": 120},
            ]
        },
    },
    {
        "name": "web_open",
        "display_name": "Open Page",
        "description": (
            "Navigate the conversation's PERSISTENT browser page to a URL inside the "
            "aio-sandbox container. Unlike `browse` (one-shot read), the page stays open "
            "across tool calls, so you can then use web_eval / web_cdp to interact with it "
            "(click, fill forms, read DOM, scroll) for multi-step web automation. Each "
            "conversation has its own isolated page (cookies/login persist within it)."
        ),
        "category": "browser",
        "icon": "🌐",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The full URL to open (must include http:// or https://)."},
            },
            "required": ["url"],
        },
        "config": {
            "sandbox_type": "aio_sandbox",
            "api_url": "http://aio-sandbox:8080",
            "api_key": "",
            "default_timeout": 30,
            "max_timeout": 60,
        },
        "config_schema": {
            "fields": [
                {"key": "api_url", "label": "Sandbox URL", "type": "text", "default": "http://aio-sandbox:8080", "placeholder": "http://aio-sandbox:8080", "required": True},
                {"key": "api_key", "label": "Bearer Token (optional)", "type": "password", "default": "", "placeholder": "Only set if the sandbox is auth-protected", "required": False},
                {"key": "default_timeout", "label": "Default Timeout (seconds)", "type": "number", "default": 30, "min": 5, "max": 120},
                {"key": "max_timeout", "label": "Max Timeout (seconds)", "type": "number", "default": 60, "min": 10, "max": 120},
            ]
        },
    },
    {
        "name": "web_eval",
        "display_name": "Run JS in Page",
        "description": (
            "Run arbitrary async JavaScript in the conversation's persistent browser page "
            "(opened via web_open) and return the result. Use this to read or manipulate the "
            "DOM, click elements (el.click()), fill inputs, scroll, await fetch(...), or "
            "extract structured data. The value of the expression (or an awaited promise) is "
            "returned, JSON-serialized. Example: `document.querySelector('h1').innerText`."
        ),
        "category": "browser",
        "icon": "🧩",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "JavaScript to evaluate in the page. May use await. The last expression's value is returned."},
            },
            "required": ["expression"],
        },
        "config": {
            "sandbox_type": "aio_sandbox",
            "api_url": "http://aio-sandbox:8080",
            "api_key": "",
            "default_timeout": 30,
            "max_timeout": 60,
        },
        "config_schema": {
            "fields": [
                {"key": "api_url", "label": "Sandbox URL", "type": "text", "default": "http://aio-sandbox:8080", "placeholder": "http://aio-sandbox:8080", "required": True},
                {"key": "api_key", "label": "Bearer Token (optional)", "type": "password", "default": "", "placeholder": "Only set if the sandbox is auth-protected", "required": False},
                {"key": "default_timeout", "label": "Default Timeout (seconds)", "type": "number", "default": 30, "min": 5, "max": 120},
                {"key": "max_timeout", "label": "Max Timeout (seconds)", "type": "number", "default": 60, "min": 10, "max": 120},
            ]
        },
    },
    {
        "name": "web_cdp",
        "display_name": "Raw CDP",
        "description": (
            "Send a raw Chrome DevTools Protocol command to the conversation's persistent "
            "browser page and return the result. This exposes the full low-level browser "
            "surface: Input.dispatchMouseEvent / dispatchKeyEvent (OS-trusted clicks & typing "
            "for anti-bot), Page.* (navigate/captureScreenshot/printToPDF), Network.* "
            "(setCookie / request interception), Emulation.* (user-agent / viewport / geo / "
            "timezone), Fetch.*, DOM.*, etc. The command is scoped to THIS conversation's page; "
            "browser-global methods (Target.* / Browser.*) are rejected to keep conversations "
            "isolated. Pass `method` (e.g. 'Input.dispatchKeyEvent') and `params` (an object)."
        ),
        "category": "browser",
        "icon": "🛠️",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "description": "CDP method name, e.g. 'Input.dispatchMouseEvent' or 'Page.navigate'."},
                "params": {"type": "object", "description": "CDP method params object. Optional; defaults to {}."},
            },
            "required": ["method"],
        },
        "config": {
            "sandbox_type": "aio_sandbox",
            "api_url": "http://aio-sandbox:8080",
            "api_key": "",
            "default_timeout": 30,
            "max_timeout": 60,
        },
        "config_schema": {
            "fields": [
                {"key": "api_url", "label": "Sandbox URL", "type": "text", "default": "http://aio-sandbox:8080", "placeholder": "http://aio-sandbox:8080", "required": True},
                {"key": "api_key", "label": "Bearer Token (optional)", "type": "password", "default": "", "placeholder": "Only set if the sandbox is auth-protected", "required": False},
                {"key": "default_timeout", "label": "Default Timeout (seconds)", "type": "number", "default": 30, "min": 5, "max": 120},
                {"key": "max_timeout", "label": "Max Timeout (seconds)", "type": "number", "default": 60, "min": 10, "max": 120},
            ]
        },
    },
    {
        "name": "web_screenshot",
        "display_name": "Screenshot Page",
        "description": (
            "Capture a PNG screenshot of the conversation's persistent browser page (opened "
            "via web_open) and save it to your workspace. Use after interacting with the page "
            "to see its current state. Does not navigate or change the page."
        ),
        "category": "browser",
        "icon": "📸",
        "is_default": False,
        "parameters_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "config": {
            "sandbox_type": "aio_sandbox",
            "api_url": "http://aio-sandbox:8080",
            "api_key": "",
            "default_timeout": 30,
            "max_timeout": 60,
        },
        "config_schema": {
            "fields": [
                {"key": "api_url", "label": "Sandbox URL", "type": "text", "default": "http://aio-sandbox:8080", "placeholder": "http://aio-sandbox:8080", "required": True},
                {"key": "api_key", "label": "Bearer Token (optional)", "type": "password", "default": "", "placeholder": "Only set if the sandbox is auth-protected", "required": False},
                {"key": "default_timeout", "label": "Default Timeout (seconds)", "type": "number", "default": 30, "min": 5, "max": 120},
                {"key": "max_timeout", "label": "Max Timeout (seconds)", "type": "number", "default": 60, "min": 10, "max": 120},
            ]
        },
    },
]
