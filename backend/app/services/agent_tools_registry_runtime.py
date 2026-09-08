from __future__ import annotations

from types import SimpleNamespace
import uuid

from loguru import logger
from sqlalchemy import or_, select

from app.core.okr_feature import OKR_TOOL_NAMES, is_retired_okr_tool, okr_feature_enabled
from app.database import async_session
from app.services.agent_tools_catalog import AGENT_TOOLS
from app.services.agent_tools_config_runtime import _get_tool_config
from app.services.turn_tool_settings import current_tool_settings
from app.services.tool_enablement import tool_visibility_clause
from app.services.mcp_naming import load_mcp_display_names, model_mcp_description

_ALWAYS_INCLUDE_CORE = {
    "add_contact",
    "complete_focus_item",
    "list_focus_items",
    "remove_contact",
    "search_contacts",
    "send_channel_file",
    "send_media",
    "send_file_to_agent",
    "upsert_focus_item",
    "write_file",
}
_CHANNEL_MESSAGE_TOOL_NAMES = {
    "recall_message",
    "send_channel_message",
    "send_group_session_message",
    "send_session_message",
}
_FEISHU_TOOL_NAMES = {
    "send_feishu_message",
    "feishu_user_search",
    "bitable_create_app",
    "bitable_list_tables",
    "bitable_list_fields",
    "bitable_query_records",
    "bitable_create_record",
    "bitable_update_record",
    "bitable_delete_record",
    "feishu_doc_search",
    "feishu_wiki_list",
    "feishu_doc_read",
    "feishu_doc_create",
    "feishu_doc_append",
    "feishu_drive_share",
    "feishu_drive_delete",
    "feishu_calendar_list",
    "feishu_calendar_create",
    "feishu_calendar_update",
    "feishu_calendar_delete",
    "feishu_approval_create",
    "feishu_approval_query",
    "feishu_approval_get",
}
_FIXED_MEDIA_TOOL_NAMES = ("send_media",)
_LEGACY_MEDIA_TOOL_NAMES = frozenset({"send_audio", "send_video"})


