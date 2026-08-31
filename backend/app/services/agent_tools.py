"""Agent tools — unified file-based tools that give digital employees
access to their own structured workspace.

Design principle:  ONE set of file tools covers EVERYTHING.
The agent's workspace uses well-known paths:
  - soul.md             → personality definition
  - memory/memory.md    → long-term memory / notes
  - skills/             → skill definitions (markdown files)
  - workspace/          → general working files, reports, etc.

The agent reads/writes these files directly. No per-concept tools needed.
"""

import asyncio
import base64
from dataclasses import dataclass
import fnmatch
import json
import mimetypes
import multiprocessing as mp
import os
import queue
import tempfile
import uuid
import unicodedata
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Optional, Any
from urllib.parse import unquote, urlsplit
import re

from fastapi import HTTPException
from loguru import logger
from sqlalchemy import func, select, or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from app.database import async_session, engine
from app.core.okr_feature import OKR_TOOL_NAMES, is_retired_okr_tool, okr_feature_enabled
from app.models.task import Task
from app.models.agent import Agent as AgentModel
from app.models.org import AgentRelationship, OrgMember, AgentAgentRelationship
from app.models.audit import ChatMessage, AuditLog
from app.models.chat_compaction import ChatCompaction  # noqa: F401 - register ChatMessage FK target
from app.models.chat_session import ChatSession
from app.models.channel_config import ChannelConfig
from app.models.participant import Participant  # noqa: F401 - register chat FK target
from app.models.user import User as UserModel
from app.services.auth_registry import auth_provider_registry
from app.services.agent_memory import CORE_MEMORY_TEMPLATE
from app.services.agent_runtime_workspace import (
    current_agent_runtime_workspace,
    project_agent_runtime_workspace,
    standard_agent_runtime_workspace,
)
from app.services.channel_session import find_or_create_channel_session
from app.services.channel_user_service import get_platform_user_by_org_member
from app.services.chat_attachments import (
    attachment_from_workspace_path,
    canonical_media_mime,
    infer_attachment_kind,
    MEDIA_PROBE_CHUNK_BYTES,
    sniff_image_kind_bytes,
    sniff_media_mime_bytes,
)
from app.services.document_conversion import (
    convert_html_to_pdf as convert_html_file_to_pdf,
    convert_html_to_pptx as convert_html_file_to_pptx,
)
from app.services.focus_service import (
    complete_focus_item,
    ensure_focus_item,
    is_focus_file_path,
    list_focus_items,
    upsert_focus_item,
)
from app.services.workspace_collaboration import (
    delete_workspace_file,
    move_workspace_path,
    normalize_workspace_path,
    read_text_if_exists,
    write_workspace_file,
)
from app.services.storage import get_storage_backend, normalize_storage_key
from app.services.storage_runtime.base import WriteCondition, content_hash_bytes
from app.services.workspace_locking import workspace_locks
from app.core.permissions import evaluate_agent_relationship_status, evaluate_human_relationship_status
from app.services.access_relationships import ensure_access_granted_platform_relationships
from app.services.tool_enablement import agent_tool_enabled
from app.config import get_settings
from app.services.llm.confirmation_tool import REQUEST_CONFIRMATION_TOOL_NAME
from app.services.media_tool_contract import (
    SEND_MEDIA_FUNCTION_TOOL,
    normalize_media_display_title,
)
from app.services.media_url_source import (
    MediaUrlError,
    import_managed_media_url,
    normalize_managed_media_headers,
    validate_media_url,
)
from app.services.sandbox_mcp_host import SandboxMcpHost
from app.services.sandbox_mcp_hub_client import SandboxMcpHubClient
from app.services.recipient_resolver import (
    RecipientResolutionError,
    resolve_agent_recipient,
    resolve_human_recipient,
    resolve_human_channel_recipient,
    resolve_platform_user_recipient,
)
from app.services.im_delivery import (
    DELIVERY_LEASE,
    DeliveryReceiptPersistenceError,
    IMDeliveryPart,
    IMDeliveryResult,
    MentionIntent,
    _merge_delivery_into_meta,
    append_delivery_part,
    attach_delivery_to_meta,
    recall_message,
    register_delivery,
)
from app.services.dingtalk_group_mentions import (
    prepare_group_user_mentions,
)
from app.services.user_output import sanitize_user_visible_text
from app.services.turn_runtime import (
    TurnRuntime,
    deliver_message_to_runtime,
    deliver_message_with_receipt,
)
from app.services.user_project_tools import (
    USER_PROJECT_TOOL_NAMES,
    USER_PROJECT_TOOL_SCHEMAS,
    execute_user_project_tool,
    record_user_project_tool_activity,
    user_project_tool_error,
)
from app.services.agent_tools_catalog import AGENT_TOOLS
from app.services import agent_tools_document_tools as _agent_tools_document_tools_module
from app.services import agent_tools_feishu_auth as _agent_tools_feishu_auth_module
from app.services import agent_tools_file_delivery_runtime as _agent_tools_file_delivery_module
from app.services import agent_tools_code_runtime as _agent_tools_code_runtime_module
from app.services import agent_tools_channel_file_receipts as _agent_tools_file_receipts_module
from app.services import agent_tools_channel_file_delivery as _agent_tools_channel_file_module
from app.services import agent_tools_agentbay_browser_ops as _agent_tools_agentbay_browser_module
from app.services import agent_tools_agentbay_computer_apps as _agent_tools_agentbay_apps_module
from app.services import agent_tools_agentbay_computer_screen as _agent_tools_agentbay_screen_module
from app.services import agent_tools_agentbay_computer_window_input as _agent_tools_agentbay_window_module
from app.services import agent_tools_agentbay_code_ops as _agent_tools_agentbay_code_module
from app.services import agent_tools_agentbay_transfer as _agent_tools_agentbay_transfer_module
from app.services import agent_tools_image_ops as _agent_tools_image_ops_module
from app.services import agent_tools_media_delivery_core as _agent_tools_media_core_module
from app.services import agent_tools_message_transports as _agent_tools_message_transports_module
from app.services import agent_tools_mcp_runtime as _agent_tools_mcp_runtime_module
from app.services import agent_tools_mcp_import_ops as _agent_tools_mcp_import_module
from app.services import agent_tools_media_delivery_replay_publish as _agent_tools_media_replay_module
from app.services import agent_tools_media_delivery_runtime as _agent_tools_media_runtime_module
from app.services import agent_tools_media_delivery_recipient as _agent_tools_media_recipient_module
from app.services import agent_tools_media_delivery_entry as _agent_tools_media_entry_module
from app.services import agent_tools_media_delivery_support as _agent_tools_media_delivery_support_module
from app.services import agent_tools_outbound_core as _agent_tools_outbound_core_module
from app.services import agent_tools_plaza_ops as _agent_tools_plaza_ops_module
from app.services import agent_tools_platform_transports as _agent_tools_platform_transports_module
from app.services import agent_tools_sandbox_web_ops as _agent_tools_sandbox_web_ops_module
from app.services import agent_tools_session_messages as _agent_tools_session_messages_module
from app.services import agent_tools_trigger_ops as _agent_tools_trigger_ops_module
from app.services import agent_tools_web_ops as _agent_tools_web_ops_module
from app.services.agent_tools_facade import (
    export_module_symbols,
    prepare_exported_callables,
    register_sync_targets,
)
from app.services.agent_tools_file_support import _tool_storage_key
from app.services.agent_tools_media_limits import (
    TOOL_MATERIALIZE_MAX_FILE_BYTES,
    TOOL_MATERIALIZE_MAX_TOTAL_BYTES,
    _media_materialization_size_error,
)
TEMP_WORKSPACE_DEFAULT_PATHS = ["workspace", "memory", "skills", "focus.md", "soul.md", "HEARTBEAT.md"]

