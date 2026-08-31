from __future__ import annotations

import uuid
from pathlib import Path

from app.database import async_session
from app.services.agent_runtime_workspace import current_agent_runtime_workspace
from app.services.agent_tools import (
    TOOL_MATERIALIZE_MAX_FILE_BYTES,
    _add_contact_tool,
    _agent_workspace_root,
    _bing_search_tool,
    _browse,
    _duckduckgo_search_tool,
    _exa_search,
    _execute_code,
    _get_agent_tenant_id,
    _google_search_tool,
    _install_skill_from_market,
    _is_enterprise_info_path,
    _is_webhook_inbox_path,
    _jina_search,
    _publish_skill_to_market,
    _read_webpage,
    _remove_contact_tool,
    _resolve_storage_source_path,
    _send_feishu_message,
    _sql_execute,
    _storage_source_error,
    _tavily_search_tool,
    _tool_storage_key,
    _web_cdp,
    _web_eval,
    _web_open,
    _web_search,
    _web_screenshot,
    _withdraw_skill_from_market,
    delete_workspace_file,
    get_storage_backend,
    is_focus_file_path,
    logger,
    move_workspace_path,
    write_workspace_file,
)
from app.services.agent_tools import _send_file_to_agent, _send_message_to_agent
from app.services.agent_tools_temp_workspace import _prepare_temp_workspace, flush_temp_workspace


# All tool names that route to the _execute_code handler.
_CODE_EXEC_TOOL_NAMES: frozenset[str] = frozenset({"execute_code", "execute_code_e2b", "execute_code_aio"})


async def _run_with_temp_workspace(
    agent_id: uuid.UUID,
    tenant_id: str | None,
    runner,
    *,
    paths: list[str] | None = None,
    source_paths: list[str] | None = None,
    sync_back: bool = False,
    max_file_bytes: int = TOOL_MATERIALIZE_MAX_FILE_BYTES,
) -> str:
    """Materialize a temporary workspace for tools that require local files."""
    materialized_paths = paths
    if source_paths:
        canonical_sources: dict[str, str] = {}
        for source_path in source_paths:
            resolved = await _resolve_storage_source_path(agent_id, source_path, tenant_id)
            if resolved.ambiguous_candidates:
                return _storage_source_error(source_path, resolved)
            if resolved.exists:
                canonical_sources[source_path] = resolved.virtual_path
        if paths is not None:
            materialized_paths = [canonical_sources.get(path, path) for path in paths]
        else:
            materialized_paths = [canonical_sources.get(path, path) for path in source_paths]

    temp_workspace = await _prepare_temp_workspace(
        agent_id,
        tenant_id=tenant_id,
        paths=materialized_paths,
        max_file_bytes=max_file_bytes,
    )
    try:
        result = await runner(temp_workspace.root)
        if sync_back:
            flush_result = await flush_temp_workspace(temp_workspace, conflict_mode="fail")
            if flush_result["conflicted"]:
                conflict_list = ", ".join(flush_result["conflicted"][:5])
                return f"❌ Workspace sync conflict for: {conflict_list}"
        return result
    finally:
        temp_workspace.cleanup()