def _build_always_tool_subsets(agent_tools: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    return (
        [t for t in agent_tools if t["function"]["name"] in _ALWAYS_INCLUDE_CORE],
        [t for t in agent_tools if t["function"]["name"] in _FEISHU_TOOL_NAMES],
        [t for t in agent_tools if t["function"]["name"] in _CHANNEL_MESSAGE_TOOL_NAMES],
    )


_always_core_tools, _feishu_tools, _channel_tools = _build_always_tool_subsets(AGENT_TOOLS)


def _stabilize_media_tool_definitions_impl(tools: list[dict], agent_tools: list[dict]) -> list[dict]:
    canonical = {item["function"]["name"]: item for item in agent_tools if item["function"]["name"] in _FIXED_MEDIA_TOOL_NAMES}
    stable = [
        item
        for item in tools
        if item.get("function", {}).get("name") not in {*_FIXED_MEDIA_TOOL_NAMES, *_LEGACY_MEDIA_TOOL_NAMES}
    ]
    stable.extend(canonical[name] for name in _FIXED_MEDIA_TOOL_NAMES)
    return stable


async def _get_computer_os_type(agent_id: uuid.UUID) -> str:
    """Return the configured OS type for the agent's computer tool.

    Reads from agentbay_browser_navigate tool config (which stores all AgentBay
    settings including os_type). Defaults to 'windows' to match AgentBay's default.
    """
    try:
        config = await _get_tool_config(agent_id, "agentbay_browser_navigate")
        return (config or {}).get("os_type", "windows")
    except Exception:
        return "windows"


def _patch_computer_tool_descriptions(tools: list[dict], os_type: str) -> list[dict]:
    """Rewrite path examples in agentbay_file_transfer to match the agent's OS.

    This ensures the Agent always sees the correct desktop and home-directory
    paths for its specific computer environment without having to guess.
    """
    import copy

    if os_type == "windows":
        # Windows paths used by AgentBay's windows_latest image
        desktop_path = r"C:\Users\Administrator\Desktop"
        home_path = r"C:\Users\Administrator"
        computer_os_label = "Windows"
    else:
        # Linux paths used by AgentBay's linux_latest image
        desktop_path = "/home/wuying/Desktop"
        home_path = "/home/wuying"
        computer_os_label = "Linux"

    # Build the OS-aware description for agentbay_file_transfer
    new_file_transfer_desc = (
        (
            "Transfer a file between any two endpoints: the agent workspace, "
            "the AgentBay browser environment, the cloud desktop (computer), or the code sandbox.\n\n"
            f"COMPUTER ENVIRONMENT OS: {computer_os_label}\n"
            f"VERIFIED PATH CONVENTIONS for the computer environment ({computer_os_label}):\n"
            f"- computer desktop: {desktop_path}\\<filename>  (e.g. {desktop_path}\\report.xlsx)\n"
            f"- computer home:    {home_path}\\<filename>\n\n"
            "Other environments (Linux-based, user 'wuying', HOME=/home/wuying/):\n"
            "- code env:     /home/wuying/<filename>  (e.g. /home/wuying/data.csv)\n"
            "- browser env:  /home/wuying/下载/<filename>  (download folder)\n"
            "- workspace:    relative path, e.g. 'workspace/data.csv'\n\n"
            "Transfer directions:\n"
            "- workspace -> env: upload a workspace file into a cloud environment\n"
            "- env -> workspace: download a file from a cloud environment into the workspace\n"
            "- env A -> env B:   transfer between environments (transparent backend temp)"
        )
        if os_type == "windows"
        else (
            "Transfer a file between any two endpoints: the agent workspace, "
            "the AgentBay browser environment, the cloud desktop (computer), or the code sandbox.\n\n"
            f"COMPUTER ENVIRONMENT OS: {computer_os_label}\n"
            f"VERIFIED PATH CONVENTIONS for the computer environment ({computer_os_label}):\n"
            f"- computer desktop: {desktop_path}/<filename>  (e.g. {desktop_path}/report.xlsx)\n"
            f"- computer home:    {home_path}/<filename>\n\n"
            "Other environments (also Linux, user 'wuying'):\n"
            "- code env:     /home/wuying/<filename>  (e.g. /home/wuying/data.csv)\n"
            "- browser env:  /home/wuying/下载/<filename>  (download folder)\n"
            "- workspace:    relative path, e.g. 'workspace/data.csv'\n\n"
            "Transfer directions:\n"
            "- workspace -> env: upload a workspace file into a cloud environment\n"
            "- env -> workspace: download a file from a cloud environment into the workspace\n"
            "- env A -> env B:   transfer between environments (transparent backend temp)"
        )
    )

    patched = []
    for tool in tools:
        fn = tool.get("function", {})
        name = fn.get("name", "")
        if name == "agentbay_file_transfer":
            # Deep copy to avoid mutating the shared AGENT_TOOLS constant
            tool = copy.deepcopy(tool)
            tool["function"]["description"] = new_file_transfer_desc
            # Also patch from_path and to_path parameter hints
            props = tool["function"].get("parameters", {}).get("properties", {})
            if "from_path" in props:
                if os_type == "windows":
                    props["from_path"]["description"] = (
                        r"Source path. Relative if workspace (e.g. 'workspace/data.csv'). "
                        r"Absolute if env: computer → C:\Users\Administrator\Desktop\file, "
                        r"code → /home/wuying/file, browser → /home/wuying/下载/file."
                    )
                else:
                    props["from_path"]["description"] = (
                        "Source path. Relative if workspace (e.g. 'workspace/data.csv'). "
                        "Absolute if env: computer → /home/wuying/Desktop/file, "
                        "code → /home/wuying/file, browser → /home/wuying/下载/file."
                    )
            if "to_path" in props:
                if os_type == "windows":
                    props["to_path"]["description"] = (
                        r"Destination path. Relative if workspace (e.g. 'workspace/output.csv'). "
                        r"Absolute if env: computer → C:\Users\Administrator\Desktop\file, "
                        r"code → /home/wuying/file, browser → /home/wuying/下载/file."
                    )
                else:
                    props["to_path"]["description"] = (
                        "Destination path. Relative if workspace (e.g. 'workspace/output.csv'). "
                        "Absolute if env: computer → /home/wuying/Desktop/file, "
                        "code → /home/wuying/file, browser → /home/wuying/下载/file."
                    )
        patched.append(tool)
    return patched


async def _agent_has_feishu(agent_id: uuid.UUID) -> bool:
    """Check if agent has a configured Feishu channel."""
    try:
        from app.models.channel_config import ChannelConfig

        async with async_session() as db:
            r = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "feishu",
                    ChannelConfig.is_configured == True,
                )
            )
            return r.scalar_one_or_none() is not None
    except Exception:
        return False


