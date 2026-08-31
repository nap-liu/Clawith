"""Ordered facade specifications for the independently split agent-tool modules."""

from __future__ import annotations

from app.services.agent_tools_facade import FacadeSpec


def _names(value: str) -> tuple[str, ...]:
    return tuple(value.split())


def _spec(
    module_name: str,
    exports: str = "",
    sync: str = "",
    *,
    sync_exports: bool = True,
) -> FacadeSpec:
    return f"app.services.{module_name}", _names(exports), _names(sync), sync_exports


MAIN_FACADE_SPECS: tuple[FacadeSpec, ...] = (
    _spec(
        "agent_tools_outbound_core",
        """
        _build_outbound_operation_key _lock_outbound_operation
        _outbound_operation_lock_id _outbound_operation_lifecycle_lock
        _locked_outbound_media_connection _outbound_media_db_session
        _find_outbound_tool_receipt _persist_outbound_channel_message
        _duplicate_outbound_claim_result _session_message_result
        _session_receipt_replay_status
        """,
        sync_exports=False,
    ),
    _spec(
        "agent_tools_image_ops",
        """
        _upload_image _generate_image _generate_image_siliconflow
        _generate_image_openai _json_path_get _render_json_template
        _json_structure_preview _find_first_image_reference
        _custom_image_reference_to_bytes _generate_image_custom_api
        _generate_image_google
        """,
    ),
    _spec(
        "agent_tools_mcp_runtime",
        "_execute_mcp_tool _execute_via_smithery_connect _smithery_auto_recover",
        """
        async_session get_settings SandboxMcpHost SandboxMcpHubClient
        current_agent_runtime_workspace _decrypt_sensitive_fields
        _agent_workspace_root
        """,
    ),
    _spec(
        "agent_tools_mcp_import_ops",
        "_discover_resources _import_mcp_server",
        sync_exports=False,
    ),
    _spec(
        "agent_tools_media_delivery_core",
        """
        _PLATFORM_SESSION_CHANNELS _preflight_managed_media_target
        _has_configured_dingtalk_media_channel _MEDIA_DELIVERY_MESSAGES
        _describe_media_delivery_result _sniff_media_file_kind
        _sniff_media_file_mime _sniff_image_file _external_media_filename
        """,
        "async_session select RecipientResolutionError resolve_human_channel_recipient",
    ),
    _spec(
        "agent_tools_message_transports",
        "_send_feishu_message _send_dingtalk_message _send_wecom_message _send_slack_message",
        """
        async_session logger select _persist_outbound_channel_message
        _duplicate_outbound_claim_result append_delivery_part register_delivery
        find_or_create_channel_session get_platform_user_by_org_member
        resolve_human_channel_recipient sanitize_user_visible_text
        """,
    ),
    _spec(
        "agent_tools_platform_transports",
        "_send_teams_channel_message _send_wechat_channel_message _send_platform_message",
        """
        async_session logger select _persist_outbound_channel_message
        _duplicate_outbound_claim_result append_delivery_part register_delivery
        find_or_create_channel_session get_platform_user_by_org_member
        resolve_platform_user_recipient sanitize_user_visible_text
        """,
    ),
    _spec(
        "agent_tools_session_messages",
        "_send_exact_session_message _send_session_message _send_group_session_message _send_channel_message",
        """
        async_session logger select _GROUP_SESSION_DENIAL
        _LEGACY_GROUP_SESSION_CHANNELS _PLATFORM_SESSION_CHANNELS
        _SESSION_MESSAGE_CAPABILITIES _SESSION_MESSAGE_DENIAL
        _build_outbound_operation_key _lock_outbound_operation
        _persist_outbound_channel_message _session_message_result
        _session_receipt_replay_status _send_dingtalk_message
        _send_feishu_message _send_slack_message _send_platform_message
        _send_teams_channel_message _send_wechat_channel_message
        _send_wecom_message append_delivery_part register_delivery
        prepare_group_user_mentions deliver_message_with_receipt
        sanitize_user_visible_text
        """,
    ),
    _spec(
        "agent_tools_sandbox_web_ops",
        """
        _check_code_safety build_cli_injection _resolve_sandbox_backend
        _browse _web_open _web_eval _web_cdp _web_screenshot
        """,
        "async_session AgentModel _get_tool_config agent_tool_enabled",
    ),
    _spec(
        "agent_tools_code_runtime",
        "_execute_code _is_cli_tool_name _execute_cli_tool _execute_code_legacy",
        """
        logger _get_tool_config _agent_workspace_root
        _canonicalize_execute_code_upload_paths _check_code_safety
        build_cli_injection
        """,
    ),
    _spec(
        "agent_tools_web_ops",
        """
        _web_search _exa_search _duckduckgo_search_tool _tavily_search_tool
        _google_search_tool _bing_search_tool
        """,
        """
        get_settings _get_tool_config _search_bing _search_duckduckgo
        _search_exa _search_google _search_tavily
        """,
    ),
    _spec(
        "agent_tools_plaza_ops",
        "_plaza_get_new_posts _plaza_create_post _plaza_add_comment",
        "async_session select",
    ),
    _spec(
        "agent_tools_feishu_auth",
        """
        _get_feishu_token _get_agent_calendar_id _feishu_resolve_open_id
        _iso_to_ts _get_feishu_credentials _get_feishu_tenant_doc_url
        _get_feishu_bitable_url _parse_feishu_url _check_feishu_err
        """,
        "async_session select",
    ),
    _spec(
        "agent_tools_file_delivery_runtime",
        """
        _send_file_to_session _send_file_to_recipient _send_file_via_feishu
        _send_file_via_slack _send_file_via_slack_channel
        """,
        """
        async_session logger select _agent_workspace_root
        record_channel_file_part resolve_human_channel_recipient
        sanitize_user_visible_text
        """,
    ),
    _spec(
        "agent_tools_channel_file_delivery",
        "_send_channel_file",
        """
        _agent_workspace_root _claim_channel_file_receipt
        _normalize_tool_workspace_rel_path _platform_file_delivery_result
        _supports_exact_file_session_route _send_file_to_session
        _send_file_to_recipient _sniff_media_file_kind append_delivery_part
        register_delivery channel_file_sender channel_file_part_recorder
        sanitize_user_visible_text
        """,
    ),
    _spec(
        "agent_tools_media_delivery_replay_publish",
        """
        _replay_terminal_media_delivery
        _replay_terminal_media_delivery_under_slot
        _publish_external_media_to_session
        """,
        """
        async_session _outbound_media_slots _lock_outbound_operation
        _build_outbound_operation_key _PLATFORM_SESSION_CHANNELS
        _describe_media_delivery_result _external_media_filename
        """,
    ),
    _spec(
        "agent_tools_media_delivery_runtime",
        "_send_media_to_session _send_media_to_session_under_lifecycle_lock",
        """
        logger select ChatMessage ChannelConfig ChatSession TurnRuntime
        deliver_message_with_receipt attachment_from_workspace_path
        canonical_media_mime normalize_media_display_title
        DeliveryReceiptPersistenceError IMDeliveryPart IMDeliveryResult
        _merge_delivery_into_meta _build_outbound_operation_key
        _outbound_operation_lifecycle_lock _outbound_media_db_session
        _PLATFORM_SESSION_CHANNELS _describe_media_delivery_result
        _sniff_media_file_mime
        """,
    ),
    _spec(
        "agent_tools_media_delivery_recipient",
        "_send_media_to_recipient",
        "async_session find_or_create_channel_session _send_media_to_session",
    ),
    _spec(
        "agent_tools_media_delivery_entry",
        "MEDIA_TOOL_MAX_FILE_BYTES _send_channel_media",
        """
        logger MEDIA_TOOL_MAX_FILE_BYTES MediaUrlError
        current_agent_runtime_workspace get_storage_backend
        import_managed_media_url normalize_managed_media_headers
        normalize_media_display_title sanitize_user_visible_text
        validate_media_url _agent_workspace_root _build_outbound_operation_key
        _describe_media_delivery_result _get_tool_config
        _normalize_tool_workspace_rel_path _preflight_managed_media_target
        _publish_external_media_to_session _replay_terminal_media_delivery
        _send_media_to_recipient _send_media_to_session _sniff_image_file
        _sniff_media_file_mime
        """,
    ),
    _spec(
        "agent_tools_trigger_ops",
        sync="""
        async_session logger select ensure_focus_item RecipientResolutionError
        resolve_agent_recipient resolve_platform_user_recipient
        _handle_set_trigger _handle_update_trigger _handle_cancel_trigger
        _handle_list_triggers
        """,
        sync_exports=False,
    ),
    _spec(
        "agent_tools_agentbay_browser_ops",
        """
        _agentbay_normalize_image_bytes _agentbay_save_image_to_workspace
        _agentbay_browser_navigate _agentbay_browser_screenshot
        _agentbay_browser_save_screenshot _agentbay_browser_click
        _agentbay_browser_type _agentbay_browser_extract
        _agentbay_browser_observe _agentbay_browser_login
        _agentbay_command_exec
        """,
        "logger",
    ),
    _spec(
        "agent_tools_agentbay_computer_apps",
        """
        _agentbay_normalize_text _agentbay_app_field _agentbay_format_apps
        _agentbay_find_installed_app_match _agentbay_uncertain_start_error
        _agentbay_visible_apps_note _agentbay_computer_start_app
        _agentbay_computer_get_installed_apps
        _agentbay_computer_list_visible_apps
        """,
        "logger",
    ),
    _spec(
        "agent_tools_agentbay_computer_screen",
        """
        _agentbay_extract_screen_dimensions _agentbay_get_screen_metadata
        _agentbay_image_dimensions _agentbay_crop_image_bytes
        _agentbay_expand_precision_crop _agentbay_desktop_coordinate_note
        _agentbay_computer_screenshot _agentbay_computer_save_screenshot
        _agentbay_computer_precision_screenshot
        _agentbay_computer_get_screen_size
        """,
        "logger _agentbay_normalize_image_bytes _agentbay_save_image_to_workspace",
    ),
    _spec(
        "agent_tools_agentbay_computer_window_input",
        """
        _agentbay_computer_click _agentbay_computer_input_text
        _agentbay_computer_press_keys _agentbay_computer_scroll
        _agentbay_computer_move_mouse _agentbay_computer_drag_mouse
        _agentbay_computer_get_cursor_position
        _agentbay_computer_get_active_window _agentbay_computer_activate_window
        _agentbay_computer_list_windows _agentbay_computer_close_window
        _agentbay_computer_dismiss_dialog
        """,
        "logger _agentbay_normalize_text _agentbay_get_screen_metadata",
    ),
    _spec(
        "agent_tools_agentbay_code_ops",
        """
        _agentbay_code_execute _agentbay_code_write_file
        _agentbay_code_read_file _agentbay_code_edit_file
        """,
        "logger",
    ),
    _spec("agent_tools_agentbay_transfer", "_agentbay_file_transfer", "logger"),
)