from app.services.agent_tools_document_tools import (
    _READ_DOCUMENT_FALLBACK_TIMEOUT_SECONDS,
    _READ_DOCUMENT_HARD_CHAR_CEILING,
    _READ_DOCUMENT_MAX_COLUMNS,
    _READ_DOCUMENT_MAX_FILE_BYTES,
    _READ_DOCUMENT_MAX_PAGES,
    _READ_DOCUMENT_MAX_ROWS,
    _READ_DOCUMENT_MAX_SHEETS,
    _READ_DOCUMENT_MAX_SLIDES,
    _READ_DOCUMENT_TIMEOUT_SECONDS,
    _convert_csv_to_xlsx,
    _convert_html_to_pdf,
    _convert_html_to_pptx,
    _convert_markdown_to_docx,
    _convert_markdown_to_pdf,
    _read_document as _read_document_impl,
    _read_document_from_storage as _read_document_from_storage_impl,
    _read_document_sync as _read_document_sync_impl,
    _read_document_with_timeout as _read_document_with_timeout_impl,
    _read_document_worker,
    _read_pdf_fast_sync,
    _read_pdf_fast_with_timeout,
    _read_pdf_fast_worker,
    _render_xlsx_row,
    _safe_document_cell_text,
)
from app.services.agent_tools_email_ops import (
    _get_email_config,
    _handle_email_tool,
)
from app.services.agent_tools_file_ops import (
    _delete_file,
    _edit_file,
    _find_files,
    _list_files,
    _read_file,
    _search_files,
    _write_file,
)
from app.services.agent_tools_file_support import (
    WORKSPACE_ROOT,
    _ResolvedStorageSource,
    _agent_workspace_root,
    _canonicalize_execute_code_upload_paths,
    _collapse_filename_for_match,
    _display_size,
    _exact_storage_source_error,
    _get_agent_tenant_id,
    _is_enterprise_info_path,
    _is_webhook_inbox_path,
    _non_empty_paths,
    _normalize_tool_rel_path,
    _relative_storage_display,
    _resolve_exact_storage_source_path,
    _resolve_storage_source_path,
    _resolve_tool_source_path,
    _resolve_tool_target_path,
    _storage_find_files,
    _storage_list_dir,
    _storage_read_file,
    _storage_search_files,
    _storage_source_error,
    _storage_walk_files,
    _tool_storage_key,
)
from app.services.agent_tools_config_runtime import (
    SENSITIVE_FIELD_KEYS,
    _TOOL_CONFIG_CACHE_TTL_SECONDS,
    _decrypt_sensitive_fields,
    _get_cached_tool_config,
    _get_tool_config,
    _set_cached_tool_config,
    _tool_config_cache,
    invalidate_tool_config_cache,
)
from app.services.agent_tools_deploy_ops import (
    _check_neon_quota_limit,
    _get_vercel_quota_summary,
    _get_vercel_token,
    _neon_create_database,
    _vercel_deploy,
    _vercel_get_deploy_logs,
    _vercel_list_deployments,
    _vercel_manage_domain,
    _vercel_set_env,
)
from app.services import agent_tools_deploy_ops as _agent_tools_deploy_ops_module
from app.services.agent_tools_pages_ops import (
    _list_page_access_requests,
    _list_published_pages,
    _publish_page,
    _replace_page_allowed_users,
    _resolve_public_base_url,
    _search_page_viewers,
    _update_published_page_access,
)
from app.services.agent_tools_okr_management import (
    _create_key_result,
    _create_objective,
    _update_any_kr_progress,
    _update_objective,
    _upsert_member_daily_report,
)
from app.services.agent_tools_okr_queries import (
    _can_access_existing_okr_target,
    _can_create_okr_target,
    _collect_okr_progress,
    _compute_okr_period_bounds,
    _generate_monthly_okr_report,
    _generate_okr_report,
    _get_my_okr,
    _get_okr,
    _get_okr_settings_tool,
    _load_okr_request_context,
    _okr_permission_denied,
    _update_kr_content,
    _update_kr_progress,
)
from app.services.agent_tools_registry_runtime import (
    _ALWAYS_INCLUDE_CORE,
    _CHANNEL_MESSAGE_TOOL_NAMES,
    _FEISHU_TOOL_NAMES,
    _FIXED_MEDIA_TOOL_NAMES,
    _LEGACY_MEDIA_TOOL_NAMES,
    _patch_computer_tool_descriptions,
    _strip_a2a_msg_type,
    get_agent_tools_for_llm as _get_agent_tools_for_llm_impl,
    _stabilize_media_tool_definitions as _stabilize_media_tool_definitions_impl,
)
from app.services.agent_tools_sql_support import (
    DEFAULT_SQL_MAX_BYTES,
    DEFAULT_SQL_MAX_ROWS,
    HARD_SQL_MAX_BYTES,
    HARD_SQL_MAX_ROWS,
    SQL_DISPLAY_CHAR_BUDGET,
    SQL_FETCH_BATCH,
    _bounded_collect,
    _clamp_sql_max_rows,
    _format_sql_result,
    _resolve_sql_max_bytes,
    _sql_execute,
    _sql_execute_mysql,
    _sql_execute_postgres,
    _sql_execute_sqlite,
)
from app.services.agent_tools_web_support import (
    _extract_page_links,
    _fallback_extract_visible_text,
    _get_jina_api_key,
    _jina_read,
    _jina_search,
    _read_webpage,
    _search_bing,
    _search_duckduckgo,
    _search_exa,
    _search_google,
    _search_tavily,
    _validate_public_http_url,
)
from app.services.agent_tools_task_contact_ops import (
    _add_contact_tool,
    _format_contact_search_results,
    _get_dingtalk_channel_provisioning_status_tool,
    _manage_tasks,
    _parse_uuid,
    _remove_contact_tool,
    _search_contacts_tool,
    _start_dingtalk_channel_provisioning_tool,
)
from app.services.agent_tools_trigger_ops import (
    MAX_TRIGGERS_PER_AGENT,
    VALID_TRIGGER_TYPES,
    _handle_cancel_trigger,
    _handle_list_triggers,
    _handle_set_trigger,
    _handle_update_trigger,
)
from app.services.agent_tools_skill_market_ops import (
    _install_skill,
    _install_skill_from_market,
    _market_install_actor,
    _market_tool_actor,
    _publish_skill_to_market,
    _search_clawhub,
    _search_skill_market,
    _withdraw_skill_from_market,
)


