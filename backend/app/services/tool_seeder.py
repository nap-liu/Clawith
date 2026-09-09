"""Seed builtin tools into the database on startup."""

from loguru import logger
from sqlalchemy import select

from app.config import get_settings
from app.core.okr_feature import OKR_TOOL_NAMES, okr_feature_enabled
from app.core.plaza_feature import PLAZA_TOOL_NAMES
from app.database import async_session
from app.models.tenant import Tenant
from app.models.tenant_setting import TenantSetting
from app.models.tool import AgentTool, Tool
from app.models.project import ProjectCapabilityBinding
from app.services.mcp_catalog_locks import lock_mcp_catalogs
from app.services.mcp_catalog_policy import can_remove_private_tool
from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_SEED
from app.services.media_tool_contract import SEND_MEDIA_TOOL_SEED
from app.services.tool_config import meaningful_config, tenant_tool_config_key
from app.services.tool_enablement import tool_is_required
from app.services.user_project_tools import USER_PROJECT_TOOL_NAMES, USER_PROJECT_TOOL_SEEDS

_settings = get_settings()

SYNC_IS_DEFAULT_TOOL_NAMES = {
    "update_self_settings",
    "run_subagent",
    "send_message_to_subagent",
    "stop_subagent",
    "send_message_to_parent",
    "execute_code",
    "execute_code_aio",
    "read_image",
    "read_webpage",
    "duckduckgo_search",
    "jina_search",
    "jina_read",
    "update_objective",
    "list_installed_mcp_servers",
    "refresh_mcp_server",
    "uninstall_mcp_server",
    # Keep request_confirmation allocated consistently for existing and new agents.
    "request_confirmation",
    # Scene management is opt-in and must remain opt-in on existing databases.
    "manage_scene",
    # AgentBay tools should NOT be is_default=True. Older seeder versions may
    # have set them to True; include them here so the seeder corrects the DB.
    "agentbay_browser_navigate",
    "agentbay_browser_screenshot",
    "agentbay_browser_save_screenshot",
    "agentbay_browser_click",
    "agentbay_browser_type",
    "agentbay_browser_extract",
    "agentbay_browser_observe",
    "agentbay_browser_login",
    "agentbay_code_execute",
    "agentbay_code_write_file",
    "agentbay_code_read_file",
    "agentbay_code_edit_file",
    "agentbay_command_exec",
    "agentbay_computer_screenshot",
    "agentbay_computer_save_screenshot",
    "agentbay_computer_click",
    "agentbay_computer_precision_screenshot",
    "agentbay_computer_input_text",
    "agentbay_computer_press_keys",
    "agentbay_computer_scroll",
    "agentbay_computer_move_mouse",
    "agentbay_computer_drag_mouse",
    "agentbay_computer_get_installed_apps",
    "agentbay_computer_start_app",
    "agentbay_computer_list_windows",
    "agentbay_computer_close_window",
    "agentbay_computer_dismiss_dialog",
    "agentbay_file_transfer",
    # User project tools are manually enabled and must stay opt-in when an old
    # database is re-seeded.
    *USER_PROJECT_TOOL_NAMES,
}

# AgentBay is retained in the codebase for compatibility with historical data,
# but the product capability is globally disabled.  Keep this as a category
# invariant rather than relying on a one-off production database edit: a fresh
# database or a restored old dump must seed/sync every AgentBay tool disabled.
FORCE_DISABLED_BUILTIN_CATEGORIES = {"agentbay"}
FORCE_DISABLED_BUILTIN_TOOL_NAMES = PLAZA_TOOL_NAMES


def builtin_tool_forced_disabled(seed: dict) -> bool:
    return bool(
        seed.get("category") in FORCE_DISABLED_BUILTIN_CATEGORIES
        or seed.get("name") in FORCE_DISABLED_BUILTIN_TOOL_NAMES
        or (not okr_feature_enabled() and seed.get("name") in OKR_TOOL_NAMES)
    )


def builtin_tool_enabled(seed: dict) -> bool:
    """Return the canonical global enabled state for a builtin seed."""
    if builtin_tool_forced_disabled(seed):
        return False
    return bool(seed.get("enabled", True))