FILE_RECEIPTS_FACADE_SPECS: tuple[FacadeSpec, ...] = (
    _spec(
        "agent_tools_channel_file_receipts",
        """
        _claim_channel_file_receipt record_channel_file_part
        _supports_exact_file_session_route _normalize_tool_workspace_rel_path
        _platform_file_delivery_result
        """,
        """
        async_session _build_outbound_operation_key channel_file_sender
        channel_file_part_recorder append_delivery_part register_delivery
        """,
    ),
)


FEISHU_DOCS_FACADE_SPECS: tuple[FacadeSpec, ...] = (
    _spec(
        "agent_tools_feishu_docs",
        """
        _resolve_docx_document_token _feishu_read_doc _feishu_create_doc
        _feishu_append_doc _feishu_wiki_get_node _feishu_doc_search
        _feishu_wiki_list _feishu_doc_read _feishu_doc_create
        _parse_inline_markdown _markdown_to_feishu_blocks _feishu_doc_append
        """,
        """
        channel_feishu_sender_open_id _check_feishu_err
        _get_feishu_credentials _get_feishu_tenant_doc_url _parse_feishu_url
        """,
    ),
)


FEISHU_BITABLE_FACADE_SPECS: tuple[FacadeSpec, ...] = (
    _spec(
        "agent_tools_feishu_bitable",
        """
        _resolve_bitable_app_token _bitable_list_tables _bitable_create_app
        _bitable_list_fields _bitable_query_records _bitable_create_record
        _bitable_update_record _bitable_delete_record
        """,
        """
        _check_feishu_err _get_feishu_bitable_url _get_feishu_credentials
        _parse_feishu_url _feishu_wiki_get_node
        """,
    ),
)


