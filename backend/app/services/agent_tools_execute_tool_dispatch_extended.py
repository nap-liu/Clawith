from __future__ import annotations

import uuid

from app.services.agent_tools import (
    _agentbay_browser_click,
    _agentbay_browser_extract,
    _agentbay_browser_login,
    _agentbay_browser_navigate,
    _agentbay_browser_observe,
    _agentbay_browser_save_screenshot,
    _agentbay_browser_screenshot,
    _agentbay_browser_type,
    _agentbay_code_edit_file,
    _agentbay_code_execute,
    _agentbay_code_read_file,
    _agentbay_code_write_file,
    _agentbay_command_exec,
    _agentbay_computer_activate_window,
    _agentbay_computer_click,
    _agentbay_computer_close_window,
    _agentbay_computer_dismiss_dialog,
    _agentbay_computer_drag_mouse,
    _agentbay_computer_get_active_window,
    _agentbay_computer_get_cursor_position,
    _agentbay_computer_get_installed_apps,
    _agentbay_computer_get_screen_size,
    _agentbay_computer_input_text,
    _agentbay_computer_list_visible_apps,
    _agentbay_computer_list_windows,
    _agentbay_computer_move_mouse,
    _agentbay_computer_precision_screenshot,
    _agentbay_computer_press_keys,
    _agentbay_computer_save_screenshot,
    _agentbay_computer_scroll,
    _agentbay_computer_screenshot,
    _agentbay_computer_start_app,
    _agentbay_file_transfer,
    _bitable_create_app,
    _bitable_create_record,
    _bitable_delete_record,
    _bitable_list_fields,
    _bitable_list_tables,
    _bitable_query_records,
    _bitable_update_record,
    _browse,
    _collect_okr_progress,
    _create_key_result,
    _create_objective,
    _discover_resources,
    _execute_cli_tool,
    _execute_mcp_tool,
    _feishu_approval_create,
    _feishu_approval_get,
    _feishu_approval_query,
    _feishu_calendar_create,
    _feishu_calendar_delete,
    _feishu_calendar_list,
    _feishu_calendar_update,
    _feishu_doc_append,
    _feishu_doc_create,
    _feishu_doc_read,
    _feishu_doc_search,
    _feishu_drive_delete,
    _feishu_drive_share,
    _feishu_user_search,
    _feishu_wiki_list,
    _generate_monthly_okr_report,
    _generate_okr_report,
    _get_my_okr,
    _get_okr,
    _get_okr_settings_tool,
    _handle_email_tool,
    _import_mcp_server,
    _install_skill,
    _install_skill_from_market,
    _list_page_access_requests,
    _list_published_pages,
    _publish_page,
    _publish_skill_to_market,
    _search_clawhub,
    _search_page_viewers,
    _search_skill_market,
    _update_any_kr_progress,
    _update_kr_content,
    _update_kr_progress,
    _update_objective,
    _update_published_page_access,
    _upsert_member_daily_report,
    _web_cdp,
    _web_eval,
    _web_open,
    _web_screenshot,
    _withdraw_skill_from_market,
)
from app.services.agent_tools_deploy_ops import (
    _neon_create_database,
    _vercel_deploy,
    _vercel_get_deploy_logs,
    _vercel_list_deployments,
    _vercel_manage_domain,
    _vercel_set_env,
)
from app.services.agent_tools_execute_tool_preflight import ExecuteToolDispatchContext
from app.services.agent_tools_temp_workspace_exec import _run_with_temp_workspace

