from __future__ import annotations

import json
import uuid

from app.services.project_runtime_tools import finalize_project_sandbox_changes
from app.services.agent_tools import (
    MEDIA_TOOL_MAX_FILE_BYTES,
    TOOL_MATERIALIZE_MAX_FILE_BYTES,
    TOOL_MATERIALIZE_MAX_TOTAL_BYTES,
    _add_contact_tool,
    _agent_workspace_root,
    _bing_search_tool,
    _convert_csv_to_xlsx,
    _convert_html_to_pdf,
    _convert_html_to_pptx,
    _convert_markdown_to_docx,
    _convert_markdown_to_pdf,
    _describe_media_delivery_result,
    _discover_resources,
    _duckduckgo_search_tool,
    _exa_search,
    _execute_code,
    _generate_image,
    _get_dingtalk_channel_provisioning_status_tool,
    _google_search_tool,
    _handle_cancel_trigger,
    _handle_list_triggers,
    _handle_set_trigger,
    _handle_update_trigger,
    _import_mcp_server,
    _jina_read,
    _jina_search,
    _manage_tasks,
    _media_materialization_size_error,
    _non_empty_paths,
    _plaza_add_comment,
    _plaza_create_post,
    _plaza_get_new_posts,
    _read_document_from_storage,
    _read_webpage,
    _remove_contact_tool,
    _replay_terminal_media_delivery,
    _resolve_storage_source_path,
    _search_contacts_tool,
    _send_channel_file,
    _send_channel_media,
    _send_channel_message,
    _send_feishu_message,
    _send_group_session_message,
    _send_platform_message,
    _send_session_message,
    _sql_execute,
    _start_dingtalk_channel_provisioning_tool,
    _storage_find_files,
    _storage_list_dir,
    _storage_read_file,
    _storage_search_files,
    _tavily_search_tool,
    _upload_image,
    _web_search,
    complete_focus_item,
    get_storage_backend,
    is_focus_file_path,
    list_focus_items,
    logger,
    recall_message,
    upsert_focus_item,
)
from app.services.agent_tools_a2a_delivery import _send_file_to_agent
from app.services.agent_tools_a2a_messaging import _send_message_to_agent
from app.services.agent_tools_execute_tool_preflight import ExecuteToolDispatchContext
from app.services.media_ai_contract import MEDIA_AI_NAMES
from app.services.media_ai_tools import execute_media_tool
from app.services.model_catalog_tool import execute_list_models
from app.services.agent_tools_temp_workspace_exec import _CODE_EXEC_TOOL_NAMES, _execute_workspace_mutation, _run_with_temp_workspace

_ROOT_TOOL_SYMBOLS = (
    "MEDIA_TOOL_MAX_FILE_BYTES",
    "TOOL_MATERIALIZE_MAX_FILE_BYTES",
    "TOOL_MATERIALIZE_MAX_TOTAL_BYTES",
    "_add_contact_tool",
    "_agent_workspace_root",
    "_bing_search_tool",
    "_convert_csv_to_xlsx",
    "_convert_html_to_pdf",
    "_convert_html_to_pptx",
    "_convert_markdown_to_docx",
    "_convert_markdown_to_pdf",
    "_describe_media_delivery_result",
    "_discover_resources",
    "_duckduckgo_search_tool",
    "_exa_search",
    "_execute_code",
    "_generate_image",
    "_get_dingtalk_channel_provisioning_status_tool",
    "_google_search_tool",
    "_handle_cancel_trigger",
    "_handle_list_triggers",
    "_handle_set_trigger",
    "_handle_update_trigger",
    "_import_mcp_server",
    "_jina_read",
    "_jina_search",
    "_manage_tasks",
    "_media_materialization_size_error",
    "_non_empty_paths",
    "_plaza_add_comment",
    "_plaza_create_post",
    "_plaza_get_new_posts",
    "_read_document_from_storage",
    "_read_webpage",
    "_remove_contact_tool",
    "_replay_terminal_media_delivery",
    "_resolve_storage_source_path",
    "_search_contacts_tool",
    "_send_channel_file",
    "_send_channel_media",
    "_send_channel_message",
    "_send_feishu_message",
    "_send_group_session_message",
    "_send_platform_message",
    "_send_session_message",
    "_sql_execute",
    "_start_dingtalk_channel_provisioning_tool",
    "_storage_find_files",
    "_storage_list_dir",
    "_storage_read_file",
    "_storage_search_files",
    "_tavily_search_tool",
    "_upload_image",
    "_web_search",
    "complete_focus_item",
    "get_storage_backend",
    "is_focus_file_path",
    "list_focus_items",
    "logger",
    "recall_message",
    "upsert_focus_item",
)