FEISHU_COLLAB_FACADE_SPECS: tuple[FacadeSpec, ...] = (
    _spec(
        "agent_tools_feishu_collab_ops",
        """
        _feishu_drive_share _feishu_drive_delete _resolve_feishu_open_id
        _feishu_calendar_list _feishu_calendar_create _feishu_calendar_update
        _feishu_calendar_delete _feishu_approval_create _feishu_approval_query
        _feishu_approval_get _feishu_user_search
        """,
        """
        async_session select selectinload AgentModel AgentRelationship
        OrgMember UserModel channel_feishu_sender_open_id _check_feishu_err
        _get_agent_calendar_id _get_feishu_credentials _iso_to_ts
        _feishu_wiki_get_node RecipientResolutionError
        resolve_human_channel_recipient
        """,
    ),
)


OUTBOUND_STATE_FACADE_SPECS: tuple[FacadeSpec, ...] = (
    _spec(
        "agent_tools_outbound_core",
        sync="""
        async_session engine _outbound_media_connection _outbound_media_slots
        _build_outbound_operation_key _lock_outbound_operation
        _outbound_operation_lock_id _outbound_operation_lifecycle_lock
        _locked_outbound_media_connection _outbound_media_db_session
        _find_outbound_tool_receipt _persist_outbound_channel_message
        _duplicate_outbound_claim_result _session_message_result
        _session_receipt_replay_status
        """,
        sync_exports=False,
    ),
    _spec(
        "agent_tools_media_delivery_support",
        sync="""
        _outbound_media_connection _outbound_media_slots
        _lock_outbound_operation _outbound_operation_lock_id
        _outbound_operation_lifecycle_lock _locked_outbound_media_connection
        _outbound_media_db_session
        """,
        sync_exports=False,
    ),
)


A2A_DELIVERY_FACADE_SPECS: tuple[FacadeSpec, ...] = (
    _spec(
        "agent_tools_a2a_delivery",
        """
        _send_file_to_agent _resolve_a2a_target _create_on_message_trigger
        _arm_a2a_delegate_callback _append_focus_item _wake_agent_async
        """,
        """
        async_session logger _build_outbound_operation_key
        current_agent_runtime_workspace ensure_focus_item get_storage_backend
        project_agent_runtime_workspace standard_agent_runtime_workspace
        """,
    ),
)


A2A_MESSAGING_FACADE_SPECS: tuple[FacadeSpec, ...] = (
    _spec(
        "agent_tools_a2a_messaging",
        "_send_message_to_agent",
        """
        async_session logger A2A_DELIVERY_GUIDANCE
        _arm_a2a_delegate_callback _build_outbound_operation_key
        _lock_outbound_operation _wake_agent_async
        """,
    ),
)