async def _agent_has_any_channel(agent_id: uuid.UUID) -> bool:
    """Check if agent has any configured channel (Feishu/DingTalk/WeCom)."""
    try:
        from app.models.channel_config import ChannelConfig

        async with async_session() as db:
            r = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.is_configured == True,
                )
            )
            return r.scalar_one_or_none() is not None
    except Exception:
        return False


def _strip_a2a_msg_type(tools: list[dict]) -> list[dict]:
    """Remove the msg_type parameter from send_message_to_agent when async A2A is disabled.

    This prevents the LLM from seeing and selecting notify/task_delegate modes
    that would be silently overridden to consult anyway, which confuses users
    who see the tool call arguments in the chat UI.
    """
    import copy

    result = []
    for t in tools:
        fn = t.get("function", {})
        if fn.get("name") == "send_message_to_agent":
            t = copy.deepcopy(t)
            fn = t["function"]
            # Simplify description to only mention consult, but keep the RESET hint
            # so agents still discover the new_conversation self-reset escape hatch.
            fn["description"] = (
                "Send a message to a digital employee colleague and receive their reply synchronously.\n\n"
                "RESET: If the conversation with a colleague gets stuck — the same tool failing over and "
                "over, repeated identical errors, looping, or visibly corrupted/garbled context — set "
                "new_conversation=true to discard the stale history and start a fresh, clean thread."
            )
            params = fn.get("parameters", {})
            props = params.get("properties", {})
            # Remove msg_type parameter entirely
            props.pop("msg_type", None)
            # Remove msg_type from required list
            req = params.get("required", [])
            if "msg_type" in req:
                params["required"] = [r for r in req if r != "msg_type"]
        result.append(t)
    return result