_settings = get_settings()
WORKSPACE_ROOT = Path(_settings.STORAGE_LOCAL_ROOT or _settings.AGENT_DATA_DIR)
MEDIA_DELIVERY_MAX_IN_FLIGHT = 4
_outbound_media_slots = _agent_tools_outbound_core_module._outbound_media_slots
_OUTBOUND_CORE_EXPORT_NAMES = (
    "_build_outbound_operation_key",
    "_lock_outbound_operation",
    "_outbound_operation_lock_id",
    "_outbound_operation_lifecycle_lock",
    "_locked_outbound_media_connection",
    "_outbound_media_db_session",
    "_find_outbound_tool_receipt",
    "_persist_outbound_channel_message",
    "_duplicate_outbound_claim_result",
    "_session_message_result",
    "_session_receipt_replay_status",
)
_OUTBOUND_CORE_SYNC_NAMES = (
    "async_session",
    "engine",
    "_outbound_media_connection",
    "_outbound_media_slots",
    *_OUTBOUND_CORE_EXPORT_NAMES,
)
_MEDIA_DELIVERY_SUPPORT_SYNC_NAMES = (
    "_outbound_media_connection",
    "_outbound_media_slots",
    "_lock_outbound_operation",
    "_outbound_operation_lock_id",
    "_outbound_operation_lifecycle_lock",
    "_locked_outbound_media_connection",
    "_outbound_media_db_session",
)
_IMAGE_OPS_EXPORT_NAMES = (
    "_upload_image",
    "_generate_image",
    "_generate_image_siliconflow",
    "_generate_image_openai",
    "_json_path_get",
    "_render_json_template",
    "_json_structure_preview",
    "_find_first_image_reference",
    "_custom_image_reference_to_bytes",
    "_generate_image_custom_api",
    "_generate_image_google",
)
_MCP_RUNTIME_EXPORT_NAMES = (
    "_execute_mcp_tool",
    "_execute_via_smithery_connect",
    "_smithery_auto_recover",
)
_MCP_RUNTIME_SYNC_NAMES = (
    "async_session",
    "get_settings",
    "SandboxMcpHost",
    "SandboxMcpHubClient",
    "current_agent_runtime_workspace",
    "_decrypt_sensitive_fields",
    "_agent_workspace_root",
    *_MCP_RUNTIME_EXPORT_NAMES,
)
_MCP_IMPORT_EXPORT_NAMES = (
    "_discover_resources",
    "_import_mcp_server",
)
_MEDIA_CORE_EXPORT_NAMES = (
    "_PLATFORM_SESSION_CHANNELS",
    "_preflight_managed_media_target",
    "_has_configured_dingtalk_media_channel",
    "_MEDIA_DELIVERY_MESSAGES",
    "_describe_media_delivery_result",
    "_sniff_media_file_kind",
    "_sniff_media_file_mime",
    "_sniff_image_file",
    "_external_media_filename",
)
_MEDIA_CORE_SYNC_NAMES = (
    "async_session",
    "select",
    "RecipientResolutionError",
    "resolve_human_channel_recipient",
    *_MEDIA_CORE_EXPORT_NAMES,
)
_MESSAGE_TRANSPORT_EXPORT_NAMES = (
    "_send_feishu_message",
    "_send_dingtalk_message",
    "_send_wecom_message",
    "_send_slack_message",
)
_MESSAGE_TRANSPORT_SYNC_NAMES = (
    "async_session",
    "logger",
    "select",
    "_persist_outbound_channel_message",
    "_duplicate_outbound_claim_result",
    "append_delivery_part",
    "register_delivery",
    "find_or_create_channel_session",
    "get_platform_user_by_org_member",
    "resolve_human_channel_recipient",
    "sanitize_user_visible_text",
    *_MESSAGE_TRANSPORT_EXPORT_NAMES,
)
_PLATFORM_TRANSPORT_EXPORT_NAMES = (
    "_send_teams_channel_message",
    "_send_wechat_channel_message",
    "_send_platform_message",
)
_PLATFORM_TRANSPORT_SYNC_NAMES = (
    "async_session",
    "logger",
    "select",
    "_persist_outbound_channel_message",
    "_duplicate_outbound_claim_result",
    "append_delivery_part",
    "register_delivery",
    "find_or_create_channel_session",
    "get_platform_user_by_org_member",
    "resolve_platform_user_recipient",
    "sanitize_user_visible_text",
    *_PLATFORM_TRANSPORT_EXPORT_NAMES,
)
_SESSION_MESSAGES_EXPORT_NAMES = (
    "_send_exact_session_message",
    "_send_session_message",
    "_send_group_session_message",
    "_send_channel_message",
)
_SESSION_MESSAGES_SYNC_NAMES = (
    "async_session",
    "logger",
    "select",
    "_GROUP_SESSION_DENIAL",
    "_LEGACY_GROUP_SESSION_CHANNELS",
    "_PLATFORM_SESSION_CHANNELS",
    "_SESSION_MESSAGE_CAPABILITIES",
    "_SESSION_MESSAGE_DENIAL",
    "_build_outbound_operation_key",
    "_lock_outbound_operation",
    "_persist_outbound_channel_message",
    "_session_message_result",
    "_session_receipt_replay_status",
    "_send_dingtalk_message",
    "_send_feishu_message",
    "_send_slack_message",
    "_send_platform_message",
    "_send_teams_channel_message",
    "_send_wechat_channel_message",
    "_send_wecom_message",
    "append_delivery_part",
    "register_delivery",
    "prepare_group_user_mentions",
    "deliver_message_with_receipt",
    "sanitize_user_visible_text",
    *_SESSION_MESSAGES_EXPORT_NAMES,
)
_SANDBOX_WEB_EXPORT_NAMES = (
    "_check_code_safety",
    "build_cli_injection",
    "_resolve_sandbox_backend",
    "_browse",
    "_web_open",
    "_web_eval",
    "_web_cdp",
    "_web_screenshot",
)
_SANDBOX_WEB_SYNC_NAMES = (
    "async_session",
    "AgentModel",
    "_get_tool_config",
    "agent_tool_enabled",
    *_SANDBOX_WEB_EXPORT_NAMES,
)
_CODE_RUNTIME_EXPORT_NAMES = (
    "_execute_code",
    "_is_cli_tool_name",
    "_execute_cli_tool",
    "_execute_code_legacy",
)
_CODE_RUNTIME_SYNC_NAMES = (
    "logger",
    "_get_tool_config",
    "_agent_workspace_root",
    "_canonicalize_execute_code_upload_paths",
    "_check_code_safety",
    "build_cli_injection",
    *_CODE_RUNTIME_EXPORT_NAMES,
)
_WEB_OPS_EXPORT_NAMES = (
    "_web_search",
    "_exa_search",
    "_duckduckgo_search_tool",
    "_tavily_search_tool",
    "_google_search_tool",
    "_bing_search_tool",
)
_WEB_OPS_SYNC_NAMES = (
    "get_settings",
    "_get_tool_config",
    "_search_bing",
    "_search_duckduckgo",
    "_search_exa",
    "_search_google",
    "_search_tavily",
    *_WEB_OPS_EXPORT_NAMES,
)
_PLAZA_OPS_EXPORT_NAMES = (
    "_plaza_get_new_posts",
    "_plaza_create_post",
    "_plaza_add_comment",
)
_PLAZA_OPS_SYNC_NAMES = (
    "async_session",
    "select",
    *_PLAZA_OPS_EXPORT_NAMES,
)
_FEISHU_AUTH_EXPORT_NAMES = (
    "_get_feishu_token",
    "_get_agent_calendar_id",
    "_feishu_resolve_open_id",
    "_iso_to_ts",
    "_get_feishu_credentials",
    "_get_feishu_tenant_doc_url",
    "_get_feishu_bitable_url",
    "_parse_feishu_url",
    "_check_feishu_err",
)
_FEISHU_AUTH_SYNC_NAMES = (
    "async_session",
    "select",
    *_FEISHU_AUTH_EXPORT_NAMES,
)
_FILE_DELIVERY_EXPORT_NAMES = (
    "_send_file_to_session",
    "_send_file_to_recipient",
    "_send_file_via_feishu",
    "_send_file_via_slack",
    "_send_file_via_slack_channel",
)
_FILE_DELIVERY_SYNC_NAMES = (
    "async_session",
    "logger",
    "select",
    "_agent_workspace_root",
    "record_channel_file_part",
    "resolve_human_channel_recipient",
    "sanitize_user_visible_text",
    *_FILE_DELIVERY_EXPORT_NAMES,
)
_CHANNEL_FILE_EXPORT_NAMES = ("_send_channel_file",)
_CHANNEL_FILE_SYNC_NAMES = (
    "_agent_workspace_root",
    "_claim_channel_file_receipt",
    "_normalize_tool_workspace_rel_path",
    "_platform_file_delivery_result",
    "_supports_exact_file_session_route",
    "_send_file_to_session",
    "_send_file_to_recipient",
    "_sniff_media_file_kind",
    "append_delivery_part",
    "register_delivery",
    "channel_file_sender",
    "channel_file_part_recorder",
    "sanitize_user_visible_text",
    *_CHANNEL_FILE_EXPORT_NAMES,
)
_MEDIA_REPLAY_EXPORT_NAMES = (
    "_replay_terminal_media_delivery",
    "_replay_terminal_media_delivery_under_slot",
    "_publish_external_media_to_session",
)
_MEDIA_REPLAY_SYNC_NAMES = (
    "async_session",
    "_outbound_media_slots",
    "_lock_outbound_operation",
    "_build_outbound_operation_key",
    "_PLATFORM_SESSION_CHANNELS",
    "_describe_media_delivery_result",
    "_external_media_filename",
    *_MEDIA_REPLAY_EXPORT_NAMES,
)
_MEDIA_RUNTIME_EXPORT_NAMES = (
    "_send_media_to_session",
    "_send_media_to_session_under_lifecycle_lock",
)
_MEDIA_RUNTIME_SYNC_NAMES = (
    "logger",
    "select",
    "ChatMessage",
    "ChannelConfig",
    "ChatSession",
    "TurnRuntime",
    "deliver_message_with_receipt",
    "attachment_from_workspace_path",
    "canonical_media_mime",
    "normalize_media_display_title",
    "DeliveryReceiptPersistenceError",
    "IMDeliveryPart",
    "IMDeliveryResult",
    "_merge_delivery_into_meta",
    "_build_outbound_operation_key",
    "_outbound_operation_lifecycle_lock",
    "_outbound_media_db_session",
    "_PLATFORM_SESSION_CHANNELS",
    "_describe_media_delivery_result",
    "_sniff_media_file_mime",
    *_MEDIA_RUNTIME_EXPORT_NAMES,
)
_MEDIA_RECIPIENT_EXPORT_NAMES = ("_send_media_to_recipient",)
_MEDIA_RECIPIENT_SYNC_NAMES = (
    "async_session",
    "find_or_create_channel_session",
    "_send_media_to_session",
    *_MEDIA_RECIPIENT_EXPORT_NAMES,
)
_MEDIA_ENTRY_EXPORT_NAMES = (
    "MEDIA_TOOL_MAX_FILE_BYTES",
    "_send_channel_media",
)
_MEDIA_ENTRY_SYNC_NAMES = (
    "logger",
    "MEDIA_TOOL_MAX_FILE_BYTES",
    "MediaUrlError",
    "current_agent_runtime_workspace",
    "get_storage_backend",
    "import_managed_media_url",
    "normalize_managed_media_headers",
    "normalize_media_display_title",
    "sanitize_user_visible_text",
    "validate_media_url",
    "_agent_workspace_root",
    "_build_outbound_operation_key",
    "_describe_media_delivery_result",
    "_get_tool_config",
    "_normalize_tool_workspace_rel_path",
    "_preflight_managed_media_target",
    "_publish_external_media_to_session",
    "_replay_terminal_media_delivery",
    "_send_media_to_recipient",
    "_send_media_to_session",
    "_sniff_image_file",
    "_sniff_media_file_mime",
    *_MEDIA_ENTRY_EXPORT_NAMES,
)
_TRIGGER_OPS_SYNC_NAMES = (
    "async_session",
    "logger",
    "select",
    "ensure_focus_item",
    "RecipientResolutionError",
    "resolve_agent_recipient",
    "resolve_platform_user_recipient",
    "_handle_set_trigger",
    "_handle_update_trigger",
    "_handle_cancel_trigger",
    "_handle_list_triggers",
)
_AGENTBAY_BROWSER_EXPORT_NAMES = (
    "_agentbay_normalize_image_bytes",
    "_agentbay_save_image_to_workspace",
    "_agentbay_browser_navigate",
    "_agentbay_browser_screenshot",
    "_agentbay_browser_save_screenshot",
    "_agentbay_browser_click",
    "_agentbay_browser_type",
    "_agentbay_browser_extract",
    "_agentbay_browser_observe",
    "_agentbay_browser_login",
    "_agentbay_command_exec",
)
_AGENTBAY_BROWSER_SYNC_NAMES = (
    "logger",
    *_AGENTBAY_BROWSER_EXPORT_NAMES,
)
_AGENTBAY_APPS_EXPORT_NAMES = (
    "_agentbay_normalize_text",
    "_agentbay_app_field",
    "_agentbay_format_apps",
    "_agentbay_find_installed_app_match",
    "_agentbay_uncertain_start_error",
    "_agentbay_visible_apps_note",
    "_agentbay_computer_start_app",
    "_agentbay_computer_get_installed_apps",
    "_agentbay_computer_list_visible_apps",
)
_AGENTBAY_APPS_SYNC_NAMES = (
    "logger",
    *_AGENTBAY_APPS_EXPORT_NAMES,
)
_AGENTBAY_SCREEN_EXPORT_NAMES = (
    "_agentbay_extract_screen_dimensions",
    "_agentbay_get_screen_metadata",
    "_agentbay_image_dimensions",
    "_agentbay_crop_image_bytes",
    "_agentbay_expand_precision_crop",
    "_agentbay_desktop_coordinate_note",
    "_agentbay_computer_screenshot",
    "_agentbay_computer_save_screenshot",
    "_agentbay_computer_precision_screenshot",
    "_agentbay_computer_get_screen_size",
)
_AGENTBAY_SCREEN_SYNC_NAMES = (
    "logger",
    "_agentbay_normalize_image_bytes",
    "_agentbay_save_image_to_workspace",
    *_AGENTBAY_SCREEN_EXPORT_NAMES,
)
_AGENTBAY_WINDOW_EXPORT_NAMES = (
    "_agentbay_computer_click",
    "_agentbay_computer_input_text",
    "_agentbay_computer_press_keys",
    "_agentbay_computer_scroll",
    "_agentbay_computer_move_mouse",
    "_agentbay_computer_drag_mouse",
    "_agentbay_computer_get_cursor_position",
    "_agentbay_computer_get_active_window",
    "_agentbay_computer_activate_window",
    "_agentbay_computer_list_windows",
    "_agentbay_computer_close_window",
    "_agentbay_computer_dismiss_dialog",
)
_AGENTBAY_WINDOW_SYNC_NAMES = (
    "logger",
    "_agentbay_normalize_text",
    "_agentbay_get_screen_metadata",
    *_AGENTBAY_WINDOW_EXPORT_NAMES,
)
_AGENTBAY_CODE_EXPORT_NAMES = (
    "_agentbay_code_execute",
    "_agentbay_code_write_file",
    "_agentbay_code_read_file",
    "_agentbay_code_edit_file",
)
_AGENTBAY_CODE_SYNC_NAMES = (
    "logger",
    *_AGENTBAY_CODE_EXPORT_NAMES,
)
_AGENTBAY_TRANSFER_EXPORT_NAMES = ("_agentbay_file_transfer",)
_AGENTBAY_TRANSFER_SYNC_NAMES = (
    "logger",
    *_AGENTBAY_TRANSFER_EXPORT_NAMES,
)