def should_sync_builtin_default(seed: dict) -> bool:
    """Whether an existing row must follow the seed's default flag."""
    return (
        seed.get("name") in SYNC_IS_DEFAULT_TOOL_NAMES
        or builtin_tool_forced_disabled(seed)
    )


LEGACY_IMAGE_TOOL_MODEL_DEFAULTS = {
    "generate_image_siliconflow": "black-forest-labs/FLUX.1-schnell",
    "generate_image_openai": "dall-e-3",
    "generate_image_google": "gemini-2.5-flash-image",
}


def _global_builtin_config(tool_data: dict) -> dict:
    """Return config safe to store on the global builtin Tool row."""
    # Builtin tools specify defaults (like 'allow_network': True) in their 'config' dict.
    # The actual sensitive data defaults are empty strings ("") so this is safe to store globally.
    return tool_data.get("config", {})

# Builtin tool definitions — these map to the hardcoded AGENT_TOOLS
from app.services.tool_seeder_builtin_1 import BUILTIN_TOOLS_PART_1
from app.services.tool_seeder_builtin_2 import BUILTIN_TOOLS_PART_2
from app.services.tool_seeder_builtin_3 import BUILTIN_TOOLS_PART_3
from app.services.tool_seeder_builtin_4 import BUILTIN_TOOLS_PART_4
from app.services.tool_seeder_builtin_5 import BUILTIN_TOOLS_PART_5
from app.services.tool_seeder_builtin_6 import BUILTIN_TOOLS_PART_6

BUILTIN_TOOLS = [
    *BUILTIN_TOOLS_PART_1,
    *BUILTIN_TOOLS_PART_2,
    *BUILTIN_TOOLS_PART_3,
    *BUILTIN_TOOLS_PART_4,
    *BUILTIN_TOOLS_PART_5,
    *BUILTIN_TOOLS_PART_6,
]

# ── AgentBay Tools ──────────────────────────────────────────────────────────

from app.services.tool_seeder_agentbay import AGENTBAY_TOOLS

BUILTIN_TOOLS = [
    *BUILTIN_TOOLS,
    # ── AgentBay Tools ──
    *AGENTBAY_TOOLS,
]

# ── OKR Tools ────────────────────────────────────────────────────────────────
# These three tools are global builtins available to ALL agents.
# OKR Agent-exclusive management tools (create_objective, create_key_result, etc.)
# are injected separately via agent_seeder when the OKR Agent is created.

from app.services.tool_seeder_extra_catalogs import (
    BROWSER_BUILTIN_TOOLS,
    DEPLOY_BUILTIN_TOOLS,
    OKR_BUILTIN_TOOLS,
)

BUILTIN_TOOLS = [
    *BUILTIN_TOOLS,
    *OKR_BUILTIN_TOOLS,
    *DEPLOY_BUILTIN_TOOLS,
    *BROWSER_BUILTIN_TOOLS,
    *USER_PROJECT_TOOL_SEEDS,
]