_ROOT_TOOL_SYMBOLS = (
    "_agentbay_browser_click",
    "_agentbay_browser_extract",
    "_agentbay_browser_login",
    "_agentbay_browser_navigate",
    "_agentbay_browser_observe",
    "_agentbay_browser_save_screenshot",
    "_agentbay_browser_screenshot",
    "_agentbay_browser_type",
    "_agentbay_code_edit_file",
    "_agentbay_code_execute",
    "_agentbay_code_read_file",
    "_agentbay_code_write_file",
    "_agentbay_command_exec",
    "_agentbay_computer_activate_window",
    "_agentbay_computer_click",
    "_agentbay_computer_close_window",
    "_agentbay_computer_dismiss_dialog",
    "_agentbay_computer_drag_mouse",
    "_agentbay_computer_get_active_window",
    "_agentbay_computer_get_cursor_position",
    "_agentbay_computer_get_installed_apps",
    "_agentbay_computer_get_screen_size",
    "_agentbay_computer_input_text",
    "_agentbay_computer_list_visible_apps",
    "_agentbay_computer_list_windows",
    "_agentbay_computer_move_mouse",
    "_agentbay_computer_precision_screenshot",
    "_agentbay_computer_press_keys",
    "_agentbay_computer_save_screenshot",
    "_agentbay_computer_scroll",
    "_agentbay_computer_screenshot",
    "_agentbay_computer_start_app",
    "_agentbay_file_transfer",
    "_bitable_create_app",
    "_bitable_create_record",
    "_bitable_delete_record",
    "_bitable_list_fields",
    "_bitable_list_tables",
    "_bitable_query_records",
    "_bitable_update_record",
    "_browse",
    "_collect_okr_progress",
    "_create_key_result",
    "_create_objective",
    "_discover_resources",
    "_execute_cli_tool",
    "_execute_mcp_tool",
    "_feishu_approval_create",
    "_feishu_approval_get",
    "_feishu_approval_query",
    "_feishu_calendar_create",
    "_feishu_calendar_delete",
    "_feishu_calendar_list",
    "_feishu_calendar_update",
    "_feishu_doc_append",
    "_feishu_doc_create",
    "_feishu_doc_read",
    "_feishu_doc_search",
    "_feishu_drive_delete",
    "_feishu_drive_share",
    "_feishu_user_search",
    "_feishu_wiki_list",
    "_generate_monthly_okr_report",
    "_generate_okr_report",
    "_get_my_okr",
    "_get_okr",
    "_get_okr_settings_tool",
    "_handle_email_tool",
    "_import_mcp_server",
    "_install_skill",
    "_install_skill_from_market",
    "_list_page_access_requests",
    "_list_published_pages",
    "_publish_page",
    "_publish_skill_to_market",
    "_search_clawhub",
    "_search_page_viewers",
    "_search_skill_market",
    "_update_any_kr_progress",
    "_update_kr_content",
    "_update_kr_progress",
    "_update_objective",
    "_update_published_page_access",
    "_upsert_member_daily_report",
    "_web_cdp",
    "_web_eval",
    "_web_open",
    "_web_screenshot",
    "_withdraw_skill_from_market",
)


def _sync_root_tool_symbols() -> None:
    from app.services import agent_tools as _agent_tools_root

    for _name in _ROOT_TOOL_SYMBOLS:
        globals()[_name] = getattr(_agent_tools_root, _name)