async def _deploy_ops_get_tool_config_proxy(*args, **kwargs):
    return await _get_tool_config(*args, **kwargs)


async def _deploy_ops_get_vercel_token_proxy(*args, **kwargs):
    return await _get_vercel_token(*args, **kwargs)


async def _deploy_ops_get_vercel_quota_summary_proxy(*args, **kwargs):
    return await _get_vercel_quota_summary(*args, **kwargs)


async def _deploy_ops_check_neon_quota_limit_proxy(*args, **kwargs):
    return await _check_neon_quota_limit(*args, **kwargs)


_agent_tools_deploy_ops_module._get_tool_config = _deploy_ops_get_tool_config_proxy
_agent_tools_deploy_ops_module._get_vercel_token = _deploy_ops_get_vercel_token_proxy
_agent_tools_deploy_ops_module._get_vercel_quota_summary = _deploy_ops_get_vercel_quota_summary_proxy
_agent_tools_deploy_ops_module._check_neon_quota_limit = _deploy_ops_check_neon_quota_limit_proxy
_agent_tools_deploy_ops_module._agent_workspace_root = _agent_workspace_root


def _sync_document_tool_bindings() -> None:
    _agent_tools_document_tools_module.get_storage_backend = get_storage_backend
    _agent_tools_document_tools_module._prepare_temp_workspace = _prepare_temp_workspace
    _agent_tools_document_tools_module._read_document = globals()["_read_document"]
    _agent_tools_document_tools_module._READ_DOCUMENT_MAX_FILE_BYTES = _READ_DOCUMENT_MAX_FILE_BYTES
    _agent_tools_document_tools_module._READ_DOCUMENT_TIMEOUT_SECONDS = _READ_DOCUMENT_TIMEOUT_SECONDS
    _agent_tools_document_tools_module._READ_DOCUMENT_FALLBACK_TIMEOUT_SECONDS = _READ_DOCUMENT_FALLBACK_TIMEOUT_SECONDS
    _agent_tools_document_tools_module._READ_DOCUMENT_MAX_COLUMNS = _READ_DOCUMENT_MAX_COLUMNS
    _agent_tools_document_tools_module._READ_DOCUMENT_MAX_ROWS = _READ_DOCUMENT_MAX_ROWS
    _agent_tools_document_tools_module._READ_DOCUMENT_MAX_SHEETS = _READ_DOCUMENT_MAX_SHEETS
    _agent_tools_document_tools_module._READ_DOCUMENT_MAX_PAGES = _READ_DOCUMENT_MAX_PAGES
    _agent_tools_document_tools_module._READ_DOCUMENT_MAX_SLIDES = _READ_DOCUMENT_MAX_SLIDES
    _agent_tools_document_tools_module._READ_DOCUMENT_HARD_CHAR_CEILING = _READ_DOCUMENT_HARD_CHAR_CEILING