async def _execute_workspace_mutation(
    tool_name: str,
    arguments: dict,
    *,
    agent_id: uuid.UUID,
    base_dir: Path,
    session_id: str | None,
) -> str:
    """Handle shared workspace mutations for both direct and normal tool execution."""
    runtime_workspace = current_agent_runtime_workspace(agent_id)
    if tool_name == "write_file":
        path = arguments.get("path")
        content = arguments.get("content")
        if not path:
            return "❌ Missing required argument 'path' for write_file. Please provide a file path like 'skills/my-skill/SKILL.md'"
        if content is None:
            return "❌ Missing required argument 'content' for write_file"
        if is_focus_file_path(path):
            return "❌ Focus is no longer stored in focus.md. Use upsert_focus_item or complete_focus_item."
        if _is_enterprise_info_path(path):
            return (
                "❌ enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."
            )
        if runtime_workspace.is_agent_write_protected(path):
            return f"❌ {path} is managed by the project owner and is read-only for Agents."
        if _is_webhook_inbox_path(path):
            return "❌ webhook/ is a system-managed inbox and is read-only for agents."
        async with async_session() as _wdb:
            write_result = await write_workspace_file(
                _wdb,
                agent_id=agent_id,
                base_dir=base_dir,
                path=path,
                content=content,
                actor_type="agent",
                actor_id=agent_id,
                operation="write",
                session_id=session_id,
                enforce_human_lock=True,
            )
            await _wdb.commit()
        return (
            f"✅ Written to {write_result.path} ({len(content)} chars)"
            if write_result.ok
            else f"❌ {write_result.message}"
        )

    if tool_name == "move_file":
        source_path = arguments.get("source_path")
        destination_path = arguments.get("destination_path")
        if not source_path:
            return "❌ Missing required argument 'source_path' for move_file"
        if not destination_path:
            return "❌ Missing required argument 'destination_path' for move_file"
        if is_focus_file_path(source_path) or is_focus_file_path(destination_path):
            return "❌ Focus is no longer stored in focus.md. Use Focus tools instead."
        if str(source_path).strip("/") in {"tasks.json", "soul.md"}:
            return f"❌ {source_path} cannot be moved (protected)"
        if _is_enterprise_info_path(source_path) or _is_enterprise_info_path(destination_path):
            return (
                "❌ enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."
            )
        if runtime_workspace.is_agent_write_protected(source_path) or runtime_workspace.is_agent_write_protected(
            destination_path
        ):
            return "❌ Project-owned Agent identity files cannot be moved."
        if _is_webhook_inbox_path(source_path) or _is_webhook_inbox_path(destination_path):
            return "❌ webhook/ is a system-managed inbox and is read-only for agents."
        async with async_session() as _wdb:
            move_result = await move_workspace_path(
                _wdb,
                agent_id=agent_id,
                base_dir=base_dir,
                source_path=source_path,
                destination_path=destination_path,
                actor_type="agent",
                actor_id=agent_id,
                session_id=session_id,
                enforce_human_lock=True,
                overwrite=bool(arguments.get("overwrite", False)),
            )
            await _wdb.commit()
        return f"✅ {move_result.message}" if move_result.ok else f"❌ {move_result.message}"

    if tool_name == "delete_file":
        path = arguments.get("path", "")
        if is_focus_file_path(path):
            return "❌ Focus is no longer stored in focus.md. Use Focus tools instead."
        if _is_enterprise_info_path(path):
            return (
                "❌ enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."
            )
        if runtime_workspace.is_agent_write_protected(path):
            return f"❌ {path} is managed by the project owner and is read-only for Agents."
        if _is_webhook_inbox_path(path):
            return "❌ webhook/ is a system-managed inbox and is read-only for agents."
        async with async_session() as _wdb:
            delete_result = await delete_workspace_file(
                _wdb,
                agent_id=agent_id,
                base_dir=base_dir,
                path=path,
                actor_type="agent",
                actor_id=agent_id,
                session_id=session_id,
                enforce_human_lock=True,
            )
            await _wdb.commit()
        return f"✅ Deleted {delete_result.path}" if delete_result.ok else f"❌ {delete_result.message}"

    if tool_name == "edit_file":
        path = arguments.get("path")
        old_string = arguments.get("old_string")
        new_string = arguments.get("new_string")
        if not path:
            return "❌ Missing required argument 'path' for edit_file"
        if old_string is None:
            return "❌ Missing required argument 'old_string' for edit_file"
        if new_string is None:
            return "❌ Missing required argument 'new_string' for edit_file"
        if is_focus_file_path(path):
            return "❌ Focus is no longer stored in focus.md. Use upsert_focus_item or complete_focus_item."
        if _is_enterprise_info_path(path):
            return (
                "❌ enterprise_info is shared company context and is read-only for agents. Ask an admin to update it."
            )
        if runtime_workspace.is_agent_write_protected(path):
            return f"❌ {path} is managed by the project owner and is read-only for Agents."
        if _is_webhook_inbox_path(path):
            return "❌ webhook/ is a system-managed inbox and is read-only for agents."

        replace_all = arguments.get("replace_all", False)
        storage = get_storage_backend()
        storage_key, normalized_path, _ = _tool_storage_key(agent_id, path, None)
        if not await storage.is_file(storage_key):
            return f"File not found: {path}"

        content = await storage.read_text(storage_key, encoding="utf-8", errors="replace")
        if old_string not in content:
            return (
                f"❌ 'old_string' not found in {path}. Please check the exact text including whitespace and newlines."
            )
        count = content.count(old_string)
        if count > 1 and not replace_all:
            return f"❌ 'old_string' appears {count} times in {path}. Use replace_all=true or provide more context to make the match unique."

        new_content = (
            content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
        )
        async with async_session() as _wdb:
            write_result = await write_workspace_file(
                _wdb,
                agent_id=agent_id,
                base_dir=base_dir,
                path=normalized_path,
                content=new_content,
                actor_type="agent",
                actor_id=agent_id,
                operation="edit",
                session_id=session_id,
                enforce_human_lock=True,
            )
            await _wdb.commit()
        replaced = count if replace_all else 1
        return (
            f"✅ Replaced {replaced} occurrence(s) in {write_result.path}"
            if write_result.ok
            else f"❌ {write_result.message}"
        )

    return f"Tool {tool_name} does not support workspace mutation execution"


