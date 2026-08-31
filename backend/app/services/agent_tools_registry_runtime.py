from __future__ import annotations

import copy
from types import SimpleNamespace
import uuid

from loguru import logger
from sqlalchemy import or_, select

from app.core.okr_feature import OKR_TOOL_NAMES, is_retired_okr_tool, okr_feature_enabled
from app.database import async_session

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


def _stabilize_media_tool_definitions(tools: list[dict], agent_tools: list[dict]) -> list[dict]:
    canonical = {item["function"]["name"]: item for item in agent_tools if item["function"]["name"] in _FIXED_MEDIA_TOOL_NAMES}
    stable = [
        item
        for item in tools
        if item.get("function", {}).get("name") not in {*_FIXED_MEDIA_TOOL_NAMES, *_LEGACY_MEDIA_TOOL_NAMES}
    ]
    stable.extend(canonical[name] for name in _FIXED_MEDIA_TOOL_NAMES)
    return stable


def _patch_computer_tool_descriptions(tools: list[dict], os_type: str) -> list[dict]:
    if os_type == "windows":
        desktop_path = r"C:\Users\Administrator\Desktop"
        home_path = r"C:\Users\Administrator"
        computer_os_label = "Windows"
    else:
        desktop_path = "/home/wuying/Desktop"
        home_path = "/home/wuying"
        computer_os_label = "Linux"

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
        if fn.get("name", "") == "agentbay_file_transfer":
            tool = copy.deepcopy(tool)
            tool["function"]["description"] = new_file_transfer_desc
            props = tool["function"].get("parameters", {}).get("properties", {})
            if "from_path" in props:
                props["from_path"]["description"] = (
                    r"Source path. Relative if workspace (e.g. 'workspace/data.csv'). "
                    r"Absolute if env: computer → C:\Users\Administrator\Desktop\file, "
                    r"code → /home/wuying/file, browser → /home/wuying/下载/file."
                ) if os_type == "windows" else (
                    "Source path. Relative if workspace (e.g. 'workspace/data.csv'). "
                    "Absolute if env: computer → /home/wuying/Desktop/file, "
                    "code → /home/wuying/file, browser → /home/wuying/下载/file."
                )
            if "to_path" in props:
                props["to_path"]["description"] = (
                    r"Destination path. Relative if workspace (e.g. 'workspace/output.csv'). "
                    r"Absolute if env: computer → C:\Users\Administrator\Desktop\file, "
                    r"code → /home/wuying/file, browser → /home/wuying/下载/file."
                ) if os_type == "windows" else (
                    "Destination path. Relative if workspace (e.g. 'workspace/output.csv'). "
                    "Absolute if env: computer → /home/wuying/Desktop/file, "
                    "code → /home/wuying/file, browser → /home/wuying/下载/file."
                )
        patched.append(tool)
    return patched


async def _agent_has_feishu(agent_id: uuid.UUID) -> bool:
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
    result = []
    for t in tools:
        fn = t.get("function", {})
        if fn.get("name") == "send_message_to_agent":
            t = copy.deepcopy(t)
            fn = t["function"]
            fn["description"] = (
                "Send a message to a digital employee colleague and receive their reply synchronously.\n\n"
                "RESET: If the conversation with a colleague gets stuck — the same tool failing over and "
                "over, repeated identical errors, looping, or visibly corrupted/garbled context — set "
                "new_conversation=true to discard the stale history and start a fresh, clean thread."
            )
            params = fn.get("parameters", {})
            props = params.get("properties", {})
            props.pop("msg_type", None)
            req = params.get("required", [])
            if "msg_type" in req:
                params["required"] = [r for r in req if r != "msg_type"]
        result.append(t)
    return result


async def get_agent_tools_for_llm(
    agent_id: uuid.UUID,
    *,
    agent_tools: list[dict],
    get_tool_config,
    assignment_snapshot: list[dict] | None = None,
) -> list[dict]:
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

            visible_clauses = [Tool.source == "builtin"]
            if agent_tenant_id:
                visible_clauses.append((Tool.source == "admin") & ((Tool.tenant_id == agent_tenant_id) | (Tool.tenant_id.is_(None))))
            else:
                visible_clauses.append((Tool.source == "admin") & (Tool.tenant_id.is_(None)))
            if assigned_tool_ids:
                visible_clauses.append(Tool.id.in_(assigned_tool_ids))

            tool_clauses = [or_(Tool.enabled == True, Tool.name.in_(REQUIRED_AGENT_TOOL_NAMES)), or_(*visible_clauses)]
            if not okr_feature_enabled():
                tool_clauses.append(Tool.name.not_in(OKR_TOOL_NAMES))
            tool_clauses.append(Tool.name.not_in(PLAZA_TOOL_NAMES))
            all_tools = (await db.execute(select(Tool).where(*tool_clauses))).scalars().all()

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
                    if fn_name not in db_tool_names and fn_name not in explicitly_disabled_names:
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
                result = _stabilize_media_tool_definitions(result, agent_tools)
                final_names = sorted(t["function"]["name"] for t in result)
                logger.info(
                    f"[Tools] agent={agent_id} FINAL {len(result)} tools "
                    f"(assignments={len(assignments)}, disabled={len(explicitly_disabled_names)}): {final_names}"
                )
                return result
            raise ValueError("No tools found for agent in DB")
    except Exception as e:
        logger.error(f"[Tools] DB load failed, using fallback: {e}")

    fallback = _patch_computer_tool_descriptions(_always_tools, computer_os_type)
    if not _a2a_async:
        fallback = _strip_a2a_msg_type(fallback)
    return _stabilize_media_tool_definitions(fallback, agent_tools)