async def execute_tool_dispatch_extended(state: ExecuteToolDispatchContext) -> str:
    _sync_root_tool_symbols()
    tool_name = state.tool_name
    arguments = state.arguments
    agent_id = state.agent_id
    user_id = state.user_id
    session_id = state.session_id
    tool_call_id = state.tool_call_id
    turn_anchor_id = state.turn_anchor_id
    _agent_tenant_id = state.agent_tenant_id
    ws = state.ws

    if tool_name == "discover_resources":
        result = await _discover_resources(agent_id, arguments)
    elif tool_name == "import_mcp_server":
        result = await _import_mcp_server(agent_id, arguments)
    elif tool_name == "list_installed_mcp_servers":
        from app.services.agent_mcp_lifecycle import list_installed_mcp_servers

        result = await list_installed_mcp_servers(agent_id)
    elif tool_name == "refresh_mcp_server":
        from app.services.agent_mcp_lifecycle import refresh_mcp_server

        try:
            _server_id = uuid.UUID(str(arguments.get("mcp_server_id") or ""))
        except (ValueError, TypeError):
            result = "❌ mcp_server_id must be an exact UUID from list_installed_mcp_servers."
        else:
            result = await refresh_mcp_server(
                agent_id,
                _server_id,
                user_id=user_id,
                session_id=session_id,
            )
    elif tool_name == "uninstall_mcp_server":
        from app.services.agent_mcp_lifecycle import uninstall_mcp_server

        try:
            _server_id = uuid.UUID(str(arguments.get("mcp_server_id") or ""))
        except (ValueError, TypeError):
            result = "❌ mcp_server_id must be an exact UUID from list_installed_mcp_servers."
        else:
            result = await uninstall_mcp_server(agent_id, _server_id)
    # ── Feishu Bitable Tools ──
    elif tool_name == "bitable_create_app":
        result = await _bitable_create_app(agent_id, arguments)
    elif tool_name == "bitable_list_tables":
        result = await _bitable_list_tables(agent_id, arguments)
    elif tool_name == "bitable_list_fields":
        result = await _bitable_list_fields(agent_id, arguments)
    elif tool_name == "bitable_query_records":
        result = await _bitable_query_records(agent_id, arguments)
    elif tool_name == "bitable_create_record":
        result = await _bitable_create_record(agent_id, arguments)
    elif tool_name == "bitable_update_record":
        result = await _bitable_update_record(agent_id, arguments)
    elif tool_name == "bitable_delete_record":
        result = await _bitable_delete_record(agent_id, arguments)
    # ── Feishu Document Tools ──
    elif tool_name == "feishu_doc_search":
        result = await _feishu_doc_search(agent_id, arguments)
    elif tool_name == "feishu_wiki_list":
        result = await _feishu_wiki_list(agent_id, arguments)
    elif tool_name == "feishu_doc_read":
        result = await _feishu_doc_read(agent_id, arguments)
    elif tool_name == "feishu_doc_create":
        result = await _feishu_doc_create(agent_id, arguments)
    elif tool_name == "feishu_doc_append":
        result = await _feishu_doc_append(agent_id, arguments)
    # ── Feishu Calendar Tools ──
    elif tool_name == "feishu_drive_share":
        result = await _feishu_drive_share(agent_id, arguments)
    elif tool_name == "feishu_drive_delete":
        result = await _feishu_drive_delete(agent_id, arguments)
    elif tool_name == "feishu_user_search":
        result = await _feishu_user_search(agent_id, arguments)
    elif tool_name == "feishu_calendar_list":
        result = await _feishu_calendar_list(agent_id, arguments)
    elif tool_name == "feishu_calendar_create":
        result = await _feishu_calendar_create(agent_id, arguments)
    elif tool_name == "feishu_calendar_update":
        result = await _feishu_calendar_update(agent_id, arguments)
    elif tool_name == "feishu_calendar_delete":
        result = await _feishu_calendar_delete(agent_id, arguments)
    elif tool_name == "feishu_approval_create":
        result = await _feishu_approval_create(agent_id, arguments)
    elif tool_name == "feishu_approval_query":
        result = await _feishu_approval_query(agent_id, arguments)
    elif tool_name == "feishu_approval_get":
        result = await _feishu_approval_get(agent_id, arguments)
    # ── Email Tools ──
    elif tool_name in ("send_email", "read_emails", "reply_email"):
        result = await _handle_email_tool(tool_name, agent_id, ws, arguments)
    # ── Pages: public HTML hosting ──
    elif tool_name == "publish_page":
        result = await _publish_page(agent_id, user_id, ws, arguments)
    elif tool_name == "list_published_pages":
        result = await _list_published_pages(agent_id)
    elif tool_name == "list_page_access_requests":
        result = await _list_page_access_requests(agent_id, user_id, arguments)
    elif tool_name == "search_page_viewers":
        result = await _search_page_viewers(agent_id, user_id, arguments)
    elif tool_name == "update_published_page_access":
        result = await _update_published_page_access(agent_id, user_id, arguments)
    # ── aio-sandbox Browser ──
    elif tool_name == "browse":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _browse(agent_id, temp_ws, arguments, user_id=user_id, session_id=session_id),
            sync_back=True,
        )
    elif tool_name == "web_open":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _web_open(agent_id, temp_ws, arguments, user_id=user_id, session_id=session_id),
            sync_back=True,
        )
    elif tool_name == "web_eval":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _web_eval(agent_id, temp_ws, arguments, user_id=user_id, session_id=session_id),
            sync_back=True,
        )
    elif tool_name == "web_cdp":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _web_cdp(agent_id, temp_ws, arguments, user_id=user_id, session_id=session_id),
            sync_back=True,
        )
    elif tool_name == "web_screenshot":
        result = await _run_with_temp_workspace(
            agent_id,
            _agent_tenant_id,
            lambda temp_ws: _web_screenshot(agent_id, temp_ws, arguments, user_id=user_id, session_id=session_id),
            sync_back=True,
        )
    # ── AgentBay Tools ──
    elif tool_name == "agentbay_browser_navigate":
        result = await _agentbay_browser_navigate(agent_id, ws, arguments)
    elif tool_name == "agentbay_browser_screenshot":
        result = await _agentbay_browser_screenshot(agent_id, ws, arguments)
    elif tool_name == "agentbay_browser_save_screenshot":
        result = await _agentbay_browser_save_screenshot(agent_id, ws, arguments)
    elif tool_name == "agentbay_browser_click":
        result = await _agentbay_browser_click(agent_id, ws, arguments)
    elif tool_name == "agentbay_browser_type":
        result = await _agentbay_browser_type(agent_id, ws, arguments)
    elif tool_name == "agentbay_code_execute":
        result = await _agentbay_code_execute(agent_id, ws, arguments)
    elif tool_name == "agentbay_code_write_file":
        result = await _agentbay_code_write_file(agent_id, ws, arguments)
    elif tool_name == "agentbay_code_read_file":
        result = await _agentbay_code_read_file(agent_id, ws, arguments)
    elif tool_name == "agentbay_code_edit_file":
        result = await _agentbay_code_edit_file(agent_id, ws, arguments)
    elif tool_name == "agentbay_browser_extract":
        result = await _agentbay_browser_extract(agent_id, ws, arguments)
    elif tool_name == "agentbay_browser_observe":
        result = await _agentbay_browser_observe(agent_id, ws, arguments)
    elif tool_name == "agentbay_browser_login":
        result = await _agentbay_browser_login(agent_id, ws, arguments)
    elif tool_name == "agentbay_command_exec":
        result = await _agentbay_command_exec(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_screenshot":
        result = await _agentbay_computer_screenshot(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_save_screenshot":
        result = await _agentbay_computer_save_screenshot(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_precision_screenshot":
        result = await _agentbay_computer_precision_screenshot(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_click":
        result = await _agentbay_computer_click(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_input_text":
        result = await _agentbay_computer_input_text(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_press_keys":
        result = await _agentbay_computer_press_keys(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_scroll":
        result = await _agentbay_computer_scroll(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_move_mouse":
        result = await _agentbay_computer_move_mouse(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_drag_mouse":
        result = await _agentbay_computer_drag_mouse(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_get_screen_size":
        result = await _agentbay_computer_get_screen_size(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_start_app":
        result = await _agentbay_computer_start_app(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_get_installed_apps":
        result = await _agentbay_computer_get_installed_apps(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_get_cursor_position":
        result = await _agentbay_computer_get_cursor_position(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_get_active_window":
        result = await _agentbay_computer_get_active_window(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_list_windows":
        result = await _agentbay_computer_list_windows(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_activate_window":
        result = await _agentbay_computer_activate_window(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_close_window":
        result = await _agentbay_computer_close_window(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_dismiss_dialog":
        result = await _agentbay_computer_dismiss_dialog(agent_id, ws, arguments)
    elif tool_name == "agentbay_computer_list_visible_apps":
        result = await _agentbay_computer_list_visible_apps(agent_id, ws, arguments)
    elif tool_name == "agentbay_file_transfer":
        result = await _agentbay_file_transfer(agent_id, ws, arguments)
    # ── Skill Management ──
    elif tool_name == "search_clawhub":
        result = await _search_clawhub(agent_id, arguments)
    elif tool_name == "install_skill":
        result = await _install_skill(agent_id, ws, arguments)
    elif tool_name == "search_skill_market":
        result = await _search_skill_market(agent_id, arguments)
    elif tool_name == "install_skill_from_market":
        result = await _install_skill_from_market(
            agent_id,
            user_id,
            arguments,
            session_id=session_id,
            turn_anchor_id=turn_anchor_id,
        )
    elif tool_name == "publish_skill_to_market":
        result = await _publish_skill_to_market(agent_id, user_id, arguments)
    elif tool_name == "withdraw_skill_from_market":
        result = await _withdraw_skill_from_market(agent_id, user_id, arguments)
    # ── OKR Tools ──
    elif tool_name == "get_okr":
        result = await _get_okr(agent_id, arguments)
    elif tool_name == "get_my_okr":
        result = await _get_my_okr(agent_id, arguments)
    elif tool_name == "update_kr_content":
        result = await _update_kr_content(agent_id, user_id, arguments)
    elif tool_name == "update_kr_progress":
        result = await _update_kr_progress(agent_id, user_id, arguments)
    # collect_okr_progress: legacy batch progress collection
    elif tool_name == "collect_okr_progress":
        result = await _collect_okr_progress(agent_id)
    # generate_okr_report: build daily/weekly structured report and store it
    elif tool_name == "generate_okr_report":
        result = await _generate_okr_report(agent_id, arguments)
    # get_okr_settings: read tenant OKR configuration for scheduling decisions
    elif tool_name == "get_okr_settings":
        result = await _get_okr_settings_tool(agent_id)
    # ── OKR Management Tools (OKR Agent exclusive) ──
    elif tool_name == "create_objective":
        result = await _create_objective(agent_id, user_id, arguments)
    elif tool_name == "create_key_result":
        result = await _create_key_result(agent_id, user_id, arguments)
    elif tool_name == "update_objective":
        result = await _update_objective(agent_id, user_id, arguments)
    elif tool_name == "update_any_kr_progress":
        result = await _update_any_kr_progress(agent_id, user_id, arguments)
    # generate_monthly_okr_report: produce the monthly summary report
    elif tool_name == "generate_monthly_okr_report":
        result = await _generate_monthly_okr_report(agent_id)
    elif tool_name == "upsert_member_daily_report":
        result = await _upsert_member_daily_report(agent_id, arguments)
    # ── Scene management (strict current-manager session gate) ──
    elif tool_name == "manage_scene":
        from app.services.scene_service import execute_scene_management_tool

        result = await execute_scene_management_tool(
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
            arguments=arguments,
        )
    # ── Vercel & Neon Deploy Tools ──
    elif tool_name == "vercel_deploy":
        result = await _vercel_deploy(agent_id, ws, arguments)
    elif tool_name == "vercel_list_deployments":
        result = await _vercel_list_deployments(agent_id, arguments)
    elif tool_name == "vercel_get_deploy_logs":
        result = await _vercel_get_deploy_logs(agent_id, arguments)
    elif tool_name == "vercel_set_env":
        result = await _vercel_set_env(agent_id, arguments)
    elif tool_name == "vercel_manage_domain":
        result = await _vercel_manage_domain(agent_id, arguments)
    elif tool_name == "neon_create_database":
        result = await _neon_create_database(agent_id, arguments)
    else:
        # CLI tools (type='cli') are standalone functions executed in the
        # aio sandbox with that tool's auth injected. Try CLI first; if
        # tool_name is not a CLI tool for this agent, fall through to MCP.
        cli_result = await _execute_cli_tool(
            agent_id, ws, tool_name, arguments, user_id=user_id, session_id=session_id
        )
        if cli_result is not None:
            result = cli_result
        else:
            result = await _execute_mcp_tool(
                tool_name,
                arguments,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                tool_call_id=tool_call_id,
            )

    return result