async def seed_builtin_tools():
    """Insert or update builtin tools in the database."""
    from app.models.agent import Agent
    from app.models.tool import AgentTool

    async with async_session() as db:
        # Rename or merge persisted builtin tools in place so existing Agent
        # assignments survive a public tool-name cleanup.
        for old_name, new_name in (
            ("send_web_message", "send_platform_message"),
            ("reassign_background_execution_user", "set_execution_user"),
        ):
            old_tool = (
                await db.execute(select(Tool).where(Tool.name == old_name))
            ).scalar_one_or_none()
            new_tool = (
                await db.execute(select(Tool).where(Tool.name == new_name))
            ).scalar_one_or_none()
            if old_tool and not new_tool:
                old_tool.name = new_name
                logger.info(f"[ToolSeeder] Renamed builtin tool: {old_name} -> {new_name}")
            elif old_tool and new_tool:
                old_assignments = await db.execute(
                    select(AgentTool).where(AgentTool.tool_id == old_tool.id)
                )
                for assignment in old_assignments.scalars().all():
                    existing_assignment = await db.execute(
                        select(AgentTool).where(
                            AgentTool.agent_id == assignment.agent_id,
                            AgentTool.tool_id == new_tool.id,
                        )
                    )
                    if not existing_assignment.scalar_one_or_none():
                        assignment.tool_id = new_tool.id
                await db.delete(old_tool)
                logger.info(f"[ToolSeeder] Merged legacy builtin tool into {new_name}")

        new_tool_ids = []
        required_tool_ids = set()
        for t in BUILTIN_TOOLS:
            seed_config = _global_builtin_config(t)
            seed_enabled = builtin_tool_enabled(t)
            seed_is_default = False if builtin_tool_forced_disabled(t) else t["is_default"]
            result = await db.execute(select(Tool).where(Tool.name == t["name"]))
            existing = result.scalar_one_or_none()
            if not existing:
                tool = Tool(
                    name=t["name"],
                    display_name=t["display_name"],
                    description=t["description"],
                    type="builtin",
                    category=t["category"],
                    icon=t["icon"],
                    enabled=seed_enabled,
                    is_default=seed_is_default,
                    parameters_schema=t.get("parameters_schema", {"type": "object", "properties": {}}),
                    config=seed_config,
                    config_schema=t.get("config_schema", {}),
                    source="builtin",
                )
                db.add(tool)
                await db.flush()  # get tool.id
                if tool_is_required(t["name"]):
                    required_tool_ids.add(tool.id)
                if seed_is_default:
                    new_tool_ids.append(tool.id)
                logger.info(f"[ToolSeeder] Created builtin tool: {t['name']}")
            else:
                # Sync fields that may evolve
                updated_fields = []
                if tool_is_required(t["name"]):
                    required_tool_ids.add(existing.id)
                    if not existing.enabled:
                        existing.enabled = True
                        updated_fields.append("enabled")
                if existing.category != t["category"]:
                    existing.category = t["category"]
                    updated_fields.append("category")
                if existing.description != t["description"]:
                    existing.description = t["description"]
                    updated_fields.append("description")
                if existing.display_name != t["display_name"]:
                    existing.display_name = t["display_name"]
                    updated_fields.append("display_name")
                if existing.icon != t["icon"]:
                    existing.icon = t["icon"]
                    updated_fields.append("icon")
                if should_sync_builtin_default(t) and existing.is_default != seed_is_default:
                    existing.is_default = seed_is_default
                    updated_fields.append("is_default")
                    # A builtin that becomes default-on must receive explicit
                    # assignments for existing Agents too. Existing rows,
                    # including manual opt-outs, are preserved below.
                    if seed_is_default:
                        new_tool_ids.append(existing.id)
                if (
                    builtin_tool_forced_disabled(t)
                    and existing.enabled != seed_enabled
                ):
                    existing.enabled = seed_enabled
                    updated_fields.append("enabled")
                if t.get("config_schema") and existing.config_schema != t["config_schema"]:
                    existing.config_schema = t["config_schema"]
                    updated_fields.append("config_schema")
                    # Merge new config defaults when config_schema changes
                    if seed_config:
                        existing.config = {**seed_config, **(existing.config or {})}
                        updated_fields.append("config")
                if not existing.config and seed_config:
                    existing.config = seed_config
                    updated_fields.append("config")
                elif seed_config and existing.config != seed_config:
                    # Merge new config keys into existing config so that flags like
                    # okr_agent_only are propagated to already-created tool records.
                    # Existing keys take precedence (agent-specific overrides are preserved).
                    merged = {**seed_config, **(existing.config or {})}
                    if merged != existing.config:
                        existing.config = merged
                        updated_fields.append("config")
                legacy_model = LEGACY_IMAGE_TOOL_MODEL_DEFAULTS.get(t["name"])
                if legacy_model and existing.config == {
                    "model": legacy_model,
                    "api_key": "",
                    "base_url": "",
                }:
                    existing.config = {
                        "model": "",
                        "api_key": "",
                        "base_url": "",
                    }
                    updated_fields.append("config")
                if existing.parameters_schema != t["parameters_schema"]:
                    existing.parameters_schema = t["parameters_schema"]
                    updated_fields.append("parameters_schema")
                if updated_fields:
                    logger.info(f"[ToolSeeder] Updated {', '.join(updated_fields)}: {t['name']}")

        # Auto-assign new default tools to standard Agents only. Project Agents
        # have an explicit project-local allowlist and must not inherit future
        # platform defaults during startup. Required protocol tools still
        # self-heal for every Agent regardless of scope.
        assignment_tool_ids = set(new_tool_ids) | required_tool_ids
        if assignment_tool_ids:
            agents_result = await db.execute(select(Agent.id, Agent.scope))
            agent_rows = agents_result.fetchall()
            assignments_result = await db.execute(
                select(AgentTool).where(AgentTool.tool_id.in_(assignment_tool_ids))
            )
            assignments_by_pair = {
                (assignment.agent_id, assignment.tool_id): assignment
                for assignment in assignments_result.scalars().all()
            }
            ensured_count = 0
            for agent_id, agent_scope in agent_rows:
                applicable_tool_ids = set(required_tool_ids)
                if agent_scope == "standard":
                    applicable_tool_ids.update(new_tool_ids)
                for tool_id in applicable_tool_ids:
                    assignment = assignments_by_pair.get((agent_id, tool_id))
                    if assignment is None:
                        db.add(AgentTool(agent_id=agent_id, tool_id=tool_id, enabled=True))
                        ensured_count += 1
                    elif tool_id in required_tool_ids and not assignment.enabled:
                        assignment.enabled = True
                        ensured_count += 1
            logger.info(
                f"[ToolSeeder] Ensured {ensured_count} new/required assignments "
                f"across {len(agent_rows)} agents"
            )

        OBSOLETE_TOOLS = ["bing_search", "manage_tasks"]
        for obsolete_name in OBSOLETE_TOOLS:
            result = await db.execute(select(Tool).where(Tool.name == obsolete_name))
            obsolete = result.scalar_one_or_none()
            if obsolete:
                await db.delete(obsolete)
                logger.info(f"[ToolSeeder] Removed obsolete tool: {obsolete_name}")

        # Legacy deployments stored company credentials for builtin tools in
        # the global tools.config row. Move those values into the first tenant's
        # tenant_settings once, then clear the global row so new companies do
        # not inherit another company's keys.
        first_tenant_r = await db.execute(select(Tenant).order_by(Tenant.created_at).limit(1))
        first_tenant = first_tenant_r.scalar_one_or_none()
        if first_tenant:
            builtin_config_tools_r = await db.execute(select(Tool).where(Tool.source == "builtin"))
            migrated = 0
            for tool in builtin_config_tools_r.scalars().all():
                if not (tool.config_schema or {}).get("fields"):
                    continue
                legacy_config = meaningful_config(tool.config or {})
                if not legacy_config:
                    continue
                setting_key = tenant_tool_config_key(tool.name)
                existing_setting_r = await db.execute(
                    select(TenantSetting).where(
                        TenantSetting.tenant_id == first_tenant.id,
                        TenantSetting.key == setting_key,
                    )
                )
                if not existing_setting_r.scalar_one_or_none():
                    db.add(TenantSetting(
                        tenant_id=first_tenant.id,
                        key=setting_key,
                        value={"config": legacy_config},
                    ))
                    migrated += 1
                
                # Remove sensitive fields from global config instead of wiping it
                clean_config = {}
                schema_fields = (tool.config_schema or {}).get("fields", [])
                sensitive_keys = {f["key"] for f in schema_fields if f.get("type") == "password"}
                for k, v in (tool.config or {}).items():
                    if k not in sensitive_keys:
                        clean_config[k] = v
                tool.config = clean_config
            if migrated:
                logger.info(
                    f"[ToolSeeder] Migrated {migrated} legacy builtin tool config(s) "
                    f"to tenant_settings for tenant {first_tenant.id}"
                )

        await db.commit()
        logger.info("[ToolSeeder] Builtin tools seeded")