async def _get_agent_tools_for_llm_impl(
    agent_id: uuid.UUID,
    *,
    agent_tools: list[dict],
    get_tool_config,
    assignment_snapshot: list[dict] | None = None,
) -> list[dict]:
    scope = current_tool_settings(agent_id)
    if assignment_snapshot is None and scope is not None:
        assignment_snapshot = scope.assignments
    has_feishu = await _agent_has_feishu(agent_id)
    has_any_channel = await _agent_has_any_channel(agent_id)
    always_core_tools, feishu_tools, channel_tools = _build_always_tool_subsets(agent_tools)
    _always_tools = always_core_tools + (feishu_tools if has_feishu else []) + (channel_tools if has_any_channel else [])

    _a2a_async = False
    is_system_agent = False
    agent_tenant_id = None
    try:
        from app.models.agent import Agent as AgentModel
        from app.models.tenant import Tenant

        async with async_session() as flag_db:
            agent_row = (await flag_db.execute(select(AgentModel).where(AgentModel.id == agent_id))).scalar_one_or_none()
            agent_tenant_id = agent_row.tenant_id if agent_row else None
            is_system_agent = bool(agent_row and agent_row.is_system)
            if agent_tenant_id:
                tenant = (await flag_db.execute(select(Tenant).where(Tenant.id == agent_tenant_id))).scalar_one_or_none()
                if tenant:
                    _a2a_async = getattr(tenant, "a2a_async_enabled", False)
    except Exception:
        pass

    computer_os_type = "windows"

    try:
        from app.core.plaza_feature import PLAZA_TOOL_NAMES
        from app.models.tool import AgentTool, Tool
        from app.services.tool_enablement import REQUIRED_AGENT_TOOL_NAMES, resolved_agent_tool_enabled, tool_is_required

        async with async_session() as db:
            if assignment_snapshot is None:
                agent_tools_rows = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))
                assignments = {str(at.tool_id): at for at in agent_tools_rows.scalars().all()}
            else:
                assignments = {
                    str(item["tool_id"]): SimpleNamespace(enabled=True, config=dict(item.get("config") or {}))
                    for item in assignment_snapshot
                    if isinstance(item, dict) and item.get("tool_id")
                }
            assigned_tool_ids = [uuid.UUID(tool_id) for tool_id in assignments]

            tool_clauses = [
                or_(Tool.enabled == True, Tool.name.in_(REQUIRED_AGENT_TOOL_NAMES)),
                tool_visibility_clause(agent_tenant_id, assigned_tool_ids),
            ]
            if not okr_feature_enabled():
                tool_clauses.append(Tool.name.not_in(OKR_TOOL_NAMES))
            tool_clauses.append(Tool.name.not_in(PLAZA_TOOL_NAMES))
            all_tools = (await db.execute(select(Tool).where(*tool_clauses))).scalars().all()
            mcp_display_names = await load_mcp_display_names(db, all_tools)

            from app.services.cli_tools.sandbox_inject import _TOOL_NAME_RE

            result = []
            db_tool_names = set()
            explicitly_disabled_names = set()
            for t in all_tools:
                if is_retired_okr_tool(t.name) or t.name == "send_message_to_parent":
                    continue
                tid = str(t.id)
                at = assignments.get(tid)
                enabled = resolved_agent_tool_enabled(t.name, at)
                if not enabled:
                    if (assignment_snapshot is not None or (at and not at.enabled)) and not tool_is_required(t.name):
                        explicitly_disabled_names.add(t.name)
                    continue

                if t.type == "cli":
                    binary = (t.config or {}).get("binary")
                    if not (isinstance(binary, dict) and binary.get("sha256")):
                        continue
                    always_names = {a["function"]["name"] for a in _always_tools}
                    if not _TOOL_NAME_RE.fullmatch(t.name) or t.name == "toolscall" or t.name in db_tool_names or t.name in always_names:
                        logger.warning(f"[Tools] Skipping CLI tool '{t.name}' (unsafe name, or collides with a builtin/duplicate function)")
                        continue
                    result.append(
                        {
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "description": t.description,
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "command": {
                                            "type": "string",
                                            "description": (
                                                f"完整的 bash 命令行,**必须以程序名 `{t.name}` 开头**"
                                                f"(它是沙箱里一个已注入身份认证的真实命令;具体子命令/"
                                                f"参数见本工具说明)。例如 `{t.name} <参数...>`——不要省略"
                                                f"程序名只写参数。支持管道/重定向等任意 bash 组合,"
                                                f"如 `{t.name} <参数...> | jq '.'`。"
                                            ),
                                        }
                                    },
                                    "required": ["command"],
                                },
                            },
                        }
                    )
                    db_tool_names.add(t.name)
                    continue

                if t.category == "feishu" and not has_feishu:
                    continue
                if (t.config or {}).get("okr_agent_only") and not is_system_agent:
                    continue
                description = t.description
                if t.type == "mcp":
                    description = model_mcp_description(
                        description,
                        mcp_display_names.get(t.mcp_server_id),
                        t.display_name,
                    )
                if t.name == "execute_code_aio":
                    from app.services.toolscall.capability import TOOLSCALL_USAGE_DESCRIPTION, toolscall_enabled_for_agent

                    toolscall_enabled = toolscall_enabled_for_agent(at.config if at else None)
                    if toolscall_enabled:
                        description = str(description or "").rstrip() + TOOLSCALL_USAGE_DESCRIPTION

                tool_def = {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": description,
                        "parameters": t.parameters_schema or {"type": "object", "properties": {}},
                    },
                }
                if t.name in db_tool_names:
                    logger.warning(
                        f"[Tools] Duplicate tool name '{t.name}' found in DB "
                        f"(id={t.id}). Skipping to avoid LLM error. "
                        "Run: DELETE FROM tools WHERE id IN (SELECT id FROM "
                        "(SELECT id, ROW_NUMBER() OVER (PARTITION BY name "
                        "ORDER BY created_at DESC) AS rn FROM tools) t WHERE rn > 1);"
                    )
                    continue
                if t.name in _FIXED_MEDIA_TOOL_NAMES:
                    continue

                result.append(tool_def)
                db_tool_names.add(t.name)

            if result:
                always_added = []
                for t in _always_tools:
                    fn_name = t["function"]["name"]
                    if (assignment_snapshot is None or tool_is_required(fn_name)) and fn_name not in db_tool_names and fn_name not in explicitly_disabled_names:
                        result.append(t)
                        always_added.append(fn_name)
                if always_added:
                    logger.debug(f"[Tools] agent={agent_id} added from _always_tools: {always_added}")
                if "agentbay_file_transfer" in db_tool_names:
                    try:
                        config = await get_tool_config(agent_id, "agentbay_browser_navigate")
                        computer_os_type = (config or {}).get("os_type", "windows")
                    except Exception:
                        computer_os_type = "windows"
                result = _patch_computer_tool_descriptions(result, computer_os_type)
                if not _a2a_async:
                    result = _strip_a2a_msg_type(result)
                result = _stabilize_media_tool_definitions_impl(result, agent_tools)
                final_names = sorted(t["function"]["name"] for t in result)
                logger.info(
                    f"[Tools] agent={agent_id} FINAL {len(result)} tools "
                    f"(assignments={len(assignments)}, disabled={len(explicitly_disabled_names)}): {final_names}"
                )
                return result
            raise ValueError("No tools found for agent in DB")
    except Exception as e:
        logger.error(f"[Tools] DB load failed, using fallback: {e}")

    if assignment_snapshot is not None:
        return _stabilize_media_tool_definitions_impl([], agent_tools)
    fallback = _patch_computer_tool_descriptions(_always_tools, computer_os_type)
    if not _a2a_async:
        fallback = _strip_a2a_msg_type(fallback)
    return _stabilize_media_tool_definitions_impl(fallback, agent_tools)