export_module_symbols(
    __name__,
    (_agent_tools_outbound_core_module,),
    _OUTBOUND_CORE_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _OUTBOUND_CORE_EXPORT_NAMES)
export_module_symbols(
    __name__,
    (_agent_tools_image_ops_module,),
    _IMAGE_OPS_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _IMAGE_OPS_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_image_ops_module,),
    _IMAGE_OPS_EXPORT_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_mcp_runtime_module,),
    _MCP_RUNTIME_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _MCP_RUNTIME_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_mcp_runtime_module,),
    _MCP_RUNTIME_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_mcp_import_module,),
    _MCP_IMPORT_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _MCP_IMPORT_EXPORT_NAMES)
export_module_symbols(
    __name__,
    (_agent_tools_media_core_module,),
    _MEDIA_CORE_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _MEDIA_CORE_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_media_core_module,),
    _MEDIA_CORE_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_message_transports_module,),
    _MESSAGE_TRANSPORT_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _MESSAGE_TRANSPORT_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_message_transports_module,),
    _MESSAGE_TRANSPORT_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_platform_transports_module,),
    _PLATFORM_TRANSPORT_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _PLATFORM_TRANSPORT_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_platform_transports_module,),
    _PLATFORM_TRANSPORT_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_session_messages_module,),
    _SESSION_MESSAGES_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _SESSION_MESSAGES_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_session_messages_module,),
    _SESSION_MESSAGES_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_sandbox_web_ops_module,),
    _SANDBOX_WEB_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _SANDBOX_WEB_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_sandbox_web_ops_module,),
    _SANDBOX_WEB_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_code_runtime_module,),
    _CODE_RUNTIME_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _CODE_RUNTIME_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_code_runtime_module,),
    _CODE_RUNTIME_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_web_ops_module,),
    _WEB_OPS_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _WEB_OPS_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_web_ops_module,),
    _WEB_OPS_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_plaza_ops_module,),
    _PLAZA_OPS_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _PLAZA_OPS_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_plaza_ops_module,),
    _PLAZA_OPS_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_feishu_auth_module,),
    _FEISHU_AUTH_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _FEISHU_AUTH_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_feishu_auth_module,),
    _FEISHU_AUTH_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_file_delivery_module,),
    _FILE_DELIVERY_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _FILE_DELIVERY_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_file_delivery_module,),
    _FILE_DELIVERY_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_channel_file_module,),
    _CHANNEL_FILE_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _CHANNEL_FILE_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_channel_file_module,),
    _CHANNEL_FILE_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_media_replay_module,),
    _MEDIA_REPLAY_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _MEDIA_REPLAY_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_media_replay_module,),
    _MEDIA_REPLAY_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_media_runtime_module,),
    _MEDIA_RUNTIME_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _MEDIA_RUNTIME_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_media_runtime_module,),
    _MEDIA_RUNTIME_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_media_recipient_module,),
    _MEDIA_RECIPIENT_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _MEDIA_RECIPIENT_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_media_recipient_module,),
    _MEDIA_RECIPIENT_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_media_entry_module,),
    _MEDIA_ENTRY_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _MEDIA_ENTRY_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_media_entry_module,),
    _MEDIA_ENTRY_SYNC_NAMES,
)
register_sync_targets(
    __name__,
    (_agent_tools_trigger_ops_module,),
    _TRIGGER_OPS_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_agentbay_browser_module,),
    _AGENTBAY_BROWSER_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _AGENTBAY_BROWSER_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_agentbay_browser_module,),
    _AGENTBAY_BROWSER_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_agentbay_apps_module,),
    _AGENTBAY_APPS_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _AGENTBAY_APPS_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_agentbay_apps_module,),
    _AGENTBAY_APPS_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_agentbay_screen_module,),
    _AGENTBAY_SCREEN_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _AGENTBAY_SCREEN_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_agentbay_screen_module,),
    _AGENTBAY_SCREEN_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_agentbay_window_module,),
    _AGENTBAY_WINDOW_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _AGENTBAY_WINDOW_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_agentbay_window_module,),
    _AGENTBAY_WINDOW_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_agentbay_code_module,),
    _AGENTBAY_CODE_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _AGENTBAY_CODE_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_agentbay_code_module,),
    _AGENTBAY_CODE_SYNC_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_agentbay_transfer_module,),
    _AGENTBAY_TRANSFER_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _AGENTBAY_TRANSFER_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_agentbay_transfer_module,),
    _AGENTBAY_TRANSFER_SYNC_NAMES,
)