async def _execute_tool_direct(
    tool_name: str,
    arguments: dict,
    agent_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
) -> str:
    """Execute a tool directly, bypassing autonomy checks.

    Used by the approval post-processing hook after an action
    has been approved and needs to actually run.
    """
    _agent_tenant_id = await _get_agent_tenant_id(agent_id)
    ws = _agent_workspace_root(agent_id)
    try:
        if tool_name in {"delete_file", "write_file", "move_file", "edit_file"}:
            return await _execute_workspace_mutation(
                tool_name,
                arguments,
                agent_id=agent_id,
                base_dir=ws,
                session_id=None,
            )
        elif tool_name in _CODE_EXEC_TOOL_NAMES:
            logger.info(f"[DirectTool] Executing code ({tool_name}) with arguments: {arguments}")
            return await _run_with_temp_workspace(
                agent_id,
                _agent_tenant_id,
                lambda temp_ws: _execute_code(agent_id, temp_ws, arguments, tool_name=tool_name),
                sync_back=True,
            )
        elif tool_name == "browse":
            return await _run_with_temp_workspace(
                agent_id,
                _agent_tenant_id,
                lambda temp_ws: _browse(agent_id, temp_ws, arguments, user_id=None, session_id=None),
                sync_back=True,
            )
        elif tool_name == "web_open":
            return await _run_with_temp_workspace(
                agent_id,
                _agent_tenant_id,
                lambda temp_ws: _web_open(agent_id, temp_ws, arguments, user_id=None, session_id=None),
                sync_back=True,
            )
        elif tool_name == "web_eval":
            return await _run_with_temp_workspace(
                agent_id,
                _agent_tenant_id,
                lambda temp_ws: _web_eval(agent_id, temp_ws, arguments, user_id=None, session_id=None),
                sync_back=True,
            )
        elif tool_name == "web_cdp":
            return await _run_with_temp_workspace(
                agent_id,
                _agent_tenant_id,
                lambda temp_ws: _web_cdp(agent_id, temp_ws, arguments, user_id=None, session_id=None),
                sync_back=True,
            )
        elif tool_name == "web_screenshot":
            return await _run_with_temp_workspace(
                agent_id,
                _agent_tenant_id,
                lambda temp_ws: _web_screenshot(agent_id, temp_ws, arguments, user_id=None, session_id=None),
                sync_back=True,
            )
        elif tool_name == "sql_execute":
            return await _sql_execute(arguments)
        elif tool_name == "install_skill_from_market":
            return await _install_skill_from_market(
                agent_id,
                user_id,
                arguments,
            )
        elif tool_name == "publish_skill_to_market":
            return await _publish_skill_to_market(agent_id, user_id, arguments)
        elif tool_name == "withdraw_skill_from_market":
            return await _withdraw_skill_from_market(agent_id, user_id, arguments)
        elif tool_name == "web_search":
            return await _web_search(arguments, agent_id)
        elif tool_name == "jina_search":
            return await _jina_search(arguments)
        elif tool_name == "read_webpage":
            return await _read_webpage(arguments)
        elif tool_name == "exa_search":
            return await _exa_search(arguments, agent_id)
        elif tool_name == "duckduckgo_search":
            return await _duckduckgo_search_tool(arguments)
        elif tool_name == "tavily_search":
            return await _tavily_search_tool(arguments, agent_id)
        elif tool_name == "google_search":
            return await _google_search_tool(arguments, agent_id)
        elif tool_name == "bing_search":
            return await _bing_search_tool(arguments, agent_id)
        elif tool_name == "send_feishu_message":
            return await _send_feishu_message(agent_id, arguments)
        elif tool_name == "add_contact":
            return await _add_contact_tool(agent_id, arguments, user_id=user_id)
        elif tool_name == "remove_contact":
            return await _remove_contact_tool(agent_id, arguments, user_id=user_id)
        elif tool_name == "send_message_to_agent":
            return await _send_message_to_agent(
                agent_id,
                arguments,
                user_id=None,
                origin_session_id=None,
            )
        elif tool_name == "send_file_to_agent":
            return await _send_file_to_agent(agent_id, arguments)
        else:
            return f"Tool {tool_name} does not support post-approval execution"
    except Exception as e:
        logger.exception(f"[DirectTool] Error executing {tool_name}: {e}")
        return f"Error executing {tool_name}: {e}"