def _stabilize_media_tool_definitions(tools: list[dict]) -> list[dict]:
    """Keep media tool schemas and relative order independent of channel state."""
    canonical = {
        item["function"]["name"]: item for item in AGENT_TOOLS if item["function"]["name"] in _FIXED_MEDIA_TOOL_NAMES
    }
    stable = [
        item
        for item in tools
        if item.get("function", {}).get("name") not in {*_FIXED_MEDIA_TOOL_NAMES, *_LEGACY_MEDIA_TOOL_NAMES}
    ]
    stable.extend(canonical[name] for name in _FIXED_MEDIA_TOOL_NAMES)
    return stable


async def get_agent_tools_for_llm(
    agent_id: uuid.UUID,
    *,
    assignment_snapshot: list[dict] | None = None,
) -> list[dict]:
    """Load enabled tools for an agent from DB (OpenAI function-calling format).

    Falls back to hardcoded AGENT_TOOLS if DB not ready.
    Includes stable core system tools. Required protocol tools such as
    send_media cannot be disabled; other core tools respect explicit Agent
    tool-panel assignments.
    Feishu tools are only included when the agent has a configured Feishu channel.
    send_channel_message is included when any channel (Feishu/DingTalk/WeCom) is configured.

    Also patches agentbay_file_transfer description with OS-specific paths based on
    the agent's computer tool configuration (os_type: 'windows' | 'linux').

    When the tenant's a2a_async_enabled flag is False, the msg_type parameter is
    removed from the send_message_to_agent tool so the LLM only sees the
    synchronous consult behaviour.
    """
    has_feishu = await _agent_has_feishu(agent_id)
    has_any_channel = await _agent_has_any_channel(agent_id)
    _always_tools = (
        _always_core_tools + (_feishu_tools if has_feishu else []) + (_channel_tools if has_any_channel else [])
    )

    # Check tenant-level a2a_async_enabled flag
    _a2a_async = False
    is_system_agent = False
    agent_tenant_id = None
    try:
        from app.models.tenant import Tenant
        from app.models.agent import Agent as AgentModel

        async with async_session() as _flag_db:
            _ag_r = await _flag_db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            _agent = _ag_r.scalar_one_or_none()
            _tid = _agent.tenant_id if _agent else None
            agent_tenant_id = _tid
            is_system_agent = bool(_agent and _agent.is_system)
            if _tid:
                _t_r = await _flag_db.execute(select(Tenant).where(Tenant.id == _tid))
                _tenant = _t_r.scalar_one_or_none()
                if _tenant:
                    _a2a_async = getattr(_tenant, "a2a_async_enabled", False)
    except Exception:
        pass

    # AgentBay is globally disabled in the current product.  Defaulting here
    # avoids reading its retired config on every ordinary context build (which
    # produced a misleading ERROR even though no AgentBay tool reached the LLM).
    computer_os_type = "windows"

    try:
        from app.core.plaza_feature import PLAZA_TOOL_NAMES
        from app.models.tool import AgentTool, Tool
        from app.services.tool_enablement import (
            REQUIRED_AGENT_TOOL_NAMES,
            resolved_agent_tool_enabled,
            tool_is_required,
        )

        async with async_session() as db:
            # A ProjectRun supplies the exact enabled assignment snapshot. An
            # immediate child without a ProjectRun continues to read live rows.
            if assignment_snapshot is None:
                agent_tools_r = await db.execute(select(AgentTool).where(AgentTool.agent_id == agent_id))
                assignments = {str(at.tool_id): at for at in agent_tools_r.scalars().all()}
            else:
                from types import SimpleNamespace

                assignments = {
                    str(item["tool_id"]): SimpleNamespace(
                        enabled=True,
                        config=dict(item.get("config") or {}),
                    )
                    for item in assignment_snapshot
                    if isinstance(item, dict) and item.get("tool_id")
                }
            assigned_tool_ids = [uuid.UUID(tool_id) for tool_id in assignments]

            # Get all tools visible within this agent's tenant boundary.
            tool_clauses = [
                or_(Tool.enabled == True, Tool.name.in_(REQUIRED_AGENT_TOOL_NAMES)),
                tool_visibility_clause(agent_tenant_id, assigned_tool_ids),
            ]
            if not okr_feature_enabled():
                tool_clauses.append(Tool.name.not_in(OKR_TOOL_NAMES))
            tool_clauses.append(Tool.name.not_in(PLAZA_TOOL_NAMES))
            all_tools_r = await db.execute(select(Tool).where(*tool_clauses))
            all_tools = all_tools_r.scalars().all()

            from app.services.cli_tools.sandbox_inject import _TOOL_NAME_RE

            result = []
            db_tool_names = set()
            # Track tool names that were explicitly disabled by the user
            # (have an AgentTool record with enabled=False). These must NOT
            # be re-added by the _always_tools fallback below.
            explicitly_disabled_names = set()
            for t in all_tools:
                if is_retired_okr_tool(t.name):
                    continue
                # Child-only protocol surface. Subagent execution appends this
                # definition explicitly after filtering the ordinary tool set.
                if t.name == "send_message_to_parent":
                    continue
                tid = str(t.id)
                at = assignments.get(tid)
                enabled = resolved_agent_tool_enabled(t.name, at)
                if not enabled:
                    if (assignment_snapshot is not None or (at and not at.enabled)) and not tool_is_required(t.name):
                        explicitly_disabled_names.add(t.name)
                    continue

                # type='cli' tools are standalone LLM functions: the handler
                # (_execute_cli_tool) runs the supplied bash command line in the
                # aio sandbox with this tool's identity-bound function injected.
                # Only surface tools that have a binary + a safe, non-colliding
                # function name.
                if t.type == "cli":
                    _binary = (t.config or {}).get("binary")
                    if not (isinstance(_binary, dict) and _binary.get("sha256")):
                        continue  # no binary uploaded yet (tolerate legacy null/non-dict)
                    _always_names = {a["function"]["name"] for a in _always_tools}
                    if (
                        not _TOOL_NAME_RE.fullmatch(t.name)
                        or t.name == "toolscall"
                        or t.name in db_tool_names
                        or t.name in _always_names
                    ):
                        logger.warning(
                            f"[Tools] Skipping CLI tool '{t.name}' "
                            "(unsafe name, or collides with a builtin/duplicate function)"
                        )
                        continue
                    result.append(
                        {
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "description": t.description,
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "command": {
                                            "type": "string",
                                            "description": (
                                                f"完整的 bash 命令行,**必须以程序名 `{t.name}` 开头**"
                                                f"(它是沙箱里一个已注入身份认证的真实命令;具体子命令/"
                                                f"参数见本工具说明)。例如 `{t.name} <参数...>`——不要省略"
                                                f"程序名只写参数。支持管道/重定向等任意 bash 组合,"
                                                f"如 `{t.name} <参数...> | jq '.'`。"
                                            ),
                                        }
                                    },
                                    "required": ["command"],
                                },
                            },
                        }
                    )
                    db_tool_names.add(t.name)
                    continue

                # Skip feishu tools if the agent has no Feishu channel configured
                if t.category == "feishu" and not has_feishu:
                    continue
                # Match the Agent Tools UI: regular agents must not receive
                # OKR-system-only tools, even if the DB default says enabled.
                if (t.config or {}).get("okr_agent_only") and not is_system_agent:
                    continue
                description = t.description
                if t.name == "execute_code_aio":
                    from app.services.toolscall.capability import (
                        TOOLSCALL_USAGE_DESCRIPTION,
                        toolscall_enabled_for_agent,
                    )

                    # This is an Agent-level switch. Missing configuration uses
                    # the platform's enabled-by-default policy, while an
                    # explicit false remains a per-Agent opt-out.
                    toolscall_enabled = toolscall_enabled_for_agent(at.config if at else None)
                    if toolscall_enabled:
                        description = str(description or "").rstrip() + TOOLSCALL_USAGE_DESCRIPTION

                # Build OpenAI function-calling format
                tool_def = {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": description,
                        "parameters": t.parameters_schema or {"type": "object", "properties": {}},
                    },
                }
                # Defensive dedup: skip if this name was already added.
                # Normally the UNIQUE constraint on tool.name prevents duplicate
                # rows, but old DB dumps (pre-constraint) may have them. Without
                # this guard, the LLM would receive duplicate tool names and
                # return HTTP 400 "Tool names must be unique".
                if t.name in db_tool_names:
                    logger.warning(
                        f"[Tools] Duplicate tool name '{t.name}' found in DB "
                        f"(id={t.id}). Skipping to avoid LLM error. "
                        "Run: DELETE FROM tools WHERE id IN (SELECT id FROM "
                        "(SELECT id, ROW_NUMBER() OVER (PARTITION BY name "
                        "ORDER BY created_at DESC) AS rn FROM tools) t WHERE rn > 1);"
                    )
                    continue

                # Media tools are platform protocol, not tenant configuration.
                # Always use the canonical static schema appended in fixed order.
                if t.name in _FIXED_MEDIA_TOOL_NAMES:
                    continue

                result.append(tool_def)
                db_tool_names.add(t.name)

            if result:
                # Append always-available system tools that aren't already in
                # the DB list — but respect explicit user disabling.
                always_added = []
                for t in _always_tools:
                    fn_name = t["function"]["name"]
                    if fn_name not in db_tool_names and fn_name not in explicitly_disabled_names:
                        result.append(t)
                        always_added.append(fn_name)
                if always_added:
                    logger.debug(f"[Tools] agent={agent_id} added from _always_tools: {always_added}")
                if "agentbay_file_transfer" in db_tool_names:
                    computer_os_type = await _get_computer_os_type(agent_id)
                # Inject OS-aware paths into computer-related tool descriptions
                result = _patch_computer_tool_descriptions(result, computer_os_type)
                # Strip msg_type from send_message_to_agent when async A2A is disabled
                if not _a2a_async:
                    result = _strip_a2a_msg_type(result)
                result = _stabilize_media_tool_definitions(result)
                # Final diagnostic: log the complete tool list and assignment stats
                final_names = sorted(t["function"]["name"] for t in result)
                logger.info(
                    f"[Tools] agent={agent_id} FINAL {len(result)} tools "
                    f"(assignments={len(assignments)}, "
                    f"disabled={len(explicitly_disabled_names)}): "
                    f"{final_names}"
                )
                return result
            # If DB loading fails, do not expose the full hardcoded tool catalog: that
            # can leak disabled tools (for example search tools) into the LLM. Keep only
            # the minimal always-available core/channel tools.
            # (Note: we fall through to the except-clause fallback below if result is empty or exception is raised)
            raise ValueError("No tools found for agent in DB")
    except Exception as e:
        logger.error(f"[Tools] DB load failed, using fallback: {e}")

    # If DB loading fails, do not expose the full hardcoded tool catalog: that
    # can leak disabled tools (for example search tools) into the LLM. Keep only
    # the minimal always-available core/channel tools.
    fallback = _patch_computer_tool_descriptions(_always_tools, computer_os_type)
    if not _a2a_async:
        fallback = _strip_a2a_msg_type(fallback)
    return _stabilize_media_tool_definitions(fallback)


def _stabilize_media_tool_definitions_runtime(
    tools: list[dict],
    agent_tools: list[dict] | None = None,
) -> list[dict]:
    return _stabilize_media_tool_definitions_impl(tools, agent_tools or AGENT_TOOLS)


async def get_agent_tools_for_llm_runtime(
    agent_id: uuid.UUID,
    *,
    agent_tools: list[dict] | None = None,
    get_tool_config=None,
    assignment_snapshot: list[dict] | None = None,
) -> list[dict]:
    return await _get_agent_tools_for_llm_impl(
        agent_id,
        agent_tools=agent_tools or AGENT_TOOLS,
        get_tool_config=get_tool_config or _get_tool_config,
        assignment_snapshot=assignment_snapshot,
    )


_stabilize_media_tool_definitions = _stabilize_media_tool_definitions_runtime
get_agent_tools_for_llm = get_agent_tools_for_llm_runtime