def _read_document_sync(
    ws: Path, rel_path: str, max_chars: int = _READ_DOCUMENT_HARD_CHAR_CEILING, tenant_id: str | None = None
) -> str:
    _sync_document_tool_bindings()
    return _read_document_sync_impl(ws, rel_path, max_chars=max_chars, tenant_id=tenant_id)


async def _read_document(
    ws: Path, rel_path: str, max_chars: int = _READ_DOCUMENT_HARD_CHAR_CEILING, tenant_id: str | None = None
) -> str:
    _sync_document_tool_bindings()
    return await _read_document_impl(ws, rel_path, max_chars=max_chars, tenant_id=tenant_id)


async def _read_document_with_timeout(
    ws: Path, rel_path: str, max_chars: int = _READ_DOCUMENT_HARD_CHAR_CEILING, tenant_id: str | None = None
) -> str:
    _sync_document_tool_bindings()
    return await _read_document_with_timeout_impl(ws, rel_path, max_chars=max_chars, tenant_id=tenant_id)


async def _read_document_from_storage(
    agent_id: uuid.UUID,
    rel_path: str,
    max_chars: int = 8000,
    tenant_id: str | None = None,
) -> str:
    _sync_document_tool_bindings()
    return await _read_document_from_storage_impl(
        agent_id,
        rel_path,
        max_chars=max_chars,
        tenant_id=tenant_id,
    )


TEMP_WORKSPACE_DEFAULT_PATHS = ["workspace", "memory", "skills", "focus.md", "soul.md", "HEARTBEAT.md"]
MAX_EXEC_STDOUT_CAPTURE_BYTES = 1_000_000
MAX_EXEC_STDERR_CAPTURE_BYTES = 500_000

# Cap web_eval/web_cdp serialized results surfaced to the LLM.
_STDOUT_RPA_LIMIT = 20000

# Delivery guidance appended to every A2A consult turn (not persisted to history
# so it does not pollute the stored context).
A2A_DELIVERY_GUIDANCE = (
    "你正在回复另一位数字员工同事,请简洁、切题地作答。\n"
    "如果你写了任何文件(报告/文档/分析)需要交付给对方,必须调用 "
    'send_file_to_agent(agent_id="<Relationships 中对方的 agent_id>", file_path="<路径>") 投递 —— '
    "对方无法访问你的工作区,绝不能只告诉路径。"
)

# ContextVar set by each channel handler so send_channel_file knows where to send
# Value: async callable(file_path: Path) -> None  |  None for web chat (returns URL)
channel_file_sender: ContextVar = _agent_tools_file_receipts_module.channel_file_sender
channel_file_part_recorder: ContextVar = _agent_tools_file_receipts_module.channel_file_part_recorder
channel_audio_sender: ContextVar = ContextVar('channel_audio_sender', default=None)
channel_video_sender: ContextVar = ContextVar('channel_video_sender', default=None)
# For web chat: agent_id needed to build download URL
channel_web_agent_id: ContextVar = ContextVar("channel_web_agent_id", default=None)
# Set by Feishu channel handler — open_id of the message sender so calendar tool
# can auto-invite them as attendee when no explicit attendee list is given
channel_feishu_sender_open_id: ContextVar = ContextVar("channel_feishu_sender_open_id", default=None)
_FILE_RECEIPTS_EXPORT_NAMES = (
    "_claim_channel_file_receipt",
    "record_channel_file_part",
    "_supports_exact_file_session_route",
    "_normalize_tool_workspace_rel_path",
    "_platform_file_delivery_result",
)
_FILE_RECEIPTS_SYNC_NAMES = (
    "async_session",
    "_build_outbound_operation_key",
    "channel_file_sender",
    "channel_file_part_recorder",
    "append_delivery_part",
    "register_delivery",
    *_FILE_RECEIPTS_EXPORT_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_file_receipts_module,),
    _FILE_RECEIPTS_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _FILE_RECEIPTS_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_file_receipts_module,),
    _FILE_RECEIPTS_SYNC_NAMES,
)
from app.services import agent_tools_feishu_docs as _agent_tools_feishu_docs_module