async def clean_orphaned_mcp_tools():
    """Remove proven private orphans; shared and unclassified catalogs survive."""
    async with async_session() as db:
        candidates = (await db.execute(select(Tool.id, Tool.mcp_server_id).where(
            Tool.type == "mcp", Tool.source == "agent",
            ~select(AgentTool.id).where(AgentTool.tool_id == Tool.id).exists(),
        ))).all()
        await lock_mcp_catalogs(
            db, sorted({server_id for _, server_id in candidates if server_id}),
            tool_ids=[tool_id for tool_id, _ in candidates],
        )
        # Re-read after acquiring the same locks as assignment/refresh writers.
        deleted_count = 0
        for tool_id, server_id in candidates:
            tool = await db.get(Tool, tool_id, populate_existing=True)
            if tool is None or not await can_remove_private_tool(db, tool):
                continue
            if await db.scalar(select(AgentTool.id).where(AgentTool.tool_id == tool_id).limit(1)):
                continue
            if server_id and await db.scalar(select(ProjectCapabilityBinding.id).where(
                ProjectCapabilityBinding.capability_type == "mcp",
                ProjectCapabilityBinding.capability_id == server_id,
            ).limit(1)):
                continue
            await db.delete(tool)
            deleted_count += 1
        await db.commit()
        if deleted_count > 0:
            logger.info(f"[ToolSeeder] Cleaned up {deleted_count} orphaned MCP tools")