def _sync_root_tool_symbols() -> None:
    from app.services import agent_tools as _agent_tools_root

    for _name in _ROOT_TOOL_SYMBOLS:
        globals()[_name] = getattr(_agent_tools_root, _name)


async def execute_tool_dispatch_basic(state: ExecuteToolDispatchContext) -> str | None:
    _sync_root_tool_symbols()
    tool_name = state.tool_name
    arguments = state.arguments
    agent_id = state.agent_id
    user_id = state.user_id
    session_id = state.session_id
    tool_call_id = state.tool_call_id
    turn_anchor_id = state.turn_anchor_id
    on_output = state.on_output
    tools_for_llm = state.tools_for_llm
    project_sandbox_scope = state.project_sandbox_scope
    _agent_tenant_id = state.agent_tenant_id
    ws = state.ws

    if tool_name == "list_models":
        return await execute_list_models(state)
    elif tool_name in MEDIA_AI_NAMES:
        if state.project_workspace == "agent":
            state.arguments = {key: value for key, value in arguments.items() if key != "workspace"}
        return await execute_media_tool(state)
    elif tool_name == "list_files":
        result = await _storage_list_dir(agent_id, arguments.get("path", ""), tenant_id=_agent_tenant_id)
    elif tool_name == "update_self_settings":
        from app.services.agent_self_settings_tool import handle_update_self_settings

        return await handle_update_self_settings(agent_id, arguments)
    elif tool_name == "list_focus_items":
        items = await list_focus_items(agent_id, include_completed=bool(arguments.get("include_completed", True)))
        if not items:
            result = "No Focus items."
        else:
            lines = ["Focus items:"]
            for item in items:
                label = "completed" if item["status"] == "completed" else "in_progress"
                kind = f", {item['kind']}" if item.get("kind") == "system" else ""
                if item.get("title"):
                    lines.append(f"- {item['title']} ({item['key']}) [{label}{kind}]: {item['description']}")
                else:
                    lines.append(f"- {item['key']} [{label}{kind}]: {item['description']}")
            result = "\n".join(lines)
    elif tool_name == "upsert_focus_item":
        description = (arguments.get("description") or "").strip()
        if not description:
            return "❌ Missing required argument 'description' for upsert_focus_item"
        item = await upsert_focus_item(
            agent_id,
            key=arguments.get("key"),
            title=arguments.get("title"),
            description=description,
            status="in_progress",
            kind=arguments.get("kind") or "normal",
            source=arguments.get("source") or "user",
            metadata={"tool": "upsert_focus_item"},
        )
        result = (
            f"✅ Focus item saved: {item['key']} (title: {item['title']}) — {item['description']}"
            if item.get("title")
            else f"✅ Focus item saved: {item['key']} — {item['description']}"
        )
    elif tool_name == "complete_focus_item":
        key = (arguments.get("key") or "").strip()
        if not key:
            return "❌ Missing required argument 'key' for complete_focus_item"
        item = await complete_focus_item(agent_id, key=key)
        result = f"✅ Focus item completed: {key}" if item else f"❌ Focus item not found: {key}"
    elif tool_name == "read_file":
        path = arguments.get("path")
        if not path:
            return "❌ Missing required argument 'path' for read_file"
        if is_focus_file_path(path):
            return "❌ Focus is no longer stored in focus.md. Use list_focus_items, upsert_focus_item, and complete_focus_item."
        offset = int(arguments.get("offset", 0))
        limit = int(arguments.get("limit", 2000))
        result = await _storage_read_file(agent_id, path, tenant_id=_agent_tenant_id, offset=offset, limit=limit)
    elif tool_name == "read_document":
        path = arguments.get("path")
        if not path:
            return "❌ Missing required argument 'path' for read_document"
        max_chars = min(int(arguments.get("max_chars", 8000)), 20000)
        result = await _read_document_from_storage(agent_id, path, max_chars=max_chars, tenant_id=_agent_tenant_id)
    elif tool_name in {"write_file", "move_file", "delete_file", "edit_file"}:
        result = await _execute_workspace_mutation(
            tool_name,
            arguments,
            agent_id=agent_id,
            base_dir=ws,
            session_id=session_id,
        )
    elif tool_name == "list_sessions":
        from app.services.tools.session_introspection import handle_list_sessions

        return await handle_list_sessions(agent_id, user_id, session_id, arguments)
    elif tool_name == "set_execution_user":
        from app.services.execution_identity import (
            handle_reassign_background_execution_user,
        )

        return await handle_reassign_background_execution_user(
            agent_id,
            user_id,
            session_id,
            turn_anchor_id,
            arguments,
        )
    elif tool_name == "run_background_resource":
        from app.services.background_manual_run import handle_run_background_resource

        return await handle_run_background_resource(
            agent_id,
            user_id,
            session_id,
            turn_anchor_id,
            arguments,
        )
    elif tool_name == "read_session_messages":
        from app.services.tools.session_introspection import handle_read_session_messages

        return await handle_read_session_messages(agent_id, user_id, session_id, arguments)
    elif tool_name == "search_sessions":
        from app.services.tools.session_introspection import handle_search_sessions

        return await handle_search_sessions(agent_id, user_id, session_id, arguments)
    # --- Enhanced file management tools ---
    elif tool_name == "convert_csv_to_xlsx":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _convert_csv_to_xlsx(agent_id, temp_ws, arguments),
            paths=_non_empty_paths(arguments.get("source_path", ""), arguments.get("target_path", "")),
            source_paths=_non_empty_paths(arguments.get("source_path", "")),
            sync_back=True,
        )
    elif tool_name == "convert_html_to_pdf":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _convert_html_to_pdf(agent_id, temp_ws, arguments),
            paths=_non_empty_paths(arguments.get("source_path", ""), arguments.get("target_path", "")),
            source_paths=_non_empty_paths(arguments.get("source_path", "")),
            sync_back=True,
        )
    elif tool_name == "convert_html_to_pptx":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _convert_html_to_pptx(agent_id, temp_ws, arguments),
            paths=_non_empty_paths(arguments.get("source_path", ""), arguments.get("target_path", "")),
            source_paths=_non_empty_paths(arguments.get("source_path", "")),
            sync_back=True,
        )
    elif tool_name == "convert_markdown_to_docx":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _convert_markdown_to_docx(agent_id, temp_ws, arguments),
            paths=_non_empty_paths(arguments.get("source_path", ""), arguments.get("target_path", "")),
            source_paths=_non_empty_paths(arguments.get("source_path", "")),
            sync_back=True,
        )
    elif tool_name == "convert_markdown_to_pdf":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _convert_markdown_to_pdf(agent_id, temp_ws, arguments),
            paths=_non_empty_paths(arguments.get("source_path", ""), arguments.get("target_path", "")),
            source_paths=_non_empty_paths(arguments.get("source_path", "")),
            sync_back=True,
        )
    elif tool_name == "search_files":
        pattern = arguments.get("pattern")
        if not pattern:
            return "❌ Missing required argument 'pattern' for search_files"
        result = await _storage_search_files(
            agent_id,
            pattern,
            path=arguments.get("path", "."),
            file_pattern=arguments.get("file_pattern", "*"),
            ignore_case=arguments.get("ignore_case", False),
            tenant_id=_agent_tenant_id,
        )
    elif tool_name == "find_files":
        pattern = arguments.get("pattern")
        if not pattern:
            return "❌ Missing required argument 'pattern' for find_files"
        result = await _storage_find_files(
            agent_id, pattern, path=arguments.get("path", "."), tenant_id=_agent_tenant_id
        )
    elif tool_name == "manage_tasks":
        result = await _manage_tasks(agent_id, user_id, ws, arguments)
    elif tool_name == "set_trigger":
        result = await _handle_set_trigger(
            agent_id,
            arguments,
            session_id=session_id,
            user_id=user_id,
            turn_anchor_id=turn_anchor_id,
        )
    elif tool_name == "update_trigger":
        result = await _handle_update_trigger(
            agent_id,
            arguments,
            user_id=user_id,
        )
    elif tool_name == "cancel_trigger":
        result = await _handle_cancel_trigger(
            agent_id,
            arguments,
            user_id=user_id,
        )
    elif tool_name == "list_triggers":
        result = await _handle_list_triggers(agent_id, arguments)
    elif tool_name == "search_contacts":
        result = await _search_contacts_tool(agent_id, arguments, user_id)
    elif tool_name == "add_contact":
        result = await _add_contact_tool(agent_id, arguments, user_id)
    elif tool_name == "remove_contact":
        result = await _remove_contact_tool(agent_id, arguments, user_id)
    elif tool_name == "send_feishu_message":
        result = await _send_feishu_message(
            agent_id,
            arguments,
            origin_session_id=session_id,
            origin_user_id=user_id,
            tool_call_id=tool_call_id,
            origin_turn_anchor_id=turn_anchor_id,
        )
    elif tool_name == "send_platform_message":
        result = await _send_platform_message(
            agent_id,
            arguments,
            origin_session_id=session_id,
            origin_user_id=user_id,
            tool_call_id=tool_call_id,
            origin_turn_anchor_id=turn_anchor_id,
        )
    elif tool_name == "send_channel_message":
        result = await _send_channel_message(
            agent_id,
            arguments,
            origin_session_id=session_id,
            origin_user_id=user_id,
            tool_call_id=tool_call_id,
            origin_turn_anchor_id=turn_anchor_id,
        )
    elif tool_name == "send_session_message":
        result = await _send_session_message(
            agent_id,
            arguments,
            origin_session_id=session_id,
            tool_call_id=tool_call_id,
            origin_turn_anchor_id=turn_anchor_id,
        )
    elif tool_name == "send_group_session_message":
        result = await _send_group_session_message(
            agent_id,
            arguments,
            origin_session_id=session_id,
            tool_call_id=tool_call_id,
            origin_turn_anchor_id=turn_anchor_id,
        )
    elif tool_name == "recall_message":
        raw_message_id = str(arguments.get("message_id") or "").strip()
        if not raw_message_id:
            result = json.dumps(
                {"status": "invalid_request", "reason": "message_id_required"},
                ensure_ascii=False,
            )
        else:
            result = json.dumps(
                await recall_message(
                    agent_id=agent_id,
                    message_id=raw_message_id,
                    user_id=user_id,
                    current_session_id=session_id,
                ),
                ensure_ascii=False,
            )
    elif tool_name == "start_dingtalk_channel_provisioning":
        result = await _start_dingtalk_channel_provisioning_tool(agent_id, user_id, arguments)
    elif tool_name == "get_dingtalk_channel_provisioning_status":
        result = await _get_dingtalk_channel_provisioning_status_tool(agent_id, user_id, arguments)
    elif tool_name == "send_message_to_agent":
        result = await _send_message_to_agent(
            agent_id,
            arguments,
            user_id=user_id,
            origin_session_id=session_id,
            tool_call_id=tool_call_id,
            origin_turn_anchor_id=turn_anchor_id,
        )
    elif tool_name == "send_file_to_agent":
        result = await _send_file_to_agent(
            agent_id,
            arguments,
            origin_session_id=session_id,
            tool_call_id=tool_call_id,
            origin_turn_anchor_id=turn_anchor_id,
        )
    elif tool_name == "send_channel_file":
        file_path = (arguments.get("file_path") or "").strip()
        if not file_path:
            result = "Error: file_path is required"
        else:
            result = await _run_with_temp_workspace(
                agent_id,
                _agent_tenant_id,
                lambda temp_ws: _send_channel_file(
                    agent_id,
                    temp_ws,
                    arguments,
                    tool_call_id=tool_call_id,
                    origin_session_id=session_id,
                    origin_turn_anchor_id=turn_anchor_id,
                ),
                paths=[file_path],
                source_paths=[file_path],
            )
    elif tool_name in {"send_media", "send_audio", "send_video"}:
        media_kind = (
            str(arguments.get("media_type") or "").strip().lower()
            if tool_name == "send_media"
            else ("audio" if tool_name == "send_audio" else "video")
        )
        file_path = (arguments.get("file_path") or "").strip()
        if media_kind not in {"audio", "video"}:
            result = json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "INVALID_MEDIA_TYPE",
                    "media_kind": media_kind or None,
                }
            )
        elif media_kind == "audio" and arguments.get("cover_image_path"):
            result = json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "COVER_NOT_ALLOWED_FOR_AUDIO",
                    "media_kind": media_kind,
                }
            )
        elif (
            not file_path
            and str(arguments.get("url_mode") or "").strip().lower() == "managed"
            and (
                replay_result := await _replay_terminal_media_delivery(
                    agent_id=agent_id,
                    origin_session_id=session_id,
                    intent_id=str(tool_call_id or ""),
                    origin_turn_anchor_id=turn_anchor_id,
                )
            )
            is not None
        ):
            result = json.dumps(replay_result, ensure_ascii=False)
        else:
            cover_path = str(arguments.get("cover_image_path") or "").strip() if media_kind == "video" else ""
            selected_paths = ([file_path] if file_path else []) + ([cover_path] if cover_path else [])
            oversized_code = None
            selected_sizes: list[int | None] = []
            storage = get_storage_backend()
            for selected_path in selected_paths:
                resolved = await _resolve_storage_source_path(agent_id, selected_path, _agent_tenant_id)
                if resolved.exists:
                    entry = await storage.stat(resolved.storage_key)
                    selected_sizes.append(entry.size)
                else:
                    selected_sizes.append(None)
            main_size = selected_sizes[0] if file_path and selected_sizes else None
            if main_size is not None:
                oversized_code = _media_materialization_size_error(
                    main_size,
                    selected_sizes[1] if len(selected_sizes) > 1 else None,
                )
            if oversized_code:
                result = json.dumps(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "failed",
                        "code": oversized_code,
                        "media_kind": media_kind,
                    },
                    ensure_ascii=False,
                )
            elif selected_paths:
                result = await _run_with_temp_workspace(
                    agent_id,
                    _agent_tenant_id,
                    lambda temp_ws: _send_channel_media(
                        agent_id,
                        temp_ws,
                        arguments,
                        media_kind=media_kind,
                        tool_call_id=tool_call_id,
                        origin_session_id=session_id,
                        origin_turn_anchor_id=turn_anchor_id,
                    ),
                    paths=selected_paths,
                    source_paths=selected_paths,
                    max_file_bytes=MEDIA_TOOL_MAX_FILE_BYTES,
                )
            else:
                result = await _send_channel_media(
                    agent_id,
                    _agent_workspace_root(agent_id),
                    arguments,
                    media_kind=media_kind,
                    tool_call_id=tool_call_id,
                    origin_session_id=session_id,
                    origin_turn_anchor_id=turn_anchor_id,
                )
        try:
            media_result_payload = json.loads(result)
        except (TypeError, ValueError, json.JSONDecodeError):
            media_result_payload = None
        if isinstance(media_result_payload, dict):
            result = json.dumps(
                _describe_media_delivery_result(media_result_payload),
                ensure_ascii=False,
            )
    elif tool_name == "web_search":
        result = await _web_search(arguments, agent_id)
    elif tool_name == "jina_search":
        result = await _jina_search(arguments)
    elif tool_name == "exa_search":
        result = await _exa_search(arguments, agent_id)
    elif tool_name == "duckduckgo_search":
        result = await _duckduckgo_search_tool(arguments)
    elif tool_name == "tavily_search":
        result = await _tavily_search_tool(arguments, agent_id)
    elif tool_name == "google_search":
        result = await _google_search_tool(arguments, agent_id)
    elif tool_name == "bing_search":
        result = await _bing_search_tool(arguments, agent_id)
    elif tool_name == "jina_read":
        result = await _jina_read(arguments)
    elif tool_name == "read_webpage":
        result = await _read_webpage(arguments)
    elif tool_name == "plaza_get_new_posts":
        result = await _plaza_get_new_posts(agent_id, arguments)
    elif tool_name == "plaza_create_post":
        result = await _plaza_create_post(agent_id, arguments)
    elif tool_name == "plaza_add_comment":
        result = await _plaza_add_comment(agent_id, arguments)
    elif tool_name in _CODE_EXEC_TOOL_NAMES:
        logger.info(f"[DirectTool] Executing code ({tool_name}) with arguments: {arguments}")
        if project_sandbox_scope is not None:
            project, project_member, project_run = project_sandbox_scope
            action = str(arguments.get("action") or "execute").strip().casefold()
            execution_mode = str(arguments.get("execution_mode") or "foreground").strip().casefold()
            if action != "execute" or execution_mode != "foreground":
                return (
                    "❌ Project repository sandbox execution is foreground-only. "
                    "Use workspace='agent' for persistent background jobs."
                )
            from app.services.project_git_service import create_project_sandbox_workspace

            sandbox_workspace = await create_project_sandbox_workspace(
                project,
                max_file_bytes=TOOL_MATERIALIZE_MAX_FILE_BYTES,
                max_total_bytes=TOOL_MATERIALIZE_MAX_TOTAL_BYTES,
            )
            try:
                result = await _execute_code(
                    agent_id,
                    sandbox_workspace.root,
                    arguments,
                    tool_name=tool_name,
                    user_id=user_id,
                    session_id=session_id,
                    turn_anchor_id=turn_anchor_id,
                    tools_for_llm=tools_for_llm,
                    on_output=on_output,
                    work_dir_override=sandbox_workspace.root,
                    hardened_workspace=True,
                    venv_path_override=sandbox_workspace.venv_root,
                    runtime_temp_path_override=sandbox_workspace.runtime_temp_root,
                )
                try:
                    committed = await finalize_project_sandbox_changes(
                        project,
                        project_member,
                        project_run,
                        sandbox_workspace,
                        agent_id=agent_id,
                        session_id=session_id,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                    )
                except Exception:
                    logger.exception("[ProjectSandbox] result commit failed")
                    return f"{result}\n\n❌ 项目文件提交未完成，请稍后重试。"
            finally:
                sandbox_workspace.cleanup()
            if committed is not None:
                result = (
                    f"{result}\n\nProject repository commit:\n"
                    + json.dumps(committed, ensure_ascii=False, indent=2)
                )
        else:
            async def run_code(workspace):
                return await _execute_code(
                    agent_id,
                    workspace,
                    arguments,
                    tool_name=tool_name,
                    user_id=user_id,
                    session_id=session_id,
                    turn_anchor_id=turn_anchor_id,
                    tools_for_llm=tools_for_llm,
                    on_output=on_output,
                )

            if tool_name == "execute_code_aio":
                # AIO executes against the Agent workspace bind-mounted at
                # /data/agents. Materializing a second workspace here cannot
                # participate in execution or capture its changes, and adds a
                # full copy plus scan to every call.
                result = await run_code(ws)
            else:
                result = await _run_with_temp_workspace(
                    agent_id,
                    _agent_tenant_id,
                    run_code,
                    sync_back=True,
                )
    elif tool_name == "sql_execute":
        result = await _sql_execute(arguments)
    elif tool_name == "upload_image":
        file_path = (arguments.get("file_path") or "").strip()
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _upload_image(agent_id, temp_ws, arguments),
            paths=_non_empty_paths(file_path),
            source_paths=_non_empty_paths(file_path),
        )
    elif tool_name == "generate_image_siliconflow":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _generate_image(agent_id, temp_ws, arguments, "siliconflow"),
            sync_back=True,
        )
    elif tool_name == "generate_image_openai":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _generate_image(agent_id, temp_ws, arguments, "openai"),
            sync_back=True,
        )
    elif tool_name == "generate_image_google":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _generate_image(agent_id, temp_ws, arguments, "google"),
            sync_back=True,
        )
    elif tool_name == "generate_image_custom":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _generate_image(agent_id, temp_ws, arguments, "custom"),
            sync_back=True,
        )
    else:
        return None

    return result