_FEISHU_DOCS_EXPORT_NAMES = (
    "_resolve_docx_document_token",
    "_feishu_read_doc",
    "_feishu_create_doc",
    "_feishu_append_doc",
    "_feishu_wiki_get_node",
    "_feishu_doc_search",
    "_feishu_wiki_list",
    "_feishu_doc_read",
    "_feishu_doc_create",
    "_parse_inline_markdown",
    "_markdown_to_feishu_blocks",
    "_feishu_doc_append",
)
_FEISHU_DOCS_SYNC_NAMES = (
    "channel_feishu_sender_open_id",
    "_check_feishu_err",
    "_get_feishu_credentials",
    "_get_feishu_tenant_doc_url",
    "_parse_feishu_url",
    *_FEISHU_DOCS_EXPORT_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_feishu_docs_module,),
    _FEISHU_DOCS_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _FEISHU_DOCS_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_feishu_docs_module,),
    _FEISHU_DOCS_SYNC_NAMES,
)
from app.services import agent_tools_feishu_bitable as _agent_tools_feishu_bitable_module

_FEISHU_BITABLE_EXPORT_NAMES = (
    "_resolve_bitable_app_token",
    "_bitable_list_tables",
    "_bitable_create_app",
    "_bitable_list_fields",
    "_bitable_query_records",
    "_bitable_create_record",
    "_bitable_update_record",
    "_bitable_delete_record",
)
_FEISHU_BITABLE_SYNC_NAMES = (
    "_check_feishu_err",
    "_get_feishu_bitable_url",
    "_get_feishu_credentials",
    "_parse_feishu_url",
    "_feishu_wiki_get_node",
    *_FEISHU_BITABLE_EXPORT_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_feishu_bitable_module,),
    _FEISHU_BITABLE_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _FEISHU_BITABLE_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_feishu_bitable_module,),
    _FEISHU_BITABLE_SYNC_NAMES,
)
from app.services import agent_tools_feishu_collab_ops as _agent_tools_feishu_collab_module

_FEISHU_COLLAB_EXPORT_NAMES = (
    "_feishu_drive_share",
    "_feishu_drive_delete",
    "_resolve_feishu_open_id",
    "_feishu_calendar_list",
    "_feishu_calendar_create",
    "_feishu_calendar_update",
    "_feishu_calendar_delete",
    "_feishu_approval_create",
    "_feishu_approval_query",
    "_feishu_approval_get",
    "_feishu_user_search",
)
_FEISHU_COLLAB_SYNC_NAMES = (
    "async_session",
    "select",
    "selectinload",
    "AgentModel",
    "AgentRelationship",
    "OrgMember",
    "UserModel",
    "channel_feishu_sender_open_id",
    "_check_feishu_err",
    "_get_agent_calendar_id",
    "_get_feishu_credentials",
    "_iso_to_ts",
    "_feishu_wiki_get_node",
    "RecipientResolutionError",
    "resolve_human_channel_recipient",
    *_FEISHU_COLLAB_EXPORT_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_feishu_collab_module,),
    _FEISHU_COLLAB_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _FEISHU_COLLAB_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_feishu_collab_module,),
    _FEISHU_COLLAB_SYNC_NAMES,
)
_outbound_media_connection: ContextVar = _agent_tools_outbound_core_module._outbound_media_connection
register_sync_targets(
    __name__,
    (_agent_tools_outbound_core_module,),
    _OUTBOUND_CORE_SYNC_NAMES,
)
register_sync_targets(
    __name__,
    (_agent_tools_media_delivery_support_module,),
    _MEDIA_DELIVERY_SUPPORT_SYNC_NAMES,
)

# ─── Tool Definitions (OpenAI function-calling format) ──────────

# Database rows are the runtime source of truth. Keep the static schema registry
# aligned for code paths that build named subsets from it.
AGENT_TOOLS.extend(USER_PROJECT_TOOL_SCHEMAS)

def _stabilize_media_tool_definitions(tools: list[dict]) -> list[dict]:
    return _stabilize_media_tool_definitions_impl(tools, AGENT_TOOLS)


async def get_agent_tools_for_llm(
    agent_id: uuid.UUID,
    *,
    assignment_snapshot: list[dict] | None = None,
) -> list[dict]:
    return await _get_agent_tools_for_llm_impl(
        agent_id,
        agent_tools=AGENT_TOOLS,
        get_tool_config=_get_tool_config,
        assignment_snapshot=assignment_snapshot,
    )


# ─── Tool Executors ─────────────────────────────────────────────

# Subset of _CODE_EXEC_TOOL_NAMES that must NOT silently fall back to local subprocess
# when their config or runtime is broken — the user made an explicit choice.
_REMOTE_SANDBOX_TOOL_NAMES: frozenset[str] = frozenset({"execute_code_e2b", "execute_code_aio"})

# Mapping from tool_name to autonomy action_type used for policy lookup and notifications.
# Each tool name maps to the action_type key in the agent's autonomy_policy dict.
# Using the tool's own name avoids misleading notification titles (e.g. showing
# "send_feishu_message" when the agent actually called send_message_to_agent).
_TOOL_AUTONOMY_MAP = {
    "write_file": "write_workspace_files",
    "move_file": "write_workspace_files",
    "delete_file": "delete_files",
    "add_contact": "manage_relationships",
    "remove_contact": "manage_relationships",
    "send_feishu_message": "send_feishu_message",
    "send_message_to_agent": "send_message_to_agent",  # A2A messaging — distinct from feishu
    "send_file_to_agent": "send_file_to_agent",  # A2A file transfer
    "web_search": "web_search",
    "execute_code": "execute_code",
    "sql_execute": "sql_execute",
    "execute_code_e2b": "execute_code",
    "execute_code_aio": "execute_code",
    "install_skill_from_market": "install_skill_from_market",
    "publish_skill_to_market": "publish_skill_to_market",
    "withdraw_skill_from_market": "withdraw_skill_from_market",
}

_FORCED_AUTONOMY_LEVELS = {
    # Installing an already-published, visible Skill is a reversible Agent-local
    # action. The executor still requires a confirmed human with manage access.
    "install_skill_from_market": "L1",
    "publish_skill_to_market": "L3",
    "withdraw_skill_from_market": "L3",
}

async def execute_tool(
    tool_name: str,
    arguments: dict,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: str = "",
    tool_call_id: str = "",
    turn_anchor_id: uuid.UUID | None = None,
    on_output=None,
    skip_autonomy: bool = False,
    tools_for_llm: list[dict] | None = None,
    approved_by_human: bool = False,
) -> str:
    from app.services.agent_tools_execute_tool_dispatch_basic import execute_tool_dispatch_basic
    from app.services.agent_tools_execute_tool_dispatch_extended import execute_tool_dispatch_extended
    from app.services.agent_tools_execute_tool_postprocess import execute_tool_postprocess
    from app.services.agent_tools_execute_tool_preflight import execute_tool_preflight

    preflight = await execute_tool_preflight(
        tool_name,
        arguments,
        agent_id,
        user_id,
        session_id=session_id,
        tool_call_id=tool_call_id,
        turn_anchor_id=turn_anchor_id,
        on_output=on_output,
        skip_autonomy=skip_autonomy,
        tools_for_llm=tools_for_llm,
        approved_by_human=approved_by_human,
    )
    if isinstance(preflight, str):
        return preflight

    try:
        result = await execute_tool_dispatch_basic(preflight)
        if result is None:
            result = await execute_tool_dispatch_extended(preflight)
        return await execute_tool_postprocess(
            preflight.tool_name,
            preflight.arguments,
            preflight.agent_id,
            preflight.user_id,
            preflight.session_id,
            result,
        )
    except Exception as e:
        logger.exception(f"[Tool] Execution failed: {tool_name}")
        return f"Tool execution error ({tool_name}): {type(e).__name__}: {str(e)[:200]}"