# ── Atlassian Rovo MCP Server Integration ──────────────────────────────────

ATLASSIAN_ROVO_MCP_URL = "https://mcp.atlassian.com/v1/mcp"

ATLASSIAN_ROVO_CONFIG_TOOL = {
    "name": "atlassian_rovo",
    "display_name": "Atlassian Rovo (Jira / Confluence / Compass)",
    "description": (
        "Connect to Atlassian Rovo MCP Server to access Jira, Confluence, and Compass. "
        "Configure your API key to enable Jira issue management, Confluence page creation, "
        "and Compass component queries."
    ),
    "category": "atlassian",
    "icon": "🔷",
    "is_default": False,
    "parameters_schema": {"type": "object", "properties": {}},
    "config": {"api_key": ""},
    "config_schema": {
        "fields": [
            {
                "key": "api_key",
                "label": "Atlassian API Key",
                "type": "password",
                "default": "",
                "placeholder": "ATSTT3x... (service account key) or Basic base64(email:token)",
                "description": (
                    "Service account API key (Bearer) or base64-encoded email:api_token (Basic). "
                    "Get your API key from id.atlassian.com/manage-profile/security/api-tokens"
                ),
            },
        ]
    },
}


async def seed_atlassian_rovo_config():
    """Ensure the Atlassian Rovo platform config tool exists in the database.

    If the env var ATLASSIAN_API_KEY is set, it will be written into the tool config
    so the platform is immediately ready without manual UI setup.
    """
    import os
    env_key = os.environ.get("ATLASSIAN_API_KEY", "").strip()

    async with async_session() as db:
        t = ATLASSIAN_ROVO_CONFIG_TOOL
        result = await db.execute(select(Tool).where(Tool.name == t["name"]))
        existing = result.scalar_one_or_none()
        if not existing:
            initial_config = dict(t["config"])
            if env_key:
                initial_config["api_key"] = env_key
            tool = Tool(
                name=t["name"],
                display_name=t["display_name"],
                description=t["description"],
                type="mcp_config",
                category=t["category"],
                icon=t["icon"],
                is_default=t["is_default"],
                parameters_schema=t["parameters_schema"],
                config=initial_config,
                config_schema=t["config_schema"],
                mcp_server_url=ATLASSIAN_ROVO_MCP_URL,
                mcp_server_name="Atlassian Rovo",
                source="admin",
            )
            db.add(tool)
            await db.commit()
            logger.info("[ToolSeeder] Created Atlassian Rovo config tool")
        else:
            updated = False
            if existing.config_schema != t["config_schema"]:
                existing.config_schema = t["config_schema"]
                updated = True
            if existing.mcp_server_url != ATLASSIAN_ROVO_MCP_URL:
                existing.mcp_server_url = ATLASSIAN_ROVO_MCP_URL
                updated = True
            # Write env key into DB if not already stored
            if env_key and (not existing.config or not existing.config.get("api_key")):
                existing.config = {**(existing.config or {}), "api_key": env_key}
                updated = True
            if updated:
                await db.commit()
                logger.info("[ToolSeeder] Updated Atlassian Rovo config tool")


async def get_atlassian_api_key() -> str:
    """Read the Atlassian API key from the platform config tool."""
    async with async_session() as db:
        result = await db.execute(select(Tool).where(Tool.name == "atlassian_rovo"))
        tool = result.scalar_one_or_none()
        if tool and tool.config:
            return tool.config.get("api_key", "")
    return ""