# ── Standalone search engine tool wrappers ───────────────────────────────────
# Each function reads its own tool config (agent > company > defaults) and
# delegates to the existing private search implementations above.



































































_SESSION_MESSAGE_CAPABILITIES = {
    "web": frozenset({"person"}),
    "miniprogram": frozenset({"person"}),
    "wechat_miniprogram": frozenset({"person"}),
    "dingtalk": frozenset({"person", "group"}),
    "feishu": frozenset({"person", "group"}),
    "wecom": frozenset({"person", "group"}),
    "slack": frozenset({"person", "group"}),
    "teams": frozenset({"person", "group"}),
    "microsoft_teams": frozenset({"person", "group"}),
    "discord": frozenset({"person", "group"}),
    "whatsapp": frozenset({"person"}),
    "wechat": frozenset({"person"}),
}
_LEGACY_GROUP_SESSION_CHANNELS = frozenset({"dingtalk", "feishu", "wecom", "slack", "teams", "microsoft_teams"})
_SESSION_MESSAGE_DENIAL = "❌ 无法投递：该会话不存在，或不属于当前数字员工。"
_GROUP_SESSION_DENIAL = "❌ 无法投递：该群会话不存在，或不属于当前数字员工。"


































# Plaza Tools — Agent Square social feed
# ═══════════════════════════════════════════════════════

# Plaza Tools — Agent Square social feed
# ═══════════════════════════════════════════════════════








# ─── Code Execution ─────────────────────────────────────────────

# Dangerous patterns to block (for legacy fallback)
_DANGEROUS_BASH_ALWAYS = [
    "rm -rf /",
    "rm -rf ~",
    "sudo ",
    "mkfs",
    "dd if=",
    ":(){ :",
    "chmod 777 /",
    "chown ",
    "shutdown",
    "reboot",
]

_DANGEROUS_BASH_NETWORK = [
    "curl ",
    "wget ",
    "nc ",
    "ncat ",
    "ssh ",
    "scp ",
]

_DANGEROUS_PYTHON_IMPORTS_ALWAYS = [
    "shutil.rmtree",
    "os.system",
    "os.popen",
    "os.exec",
    "os.spawn",
]

_DANGEROUS_PYTHON_IMPORTS_NETWORK = [
    "socket",
    "http.client",
    "urllib.request",
    "requests",
    "ftplib",
    "smtplib",
    "telnetlib",
    "ctypes",
]

_DANGEROUS_NODE_ALWAYS = [
    "fs.rmSync",
    "fs.rmdirSync",
    "process.exit",
]

_DANGEROUS_NODE_NETWORK = [
    "require('http')",
    "require('https')",
    "require('net')",
]


























# ─── Resource Discovery Executors ───────────────────────────────








# ─── Feishu Helper ────────────────────────────────────────────────────────────


















# ─── Feishu Bitable Tools ──────────────────────────────────────────




















# ─── Feishu Document Tools ──────────────────────────────────────────










# ─── Feishu Wiki Tools ───────────────────────────────────────────────────────


















# ─── Feishu Drive Share (All File Types) ────────────────────────────────────────




# ─── Feishu Drive Delete ──────────────────────────────────────────────────────




# ─── Feishu Calendar Tools ────────────────────────────────────────────────────












# ─── Feishu Approval Tools ───────────────────────────────────────────────────








# ─── Feishu User Search ───────────────────────────────────────────────────────




# ─── AgentBay Tool Handlers ─────────────────────────────────────























from app.services import agent_tools_a2a_delivery as _agent_tools_a2a_delivery_module

_A2A_DELIVERY_EXPORT_NAMES = (
    "_send_file_to_agent",
    "_resolve_a2a_target",
    "_create_on_message_trigger",
    "_arm_a2a_delegate_callback",
    "_append_focus_item",
    "_wake_agent_async",
)
_A2A_DELIVERY_SYNC_NAMES = (
    "async_session",
    "logger",
    "_build_outbound_operation_key",
    "current_agent_runtime_workspace",
    "ensure_focus_item",
    "get_storage_backend",
    "project_agent_runtime_workspace",
    "standard_agent_runtime_workspace",
    *_A2A_DELIVERY_EXPORT_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_a2a_delivery_module,),
    _A2A_DELIVERY_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _A2A_DELIVERY_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_a2a_delivery_module,),
    _A2A_DELIVERY_SYNC_NAMES,
)

from app.services import agent_tools_a2a_messaging as _agent_tools_a2a_messaging_module

_A2A_MESSAGING_EXPORT_NAMES = ("_send_message_to_agent",)
_A2A_MESSAGING_SYNC_NAMES = (
    "async_session",
    "logger",
    "A2A_DELIVERY_GUIDANCE",
    "_arm_a2a_delegate_callback",
    "_build_outbound_operation_key",
    "_lock_outbound_operation",
    "_wake_agent_async",
    *_A2A_MESSAGING_EXPORT_NAMES,
)
export_module_symbols(
    __name__,
    (_agent_tools_a2a_messaging_module,),
    _A2A_MESSAGING_EXPORT_NAMES,
)
prepare_exported_callables(__name__, _A2A_MESSAGING_EXPORT_NAMES)
register_sync_targets(
    __name__,
    (_agent_tools_a2a_messaging_module,),
    _A2A_MESSAGING_SYNC_NAMES,
)


from app.services import agent_tools_temp_workspace as _agent_tools_temp_workspace_module
from app.services.agent_tools_temp_workspace import (
    TempWorkspace,
    TempWorkspaceManifestEntry,
    _collect_temp_workspace_files,
    _materialize_storage_entry,
    _materialize_storage_path_with_budget,
    _materialize_storage_workspace,
    _prepare_temp_workspace,
    _sync_tasks_to_file,
    flush_temp_workspace,
    initialize_agent_workspace,
)
from app.services.agent_tools_temp_workspace_exec import (
    _CODE_EXEC_TOOL_NAMES,
    _execute_tool_direct,
    _execute_workspace_mutation,
    _run_with_temp_workspace,
)


def _temp_workspace_get_storage_backend_proxy():
    return get_storage_backend()


def _temp_workspace_normalize_storage_key_proxy(*args, **kwargs):
    return normalize_storage_key(*args, **kwargs)


def _temp_workspace_tool_storage_key_proxy(*args, **kwargs):
    return _tool_storage_key(*args, **kwargs)


_agent_tools_temp_workspace_module.get_storage_backend = _temp_workspace_get_storage_backend_proxy
_agent_tools_temp_workspace_module.normalize_storage_key = _temp_workspace_normalize_storage_key_proxy
_agent_tools_temp_workspace_module._tool_storage_key = _temp_workspace_tool_storage_key_proxy
