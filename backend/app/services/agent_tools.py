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
from app.services import agent_tools_code_runtime as _agent_tools_code_runtime_module
from app.services import agent_tools_channel_file_receipts as _agent_tools_file_receipts_module
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
from app.services import agent_tools_media_delivery_support as _agent_tools_media_delivery_support_module
from app.services import agent_tools_outbound_core as _agent_tools_outbound_core_module
from app.services import agent_tools_plaza_ops as _agent_tools_plaza_ops_module
from app.services import agent_tools_platform_transports as _agent_tools_platform_transports_module
from app.services import agent_tools_sandbox_web_ops as _agent_tools_sandbox_web_ops_module
from app.services import agent_tools_trigger_ops as _agent_tools_trigger_ops_module
from app.services import agent_tools_web_ops as _agent_tools_web_ops_module
from app.services.agent_tools_facade import (
    export_module_symbols,
    prepare_exported_callables,
    register_sync_targets,
)
from app.services.agent_tools_file_support import _tool_storage_key

TOOL_MATERIALIZE_MAX_FILE_BYTES = 10 * 1024 * 1024
TOOL_MATERIALIZE_MAX_TOTAL_BYTES = 100 * 1024 * 1024
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
TOOL_MATERIALIZE_MAX_FILE_BYTES = 10 * 1024 * 1024
TOOL_MATERIALIZE_MAX_TOTAL_BYTES = 100 * 1024 * 1024
MEDIA_TOOL_MAX_FILE_BYTES = 100 * 1024 * 1024
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


def _media_materialization_size_error(
    media_size: int,
    cover_size: int | None = None,
) -> str | None:
    """Return the stable preflight error before selective materialization."""
    if media_size > MEDIA_TOOL_MAX_FILE_BYTES:
        return "MEDIA_TOO_LARGE"
    if cover_size is not None and cover_size > TOOL_MATERIALIZE_MAX_FILE_BYTES:
        return "VIDEO_COVER_TOO_LARGE"
    if media_size + (cover_size or 0) > TOOL_MATERIALIZE_MAX_TOTAL_BYTES:
        return "MEDIA_BUNDLE_TOO_LARGE"
    return None


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














async def _send_channel_file(
    agent_id: uuid.UUID,
    ws: Path,
    arguments: dict,
    *,
    tool_call_id: str | None = None,
    origin_session_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send a file to a person or back to the current channel.

    Priority:
    1. If session_id is provided, use that exact existing person/group route.
    2. If canonical user_id is provided, resolve one exact internal route.
    3. If the current Session has an exact external file route, use that route.
    4. If a legacy non-DingTalk channel sender is set, use it directly.
    5. Fall back to a structured web/H5 result when no explicit target is requested.
    """
    raw_rel_path = arguments.get("file_path", "")
    rel_path = raw_rel_path.strip() if isinstance(raw_rel_path, str) else ""
    accompany_msg = sanitize_user_visible_text(str(arguments.get("message") or ""))
    canonical_user_id = str(arguments.get("user_id") or "").strip()
    requested_session_id = str(arguments.get("session_id") or "").strip()
    channel = str(arguments.get("channel") or "").strip().lower() or None
    if canonical_user_id and requested_session_id:
        return RecipientResolutionError(
            "ambiguous_file_target",
            "Cannot provide both session_id and user_id",
        ).as_json()
    if channel and not canonical_user_id:
        return RecipientResolutionError(
            "channel_requires_user_target",
            "channel can only be used with user_id",
        ).as_json()
    if not rel_path:
        return "Error: file_path is required"
    rel_path = _normalize_tool_workspace_rel_path(rel_path)
    if not rel_path:
        return "Error: Invalid file_path"

    # Resolve file path within agent workspace
    file_path = (ws / rel_path).resolve()
    ws_resolved = ws.resolve()
    try:
        file_path.relative_to(ws_resolved)
    except ValueError:
        file_path = (_agent_workspace_root(agent_id) / rel_path).resolve()
        if not file_path.exists():
            return f"Error: File not found: {rel_path}"
    if not file_path.exists():
        return f"Error: File not found: {rel_path}"

    detected_kind = _sniff_media_file_kind(file_path) or infer_attachment_kind(
        file_path.name, mimetypes.guess_type(file_path.name)[0]
    )
    if detected_kind in {"audio", "video"}:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "WRONG_MEDIA_TOOL",
                "media_kind": detected_kind,
                "message": f"Use send_media with media_type='{detected_kind}'.",
            },
            ensure_ascii=False,
        )

    async def _deliver_to_exact_session(target_session_id: str) -> str:
        receipt_id = await _claim_channel_file_receipt(
            agent_id=agent_id,
            tool_call_id=str(tool_call_id or ""),
            origin_session_id=str(origin_session_id or ""),
            origin_turn_anchor_id=origin_turn_anchor_id,
        )
        if receipt_id is None:
            return "Failed to send file: durable delivery receipt unavailable"

        async def _record_part(part: IMDeliveryPart) -> None:
            await append_delivery_part(receipt_id, part)

        recorder_token = channel_file_part_recorder.set(_record_part)
        try:
            delivery_text, delivery_result = await _send_file_to_session(
                agent_id,
                file_path,
                target_session_id,
                accompany_msg,
            )
            if not await register_delivery(receipt_id, delivery_result):
                raise RuntimeError("delivery finalization failed")
            return delivery_text
        except Exception as exc:
            await register_delivery(
                receipt_id,
                IMDeliveryResult.from_exception("im", exc),
            )
            safe_error = sanitize_user_visible_text(str(exc)).strip()
            return f"Failed to send file: {safe_error or type(exc).__name__}"
        finally:
            channel_file_part_recorder.reset(recorder_token)

    # Priority 1: exact existing Session (person or group).
    if requested_session_id:
        return await _deliver_to_exact_session(requested_session_id)

    # Priority 2: explicit canonical recipient.
    if canonical_user_id:
        receipt_id = await _claim_channel_file_receipt(
            agent_id=agent_id,
            tool_call_id=str(tool_call_id or ""),
            origin_session_id=str(origin_session_id or ""),
            origin_turn_anchor_id=origin_turn_anchor_id,
        )
        if receipt_id is None:
            return "Failed to send file: durable delivery receipt unavailable"
        async def _record_part(part: IMDeliveryPart) -> None:
            await append_delivery_part(receipt_id, part)

        recorder_token = channel_file_part_recorder.set(_record_part)
        try:
            delivery_text, delivery_result = await _send_file_to_recipient(
                agent_id,
                file_path,
                canonical_user_id,
                accompany_msg,
                channel=channel,
            )
            if not await register_delivery(receipt_id, delivery_result):
                raise RuntimeError("delivery finalization failed")
            return delivery_text
        except Exception as exc:
            await register_delivery(
                receipt_id,
                IMDeliveryResult.from_exception("im", exc),
            )
            safe_error = sanitize_user_visible_text(str(exc)).strip()
            return f"Failed to send file: {safe_error or type(exc).__name__}"
        finally:
            channel_file_part_recorder.reset(recorder_token)

    # Priority 3: current durable external Session. DingTalk uses the same
    # proactive route here as explicit cross-session delivery.
    if origin_session_id and await _supports_exact_file_session_route(
        agent_id,
        origin_session_id,
    ):
        return await _deliver_to_exact_session(origin_session_id)

    # Priority 4: legacy channel-initiated sender for transports that have not
    # yet migrated to exact Session routing. DingTalk no longer registers one.
    sender = channel_file_sender.get()
    if sender is not None:
        receipt_id = await _claim_channel_file_receipt(
            agent_id=agent_id,
            tool_call_id=str(tool_call_id or ""),
            origin_session_id=str(origin_session_id or ""),
            origin_turn_anchor_id=origin_turn_anchor_id,
        )
        if receipt_id is None:
            return "Failed to send file: durable delivery receipt unavailable"
        async def _record_part(part: IMDeliveryPart) -> None:
            await append_delivery_part(receipt_id, part)

        recorder_token = channel_file_part_recorder.set(_record_part)
        try:
            delivery_result = await sender(file_path, accompany_msg)
            if not isinstance(delivery_result, IMDeliveryResult):
                delivery_result = IMDeliveryResult.unsupported_delivery(
                    "im",
                    "legacy_channel_file",
                )
            if not await register_delivery(receipt_id, delivery_result):
                raise RuntimeError("delivery finalization failed")
            if not delivery_result.ok:
                return f"Failed to send file: {delivery_result.error or delivery_result.status}"
            return f"File '{file_path.name}' sent to user via channel."
        except Exception as e:
            await register_delivery(
                receipt_id,
                IMDeliveryResult.from_exception("im", e),
            )
            safe_error = sanitize_user_visible_text(str(e)).strip()
            return f"Failed to send file: {safe_error or type(e).__name__}"
        finally:
            channel_file_part_recorder.reset(recorder_token)

    # Priority 5: Web/H5 chat fallback — return a structured platform file
    # delivery payload. The frontend builds the authenticated download URL.
    base_abs = _agent_workspace_root(agent_id).resolve()
    try:
        file_rel = file_path.resolve().relative_to(base_abs).as_posix()
    except ValueError:
        file_rel = rel_path
    return _platform_file_delivery_result(file_path, file_rel, accompany_msg)




async def _send_channel_media(
    agent_id: uuid.UUID,
    ws: Path,
    arguments: dict,
    *,
    media_kind: str,
    tool_call_id: str | None = None,
    origin_session_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Create one audio/video delivery without leaking renderer/IM details."""
    arguments = dict(arguments)
    if "message" in arguments:
        arguments["message"] = sanitize_user_visible_text(
            str(arguments.get("message") or "")
        ).strip()
    if "title" in arguments:
        arguments["title"] = normalize_media_display_title(
            sanitize_user_visible_text(str(arguments.get("title") or ""))
        )
    if not str(tool_call_id or "").strip():
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "MISSING_DELIVERY_INTENT_ID",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    raw_rel_path = arguments.get("file_path", "")
    raw_url = arguments.get("url", "")
    rel_path = raw_rel_path.strip() if isinstance(raw_rel_path, str) else ""
    media_url = raw_url.strip() if isinstance(raw_url, str) else ""
    url_mode = str(arguments.get("url_mode") or "").strip().lower()
    has_headers = "headers" in arguments
    managed_headers: dict[str, str] = {}
    has_file = bool(rel_path)
    has_url = bool(media_url)
    if has_file == has_url:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "INVALID_MEDIA_SOURCE",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    if has_file and url_mode:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "INVALID_MEDIA_SOURCE",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    if has_url and url_mode not in {"external", "managed"}:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "INVALID_URL_MODE",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    if has_headers and not (has_url and url_mode == "managed"):
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "INVALID_MEDIA_HEADERS",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    if has_headers:
        try:
            managed_headers = normalize_managed_media_headers(arguments.get("headers"))
        except MediaUrlError as exc:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": exc.code,
                    "media_kind": media_kind,
                },
                ensure_ascii=False,
            )

    canonical_user_id = str(arguments.get("user_id") or "").strip()
    requested_session_id = str(arguments.get("session_id") or "").strip()
    requested_channel = str(arguments.get("channel") or "").strip().lower() or None
    if canonical_user_id and requested_session_id:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "AMBIGUOUS_MEDIA_TARGET",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    if requested_channel and not canonical_user_id:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "CHANNEL_REQUIRES_USER_TARGET",
                "media_kind": media_kind,
            },
            ensure_ascii=False,
        )
    target_session_id = requested_session_id or (str(origin_session_id or "").strip() if not canonical_user_id else "")
    if not target_session_id and not canonical_user_id:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "SESSION_REQUIRED",
                "media_kind": media_kind,
                "intent_id": tool_call_id or "",
            },
            ensure_ascii=False,
        )

    file_path: Path | None = None
    managed_import = None
    source_mode = "workspace"
    if has_file:
        rel_path = _normalize_tool_workspace_rel_path(rel_path)
        if not rel_path:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "INVALID_FILE_PATH",
                    "media_kind": media_kind,
                },
                ensure_ascii=False,
            )
        agent_root = ws.resolve()
        file_path = (agent_root / rel_path).resolve()
        try:
            file_path.relative_to(agent_root)
        except ValueError:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "INVALID_FILE_PATH",
                    "media_kind": media_kind,
                },
                ensure_ascii=False,
            )
        if not file_path.exists() or not file_path.is_file():
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "MEDIA_NOT_FOUND",
                    "media_kind": media_kind,
                },
                ensure_ascii=False,
            )
        actual_mime = _sniff_media_file_mime(file_path)
        actual_kind = actual_mime.split("/", 1)[0] if actual_mime else None
        if actual_kind != media_kind:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "MEDIA_KIND_MISMATCH",
                    "media_kind": media_kind,
                    "actual_kind": actual_kind,
                },
                ensure_ascii=False,
            )
    else:
        if url_mode == "external":
            try:
                media_url = await validate_media_url(media_url, external=True)
            except MediaUrlError as exc:
                return json.dumps(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "failed",
                        "code": exc.code,
                        "media_kind": media_kind,
                    },
                    ensure_ascii=False,
                )

    cover_path: Path | None = None
    if media_kind == "video" and arguments.get("cover_image_path"):
        raw_cover_path = str(arguments.get("cover_image_path") or "").strip()
        cover_rel_path = _normalize_tool_workspace_rel_path(raw_cover_path)
        if not cover_rel_path:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "INVALID_COVER_PATH",
                    "media_kind": media_kind,
                }
            )
        cover_path = (ws / cover_rel_path).resolve()
        try:
            cover_path.relative_to(ws.resolve())
        except ValueError:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "INVALID_COVER_PATH",
                    "media_kind": media_kind,
                }
            )
        if not cover_path.exists() or not cover_path.is_file() or _sniff_image_file(cover_path) is None:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "INVALID_VIDEO_COVER",
                    "media_kind": media_kind,
                }
            )

    if has_url and url_mode == "managed":
        replay = await _replay_terminal_media_delivery(
            agent_id=agent_id,
            origin_session_id=origin_session_id,
            intent_id=tool_call_id or "",
            origin_turn_anchor_id=origin_turn_anchor_id,
        )
        if replay is not None:
            return json.dumps(replay, ensure_ascii=False)
        preflight_error = await _preflight_managed_media_target(
            agent_id=agent_id,
            session_id=target_session_id,
            user_id=canonical_user_id,
            channel=requested_channel,
            media_kind=media_kind,
            intent_id=tool_call_id or "",
        )
        if preflight_error is not None:
            return json.dumps(
                _describe_media_delivery_result(preflight_error),
                ensure_ascii=False,
            )

    tool_config = await _get_tool_config(agent_id, "send_media") or {}
    allow_download = tool_config.get("allow_download") is True
    if has_url and url_mode == "external":
        if canonical_user_id:
            return json.dumps(
                _describe_media_delivery_result(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "unsupported",
                        "code": "EXTERNAL_URL_NOT_SUPPORTED_BY_CHANNEL",
                        "media_kind": media_kind,
                        "intent_id": tool_call_id or "",
                    }
                ),
                ensure_ascii=False,
            )
        return await _publish_external_media_to_session(
            agent_id=agent_id,
            session_id=target_session_id,
            media_url=media_url,
            media_kind=media_kind,
            caption=str(arguments.get("message") or ""),
            intent_id=tool_call_id or "",
            origin_session_id=origin_session_id,
            origin_turn_anchor_id=origin_turn_anchor_id,
            allow_download=allow_download,
            tool_args=arguments,
        )

    if has_url:
        try:
            imported = await import_managed_media_url(
                media_url,
                agent_workspace=_agent_workspace_root(agent_id),
                session_id=origin_session_id or target_session_id or None,
                intent_id=tool_call_id or "",
                max_bytes=MEDIA_TOOL_MAX_FILE_BYTES,
                expected_media_kind=media_kind,
                operation_scope=(
                    _build_outbound_operation_key(
                        agent_id=agent_id,
                        origin_session_id=(origin_session_id or target_session_id or None),
                        tool_call_id=tool_call_id,
                        origin_turn_anchor_id=origin_turn_anchor_id,
                    )
                    or f"outbound-untracked:{agent_id}:{uuid.uuid4().hex}"
                ),
                request_headers=managed_headers,
            )
        except MediaUrlError as exc:
            payload = {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": exc.code,
                "media_kind": media_kind,
                "intent_id": tool_call_id or "",
            }
            if exc.http_status is not None:
                payload["http_status"] = exc.http_status
            if exc.actual_kind is not None:
                payload["actual_kind"] = exc.actual_kind
            return json.dumps(_describe_media_delivery_result(payload), ensure_ascii=False)
        file_path = imported.file_path
        rel_path = imported.workspace_path
        managed_import = imported
        source_mode = "managed_url"

    try:
        if managed_import is not None:
            try:
                await get_storage_backend().write_local_file(
                    current_agent_runtime_workspace(agent_id).storage_key(rel_path),
                    file_path,
                    content_type=managed_import.mime_type,
                )
            except Exception:
                logger.opt(exception=True).error("[SessionMedia] Managed import persistence failed")
                return json.dumps(
                    _describe_media_delivery_result(
                        {
                            "type": "media_delivery_result",
                            "version": 1,
                            "status": "failed",
                            "code": "MEDIA_STORAGE_FAILED",
                            "media_kind": media_kind,
                            "intent_id": tool_call_id or "",
                            "managed_path": rel_path,
                        }
                    ),
                    ensure_ascii=False,
                )

        assert file_path is not None and rel_path is not None
        if target_session_id:
            return await _send_media_to_session(
                agent_id=agent_id,
                session_id=target_session_id,
                file_path=file_path,
                workspace_path=rel_path,
                media_kind=media_kind,
                caption=str(arguments.get("message") or ""),
                cover_path=cover_path,
                intent_id=tool_call_id or "",
                origin_session_id=origin_session_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                allow_download=allow_download,
                source_mode=source_mode,
                tool_args=arguments,
            )
        if canonical_user_id:
            return await _send_media_to_recipient(
                agent_id=agent_id,
                file_path=file_path,
                workspace_path=rel_path,
                user_id=canonical_user_id,
                channel=requested_channel,
                media_kind=media_kind,
                caption=str(arguments.get("message") or ""),
                cover_path=cover_path,
                intent_id=tool_call_id or "",
                origin_session_id=origin_session_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                allow_download=allow_download,
                source_mode=source_mode,
                tool_args=arguments,
            )

        raise AssertionError("validated media target was not routed")
    finally:
        if managed_import is not None:
            close_import = getattr(managed_import, "close", None)
            if callable(close_import):
                close_import()


async def _replay_terminal_media_delivery(
    *,
    agent_id: uuid.UUID,
    origin_session_id: str | None,
    intent_id: str,
    origin_turn_anchor_id: uuid.UUID | None,
) -> dict | None:
    """Bound replay waits before they reserve a database connection."""
    async with _outbound_media_slots:
        return await _replay_terminal_media_delivery_under_slot(
            agent_id=agent_id,
            origin_session_id=origin_session_id,
            intent_id=intent_id,
            origin_turn_anchor_id=origin_turn_anchor_id,
        )


async def _replay_terminal_media_delivery_under_slot(
    *,
    agent_id: uuid.UUID,
    origin_session_id: str | None,
    intent_id: str,
    origin_turn_anchor_id: uuid.UUID | None,
) -> dict | None:
    """Return or safely converge a durable receipt before touching a managed URL."""
    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=intent_id or None,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    if not operation_key:
        return None
    recovered_event: dict | None = None
    recovered_session_id = ""
    result_payload: dict | None = None
    async with async_session() as db:
        await _lock_outbound_operation(db, operation_key)
        existing = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.external_event_key == operation_key,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is None or existing.role != "tool_call":
            return None
        try:
            stored_call = json.loads(existing.content or "")
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            stored_call = {}
        meta = existing.message_meta if isinstance(existing.message_meta, dict) else {}
        if str(meta.get("delivery_status") or "") == "pending":
            next_meta = {
                **meta,
                "delivery_status": "unknown",
                "delivery_code": "MEDIA_DELIVERY_STATE_UNKNOWN",
            }
            existing.message_meta = next_meta
            stored_args = stored_call.get("args")
            if not isinstance(stored_args, dict):
                stored_args = {}
            media_kind = str(next_meta.get("media_kind") or stored_args.get("media_type") or "")
            result_payload = _describe_media_delivery_result(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "unknown",
                    "code": "MEDIA_DELIVERY_STATE_UNKNOWN",
                    "media_kind": media_kind or None,
                    "intent_id": intent_id,
                    "session_id": str(existing.conversation_id),
                    "channel": str(next_meta.get("source_channel") or ""),
                    "message_id": str(existing.id),
                }
            )
            if not stored_args and media_kind:
                stored_args = {"media_type": media_kind}
            existing.content = json.dumps(
                {
                    **stored_call,
                    "name": str(stored_call.get("name") or "send_media"),
                    "call_id": str(stored_call.get("call_id") or intent_id),
                    "args": stored_args,
                    "status": "done",
                    "result": json.dumps(result_payload, ensure_ascii=False),
                    "reasoning_content": stored_call.get("reasoning_content"),
                },
                ensure_ascii=False,
            )
            recovered_session_id = str(existing.conversation_id)
            recovered_event = {
                "type": "tool_call",
                "id": str(existing.id),
                "message_id": str(existing.id),
                "name": str(stored_call.get("name") or "send_media"),
                "call_id": str(stored_call.get("call_id") or intent_id),
                "args": stored_args,
                "status": "done",
                "result": json.dumps(result_payload, ensure_ascii=False),
                "created_at": existing.created_at.isoformat() if existing.created_at else None,
            }
            await db.commit()
        else:
            if stored_call.get("status") != "done":
                return None
            try:
                stored_result = json.loads(stored_call.get("result") or "")
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                return None
            if not isinstance(stored_result, dict):
                return None
            status = str(stored_result.get("status") or "")
            if status == "sent":
                code = str(stored_result.get("code") or "")
                result_payload = _describe_media_delivery_result(
                    {
                        **stored_result,
                        "status": "already_sent",
                        "code": (
                            "MEDIA_SENT_CAPTION_FAILED" if code == "MEDIA_SENT_CAPTION_FAILED" else "MEDIA_ALREADY_SENT"
                        ),
                    }
                )
            elif status in {"already_sent", "failed", "unsupported", "unknown"}:
                result_payload = _describe_media_delivery_result(stored_result)
            else:
                return None

    if recovered_event is not None and recovered_session_id:
        try:
            from app.api.websocket import manager as ws_manager

            recovered_event["session_id"] = recovered_session_id
            await ws_manager.send_to_session(
                str(agent_id),
                recovered_session_id,
                recovered_event,
            )
        except Exception:
            logger.opt(exception=True).warning("[SessionMedia] Unknown-state recovery live mirror failed")
    return result_payload


















async def _publish_external_media_to_session(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    media_url: str,
    media_kind: str,
    caption: str,
    intent_id: str,
    origin_session_id: str | None,
    origin_turn_anchor_id: uuid.UUID | None,
    allow_download: bool,
    tool_args: dict,
) -> str:
    """Publish an external HTTPS URL as one standard tool-call record."""
    try:
        target_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return json.dumps(
            _describe_media_delivery_result(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                }
            ),
            ensure_ascii=False,
        )
    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=intent_id or None,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    if not operation_key:
        return json.dumps(
            _describe_media_delivery_result(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "MISSING_DELIVERY_INTENT_ID",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                }
            ),
            ensure_ascii=False,
        )

    filename = _external_media_filename(media_url, media_kind)
    display_title = normalize_media_display_title(tool_args.get("title"))
    guessed_mime = mimetypes.guess_type(filename)[0]
    mime_type = guessed_mime if str(guessed_mime or "").startswith(f"{media_kind}/") else None
    events: list[dict] = []
    result_payload: dict
    async with async_session() as db:
        await _lock_outbound_operation(db, operation_key)
        existing = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.external_event_key == operation_key).with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            try:
                existing_call = json.loads(existing.content or "")
                existing_result = json.loads(existing_call.get("result") or "")
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                existing_result = None
            if isinstance(existing_result, dict):
                if existing_result.get("status") == "sent":
                    existing_result = {**existing_result, "status": "already_sent", "code": "MEDIA_ALREADY_SENT"}
                return json.dumps(existing_result, ensure_ascii=False)
            return json.dumps(
                _describe_media_delivery_result(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "unknown",
                        "code": "MEDIA_DELIVERY_STATE_UNKNOWN",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "message_id": str(existing.id),
                    }
                ),
                ensure_ascii=False,
            )

        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == target_session_id,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            return json.dumps(
                _describe_media_delivery_result(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "failed",
                        "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                    }
                ),
                ensure_ascii=False,
            )
        channel = str(session.source_channel or "").strip()
        if channel not in _PLATFORM_SESSION_CHANNELS:
            return json.dumps(
                _describe_media_delivery_result(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "unsupported",
                        "code": "EXTERNAL_URL_NOT_SUPPORTED_BY_CHANNEL",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(session.id),
                        "channel": channel,
                    }
                ),
                ensure_ascii=False,
            )

        receipt: ChatMessage | None = None
        receipt_tool_payload: dict = {}
        if str(origin_session_id or "") == str(session.id) and origin_turn_anchor_id:
            running_rows = (
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.agent_id == agent_id,
                            ChatMessage.conversation_id == str(session.id),
                            ChatMessage.role == "tool_call",
                            ChatMessage.external_event_key.is_(None),
                            ChatMessage.message_meta["turn_anchor_id"].as_string() == str(origin_turn_anchor_id),
                        )
                        .order_by(ChatMessage.created_at.desc())
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
            for candidate in running_rows:
                try:
                    candidate_payload = json.loads(candidate.content or "")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(candidate_payload, dict)
                    and str(candidate_payload.get("name") or "") == "send_media"
                    and str(candidate_payload.get("call_id") or "") == intent_id
                ):
                    receipt = candidate
                    receipt_tool_payload = dict(candidate_payload)
                    break

        claim_meta = {
            "direction": "outbound",
            "source_channel": channel,
            "target_session_id": str(session.id),
            "origin_session_id": str(origin_session_id or ""),
            "tool_call_id": intent_id,
            "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
            "delivery_claim": str(origin_session_id or "") != str(session.id),
            "delivery_status": "sent",
            "delivery_code": "EXTERNAL_MEDIA_PUBLISHED",
            "delivery_mode": "external_url",
            "source_mode": "external_url",
            "media_kind": media_kind,
            "attachments": [],
            "allow_download": allow_download,
            "caption_status": "sent" if caption.strip() else "not_requested",
            "display_title": display_title,
        }
        if receipt is None:
            receipt = ChatMessage(
                agent_id=agent_id,
                user_id=session.user_id,
                role="tool_call",
                content="",
                conversation_id=str(session.id),
                external_event_key=operation_key,
                message_meta=claim_meta,
            )
            db.add(receipt)
        else:
            receipt.external_event_key = operation_key
            receipt.message_meta = {**dict(receipt.message_meta or {}), **claim_meta}
        session.last_message_at = datetime.now(timezone.utc)
        await db.flush()
        result_payload = {
            "type": "platform_media_delivery",
            "version": 1,
            "status": "sent",
            "code": "EXTERNAL_MEDIA_PUBLISHED",
            "media_kind": media_kind,
            "url": media_url,
            "source_mode": "external_url",
            "filename": filename,
            **({"title": display_title} if display_title else {}),
            **({"mime_type": mime_type} if mime_type else {}),
            "message_id": str(receipt.id),
            "allow_download": allow_download,
            "intent_id": intent_id,
            "session_id": str(session.id),
            "channel": channel,
            "conversation_type": "group" if session.is_group else "person",
            "delivery_mode": "external_url",
        }
        receipt.content = json.dumps(
            {
                **receipt_tool_payload,
                "name": "send_media",
                "call_id": intent_id,
                "args": dict(tool_args),
                "status": "done",
                "result": json.dumps(result_payload, ensure_ascii=False),
                "reasoning_content": receipt_tool_payload.get("reasoning_content"),
            },
            ensure_ascii=False,
        )
        caption_row: ChatMessage | None = None
        if caption.strip():
            caption_row = ChatMessage(
                agent_id=agent_id,
                user_id=session.user_id,
                role="assistant",
                content=caption.strip(),
                conversation_id=str(session.id),
                external_event_key=f"{operation_key}:caption",
                message_meta={
                    "direction": "outbound",
                    "source_channel": channel,
                    "target_session_id": str(session.id),
                    "origin_session_id": str(origin_session_id or ""),
                    "tool_call_id": intent_id,
                    "delivery_status": "sent",
                    "media_caption_for": str(receipt.id),
                    "attachments": [],
                },
            )
            db.add(caption_row)
        await db.commit()
        await db.refresh(receipt)
        events.append(
            {
                "type": "tool_call",
                "id": str(receipt.id),
                "message_id": str(receipt.id),
                "name": "send_media",
                "call_id": intent_id,
                "args": dict(tool_args),
                "status": "done",
                "result": json.dumps(result_payload, ensure_ascii=False),
                "created_at": receipt.created_at.isoformat() if receipt.created_at else None,
            }
        )
        if caption_row is not None:
            from app.services.chat_message_serializer import serialize_chat_message_for_client

            await db.refresh(caption_row)
            caption_event = serialize_chat_message_for_client(caption_row, source_channel=channel)
            caption_event["type"] = "assistant_message_committed"
            events.append(caption_event)

    try:
        from app.api.websocket import manager as ws_manager

        for event in events:
            event["session_id"] = str(target_session_id)
            await ws_manager.send_to_session(str(agent_id), str(target_session_id), event)
    except Exception:
        logger.opt(exception=True).warning("[SessionMedia] External URL live mirror failed")
    return json.dumps(result_payload, ensure_ascii=False)


async def _send_media_to_session(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    file_path: Path,
    workspace_path: str,
    media_kind: str,
    caption: str,
    cover_path: Path | None,
    intent_id: str,
    origin_session_id: str | None,
    origin_turn_anchor_id: uuid.UUID | None,
    allow_download: bool = False,
    source_mode: str = "workspace",
    tool_args: dict | None = None,
) -> str:
    """Serialize one complete media delivery, including the provider call."""
    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=intent_id or None,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    async with _outbound_operation_lifecycle_lock(operation_key):
        return await _send_media_to_session_under_lifecycle_lock(
            agent_id=agent_id,
            session_id=session_id,
            file_path=file_path,
            workspace_path=workspace_path,
            media_kind=media_kind,
            caption=caption,
            cover_path=cover_path,
            intent_id=intent_id,
            origin_session_id=origin_session_id,
            origin_turn_anchor_id=origin_turn_anchor_id,
            allow_download=allow_download,
            source_mode=source_mode,
            tool_args=tool_args,
        )


async def _send_media_to_session_under_lifecycle_lock(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    file_path: Path,
    workspace_path: str,
    media_kind: str,
    caption: str,
    cover_path: Path | None,
    intent_id: str,
    origin_session_id: str | None,
    origin_turn_anchor_id: uuid.UUID | None,
    allow_download: bool = False,
    source_mode: str = "workspace",
    tool_args: dict | None = None,
) -> str:
    """Deliver media through one exact Session with a durable no-duplicate claim."""
    try:
        target_session_id = uuid.UUID(str(session_id))
    except (TypeError, ValueError):
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
                "media_kind": media_kind,
                "intent_id": intent_id,
            },
            ensure_ascii=False,
        )

    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=intent_id or None,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    sniffed_mime = _sniff_media_file_mime(file_path)
    mime_type = canonical_media_mime(file_path.name, media_kind, sniffed_mime)
    attachment = attachment_from_workspace_path(
        workspace_path,
        display_name=file_path.name,
        mime_type=mime_type,
        size_bytes=file_path.stat().st_size,
    )
    persisted_args = dict(tool_args or {"media_type": media_kind, "file_path": workspace_path})
    display_title = normalize_media_display_title(persisted_args.get("title"))
    if not operation_key:
        return json.dumps(
            {
                "type": "media_delivery_result",
                "version": 1,
                "status": "failed",
                "code": "MISSING_DELIVERY_INTENT_ID",
                "media_kind": media_kind,
                "intent_id": intent_id,
            },
            ensure_ascii=False,
        )

    receipt_id: uuid.UUID
    channel = ""
    external_conv_id = ""
    target_is_group = False
    delivery_mode = "platform"
    dingtalk_app_id = ""
    dingtalk_app_secret = ""
    target_id = ""
    conversation_type = ""
    async with _outbound_media_db_session() as db:
        existing = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.external_event_key == operation_key).with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            meta = existing.message_meta if isinstance(existing.message_meta, dict) else {}
            state = str(meta.get("delivery_status") or "unknown")
            if state == "sent":
                caption_status = str(meta.get("caption_status") or "not_requested")
                existing_display_title = normalize_media_display_title(meta.get("display_title"))
                existing_result = _describe_media_delivery_result(
                    {
                        "type": "platform_media_delivery",
                        "version": 1,
                        "status": "already_sent",
                        "code": ("MEDIA_SENT_CAPTION_FAILED" if caption_status == "failed" else "MEDIA_ALREADY_SENT"),
                        "caption_status": caption_status,
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(existing.conversation_id),
                        "channel": str(meta.get("source_channel") or ""),
                        "path": workspace_path,
                        "filename": file_path.name,
                        **({"title": existing_display_title} if existing_display_title else {}),
                        "mime_type": mime_type,
                        "size": file_path.stat().st_size,
                        "message_id": str(existing.id),
                        "allow_download": meta.get("allow_download") is True,
                        "source_mode": str(meta.get("source_mode") or source_mode),
                    }
                )
                return json.dumps(existing_result, ensure_ascii=False)
            if state == "pending":
                next_meta = {
                    **meta,
                    "delivery_status": "unknown",
                    "delivery_code": "MEDIA_DELIVERY_STATE_UNKNOWN",
                }
                existing.message_meta = next_meta
                unknown_result = _describe_media_delivery_result(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "unknown",
                        "code": "MEDIA_DELIVERY_STATE_UNKNOWN",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(existing.conversation_id),
                        "channel": str(next_meta.get("source_channel") or ""),
                        "message_id": str(existing.id),
                    }
                )
                if existing.role == "tool_call":
                    try:
                        existing_call = json.loads(existing.content or "")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        existing_call = {}
                    existing.content = json.dumps(
                        {
                            "name": str(existing_call.get("name") or "send_media"),
                            "call_id": str(existing_call.get("call_id") or intent_id),
                            "args": existing_call.get("args")
                            or {
                                "media_type": media_kind,
                                "file_path": workspace_path,
                            },
                            "status": "done",
                            "result": json.dumps(unknown_result, ensure_ascii=False),
                            "reasoning_content": existing_call.get("reasoning_content"),
                        },
                        ensure_ascii=False,
                    )
                await db.commit()
                return json.dumps(unknown_result, ensure_ascii=False)
            existing_result = _describe_media_delivery_result(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": state if state in {"failed", "unknown", "unsupported"} else "unknown",
                    "code": (
                        "MEDIA_DELIVERY_STATE_UNKNOWN"
                        if state == "unknown"
                        else str(meta.get("delivery_code") or "MEDIA_DELIVERY_FAILED")
                    ),
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "session_id": str(existing.conversation_id),
                    "channel": str(meta.get("source_channel") or ""),
                    "message_id": str(existing.id),
                }
            )
            return json.dumps(existing_result, ensure_ascii=False)

        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == target_session_id,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "failed",
                    "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                },
                ensure_ascii=False,
            )

        channel = str(session.source_channel or "").strip()
        external_conv_id = str(session.external_conv_id or "").strip()
        is_platform = channel in _PLATFORM_SESSION_CHANNELS
        target_is_group = bool(session.is_group)
        if is_platform:
            delivery_mode = "platform"
        elif channel == "dingtalk":
            if not external_conv_id or "__archived_" in external_conv_id:
                return json.dumps(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "failed",
                        "code": "SESSION_ROUTE_UNAVAILABLE",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(session.id),
                        "channel": channel,
                    },
                    ensure_ascii=False,
                )
            config = (
                await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id,
                        ChannelConfig.channel_type == "dingtalk",
                        ChannelConfig.is_configured.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if not config or not config.app_id or not config.app_secret:
                return json.dumps(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "unsupported",
                        "code": "CHANNEL_MEDIA_UNSUPPORTED",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(session.id),
                        "channel": channel,
                    },
                    ensure_ascii=False,
                )
            expected_prefix = "dingtalk_group_" if target_is_group else "dingtalk_p2p_"
            wrong_prefix = "dingtalk_p2p_" if target_is_group else "dingtalk_group_"
            if not external_conv_id.startswith(expected_prefix) or external_conv_id.startswith(wrong_prefix):
                return json.dumps(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "failed",
                        "code": "SESSION_ROUTE_MISMATCH",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(session.id),
                        "channel": channel,
                    },
                    ensure_ascii=False,
                )
            target_id = external_conv_id[len(expected_prefix) :].strip()
            if not target_id:
                return json.dumps(
                    {
                        "type": "media_delivery_result",
                        "version": 1,
                        "status": "failed",
                        "code": "SESSION_ROUTE_UNAVAILABLE",
                        "media_kind": media_kind,
                        "intent_id": intent_id,
                        "session_id": str(session.id),
                        "channel": channel,
                    },
                    ensure_ascii=False,
                )
            conversation_type = "2" if target_is_group else "1"
            dingtalk_app_id = str(config.app_id)
            dingtalk_app_secret = str(config.app_secret)
            delivery_mode = "native"
        else:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "unsupported",
                    "code": "CHANNEL_MEDIA_UNSUPPORTED",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "session_id": str(session.id),
                    "channel": channel,
                },
                ensure_ascii=False,
            )

        receipt: ChatMessage | None = None
        if str(origin_session_id or "") == str(session.id) and origin_turn_anchor_id:
            running_rows = (
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.agent_id == agent_id,
                            ChatMessage.conversation_id == str(session.id),
                            ChatMessage.role == "tool_call",
                            ChatMessage.external_event_key.is_(None),
                            ChatMessage.message_meta["turn_anchor_id"].as_string() == str(origin_turn_anchor_id),
                        )
                        .order_by(ChatMessage.created_at.desc())
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
            for candidate in running_rows:
                try:
                    candidate_payload = json.loads(candidate.content or "")
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(candidate_payload, dict)
                    and str(candidate_payload.get("name") or "") == "send_media"
                    and str(candidate_payload.get("call_id") or "") == intent_id
                ):
                    receipt = candidate
                    break

        is_cross_session_claim = str(origin_session_id or "") != str(session.id)
        from app.services.im_delivery import IMDeliveryResult, attach_delivery_to_meta

        claim_meta = {
            "direction": "outbound",
            "source_channel": channel,
            "target_session_id": str(session.id),
            "origin_session_id": str(origin_session_id or ""),
            "tool_call_id": intent_id,
            "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
            # Cross-session mirrors are UI delivery records, not part of the
            # target Agent's reasoning history. A current-session row is the
            # caller's canonical tool result and must remain in LLM replay.
            "delivery_claim": is_cross_session_claim,
            "delivery_status": "pending",
        }
        claim_meta = attach_delivery_to_meta(
            claim_meta,
            IMDeliveryResult.pending(channel),
        )
        if receipt is None:
            receipt = ChatMessage(
                agent_id=agent_id,
                user_id=session.user_id,
                role="tool_call",
                content=json.dumps(
                    {
                        "name": "send_media",
                        "call_id": intent_id,
                        "args": persisted_args,
                        "status": "running",
                        "result": "",
                        "reasoning_content": None,
                    },
                    ensure_ascii=False,
                ),
                conversation_id=str(session.id),
                external_event_key=operation_key,
                message_meta=claim_meta,
            )
            db.add(receipt)
        else:
            receipt.external_event_key = operation_key
            receipt.message_meta = {**dict(receipt.message_meta or {}), **claim_meta}
        session.last_message_at = datetime.now(timezone.utc)
        await db.flush()
        receipt_meta = dict(receipt.message_meta or {})
        receipt_meta["attachments"] = [attachment]
        receipt_meta["media_kind"] = media_kind
        receipt_meta["delivery_mode"] = delivery_mode
        receipt_meta["caption_status"] = "pending" if caption.strip() else "not_requested"
        receipt_meta["requested_caption"] = caption.strip()
        receipt_meta["target_is_group"] = target_is_group
        receipt_meta["delivery_code"] = "MEDIA_DELIVERY_PENDING"
        receipt_meta["allow_download"] = allow_download
        receipt_meta["source_mode"] = source_mode
        receipt_meta["display_title"] = display_title
        receipt.message_meta = receipt_meta
        await db.commit()
        receipt_id = receipt.id

    sent = True
    code = "MEDIA_SENT"
    uncertain = False
    caption_sent = channel != "dingtalk" or not bool(caption.strip())
    caption_delivery_result: IMDeliveryResult | None = None
    media_delivery_parts: list[IMDeliveryPart] = []
    media_delivery_result = IMDeliveryResult.unsupported_delivery(
        channel or "web",
        "platform_session",
        conversation_ref=str(target_session_id),
    )
    if channel == "dingtalk":
        from app.services.dingtalk_stream import (
            DINGTALK_VOICE_MAX_BYTES,
            _send_dingtalk_media_message,
            _send_dingtalk_native_video,
            _upload_dingtalk_media,
        )

        async def _record_media_part(provider_result: dict) -> None:
            process_key = str(provider_result.get("processQueryKey") or "") or None
            part = IMDeliveryPart(
                transport=(
                    "dingtalk_openapi_group"
                    if target_is_group
                    else "dingtalk_openapi_oto"
                ),
                provider_message_id=process_key,
                conversation_ref=target_id,
                artifact_role=f"media_{media_kind}",
                recallable=bool(process_key),
            )
            media_delivery_parts.append(part)
            async with _outbound_media_db_session() as part_db:
                part_receipt = await part_db.get(
                    ChatMessage,
                    receipt_id,
                    with_for_update=True,
                )
                if part_receipt is None:
                    raise DeliveryReceiptPersistenceError(
                        "provider artifact receipt persistence failed"
                    )
                part_receipt.message_meta = _merge_delivery_into_meta(
                    part_receipt.message_meta,
                    IMDeliveryResult(
                        ok=True,
                        channel=channel,
                        parts=(part,),
                        status="pending",
                    ),
                )
                await part_db.commit()

        try:
            if media_kind == "video":
                sent, code = await _send_dingtalk_native_video(
                    dingtalk_app_id,
                    dingtalk_app_secret,
                    target_id,
                    file_path,
                    conversation_type,
                    cover_image_path=cover_path,
                    raise_on_transport_error=True,
                    on_result=_record_media_part,
                )
            elif file_path.stat().st_size > DINGTALK_VOICE_MAX_BYTES:
                sent, code = False, "MEDIA_TOO_LARGE"
            else:
                media_id = await _upload_dingtalk_media(
                    dingtalk_app_id,
                    dingtalk_app_secret,
                    str(file_path),
                    "voice",
                    raise_on_transport_error=True,
                )
                sent = bool(media_id) and await _send_dingtalk_media_message(
                    dingtalk_app_id,
                    dingtalk_app_secret,
                    target_id,
                    str(media_id or ""),
                    "voice",
                    conversation_type,
                    filename=file_path.name,
                    raise_on_transport_error=True,
                    on_result=_record_media_part,
                )
                code = "MEDIA_SENT" if sent else (
                    "MEDIA_SEND_FAILED" if media_id else "MEDIA_UPLOAD_FAILED"
                )
        except Exception as exc:
            logger.opt(exception=True).error("[SessionMedia] Provider result is unknown")
            sent, uncertain, code = False, True, "MEDIA_DELIVERY_STATE_UNKNOWN"
            media_delivery_result = IMDeliveryResult.unknown(channel, type(exc).__name__)
        else:
            if sent:
                if not media_delivery_parts:
                    media_delivery_parts.append(
                        IMDeliveryPart(
                            transport=(
                                "dingtalk_openapi_group"
                                if target_is_group
                                else "dingtalk_openapi_oto"
                            ),
                            conversation_ref=target_id,
                            artifact_role=f"media_{media_kind}",
                            recallable=False,
                        )
                    )
                media_delivery_result = IMDeliveryResult.sent(
                    channel,
                    *media_delivery_parts,
                )
            else:
                media_delivery_result = IMDeliveryResult.failed(channel, code)

        if sent and caption.strip():
            runtime = TurnRuntime(
                session_found=True,
                source_channel=channel,
                conversation_id=str(target_session_id),
                external_conv_id=external_conv_id,
                is_group=target_is_group,
            )
            caption_operation_key = f"{operation_key}:caption"
            async with _outbound_media_db_session() as caption_db:
                caption_row = (
                    await caption_db.execute(
                        select(ChatMessage).where(
                            ChatMessage.external_event_key == caption_operation_key
                        )
                    )
                ).scalar_one_or_none()
                if caption_row is None:
                    media_receipt = await caption_db.get(ChatMessage, receipt_id)
                    caption_row = ChatMessage(
                        agent_id=agent_id,
                        user_id=media_receipt.user_id if media_receipt is not None else None,
                        role="assistant",
                        content=caption.strip(),
                        conversation_id=str(target_session_id),
                        external_event_key=caption_operation_key,
                        message_meta=attach_delivery_to_meta(
                            {
                                "attachments": [],
                                "media_caption_for": str(receipt_id),
                                "artifact_role": "media_caption",
                            },
                            IMDeliveryResult.pending(channel),
                        ),
                    )
                    caption_db.add(caption_row)
                    await caption_db.flush()
                caption_message_id = caption_row.id
                await caption_db.commit()
            try:
                caption_delivery_result = await deliver_message_with_receipt(
                    agent_id=agent_id,
                    runtime=runtime,
                    message=caption.strip(),
                )
                caption_sent = caption_delivery_result.ok
            except Exception as exc:
                logger.opt(exception=True).warning("[SessionMedia] Caption delivery failed")
                caption_delivery_result = IMDeliveryResult.unknown(
                    channel,
                    type(exc).__name__,
                )
                caption_sent = False

    final_status = "sent" if sent else ("unknown" if uncertain else "failed")
    final_code = "MEDIA_SENT_CAPTION_FAILED" if sent and not caption_sent else code
    events: list[dict] = []
    async with _outbound_media_db_session() as db:
        final_receipt = await db.get(ChatMessage, receipt_id, with_for_update=True)
        if final_receipt is None:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "unknown",
                    "code": "MEDIA_DELIVERY_STATE_UNKNOWN",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "session_id": str(target_session_id),
                    "channel": channel,
                },
                ensure_ascii=False,
            )
        final_meta = dict(final_receipt.message_meta or {})
        try:
            final_tool_payload = json.loads(final_receipt.content or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            final_tool_payload = {}
        if not isinstance(final_tool_payload, dict):
            final_tool_payload = {}
        if str(final_meta.get("delivery_status") or "") != "pending":
            try:
                terminal_call = json.loads(final_receipt.content or "")
                terminal_result = json.loads(terminal_call.get("result") or "")
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                terminal_result = None
            if isinstance(terminal_result, dict):
                return json.dumps(
                    _describe_media_delivery_result(terminal_result),
                    ensure_ascii=False,
                )
            return json.dumps(_describe_media_delivery_result({
                "type": "media_delivery_result", "version": 1,
                "status": "unknown", "code": "MEDIA_DELIVERY_STATE_UNKNOWN",
                "media_kind": media_kind, "intent_id": intent_id,
                "session_id": str(target_session_id), "channel": channel,
                "message_id": str(final_receipt.id),
            }), ensure_ascii=False)
        final_meta = _merge_delivery_into_meta(final_meta, media_delivery_result)
        final_meta["delivery_status"] = final_status
        final_meta["delivery_code"] = final_code
        final_meta["caption_status"] = (
            "not_requested" if not caption.strip() else ("sent" if caption_sent else "failed")
        )
        final_receipt.message_meta = final_meta
        result_payload: dict
        if sent:
            result_payload = {
                "type": "platform_media_delivery",
                "version": 1,
                "status": final_status,
                "code": final_code,
                "media_kind": media_kind,
                "path": workspace_path,
                "filename": file_path.name,
                **({"title": display_title} if display_title else {}),
                "mime_type": mime_type,
                "size": file_path.stat().st_size,
                "message_id": str(final_receipt.id),
                "allow_download": allow_download,
                "intent_id": intent_id,
                "session_id": str(target_session_id),
                "channel": channel,
                "conversation_type": "group" if target_is_group else "person",
                "delivery_mode": delivery_mode,
                "source_mode": source_mode,
            }
            result_payload = _describe_media_delivery_result(result_payload)
        else:
            result_payload = _describe_media_delivery_result(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": final_status,
                    "code": final_code,
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "session_id": str(target_session_id),
                    "channel": channel,
                    "message_id": str(final_receipt.id),
                }
            )
        final_receipt.content = json.dumps(
            {
                **final_tool_payload,
                "name": "send_media",
                "call_id": intent_id,
                "args": persisted_args,
                "status": "done",
                "result": json.dumps(result_payload, ensure_ascii=False),
                "reasoning_content": final_tool_payload.get("reasoning_content"),
            },
            ensure_ascii=False,
        )
        caption_row: ChatMessage | None = None
        if sent and caption.strip():
            caption_operation_key = f"{operation_key}:caption"
            caption_row = (
                await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == caption_operation_key))
            ).scalar_one_or_none()
            if caption_row is None and channel != "dingtalk":
                caption_delivery_result = IMDeliveryResult.unsupported_delivery(
                    channel or "web",
                    "platform_session",
                    conversation_ref=str(target_session_id),
                )
                caption_row = ChatMessage(
                    agent_id=agent_id,
                    user_id=final_receipt.user_id,
                    role="assistant",
                    content=caption.strip(),
                    conversation_id=str(target_session_id),
                    external_event_key=caption_operation_key,
                    message_meta={
                        "attachments": [],
                        "media_caption_for": str(receipt_id),
                        "artifact_role": "media_caption",
                    },
                )
                db.add(caption_row)
                await db.flush()
            if caption_row is not None and caption_delivery_result is not None:
                caption_row.message_meta = _merge_delivery_into_meta(
                    caption_row.message_meta,
                    caption_delivery_result,
                )
        await db.commit()
        await db.refresh(final_receipt)
        events.append(
            {
                "type": "tool_call",
                "id": str(final_receipt.id),
                "message_id": str(final_receipt.id),
                "name": "send_media",
                "call_id": intent_id,
                "args": persisted_args,
                "status": "done",
                "result": json.dumps(result_payload, ensure_ascii=False),
                "created_at": final_receipt.created_at.isoformat() if final_receipt.created_at else None,
            }
        )
        if sent:
            from app.services.chat_message_serializer import serialize_chat_message_for_client
            if caption_sent and caption_row is not None:
                await db.refresh(caption_row)
                caption_event = serialize_chat_message_for_client(caption_row, source_channel=channel)
                caption_event["type"] = "assistant_message_committed"
                events.append(caption_event)

    try:
        from app.api.websocket import manager as ws_manager

        for event in events:
            event["session_id"] = str(target_session_id)
            await ws_manager.send_to_session(str(agent_id), str(target_session_id), event)
    except Exception:
        logger.opt(exception=True).warning("[SessionMedia] Web live mirror failed")

    return json.dumps(result_payload, ensure_ascii=False)


async def _send_media_to_recipient(
    *,
    agent_id: uuid.UUID,
    file_path: Path,
    workspace_path: str,
    user_id: str,
    channel: str | None,
    media_kind: str,
    caption: str,
    cover_path: Path | None,
    intent_id: str,
    origin_session_id: str | None,
    origin_turn_anchor_id: uuid.UUID | None,
    allow_download: bool = False,
    source_mode: str = "workspace",
    tool_args: dict | None = None,
) -> str:
    """Resolve a person, bind/reuse their Session, then use exact-Session delivery."""
    from app.services.recipient_resolver import (
        RecipientResolutionError,
        resolve_human_channel_recipient,
    )

    async with async_session() as db:
        try:
            route = await resolve_human_channel_recipient(db, agent_id, user_id, channel=channel)
        except RecipientResolutionError as exc:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "unsupported",
                    "code": exc.code,
                    "message": exc.message,
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "available_channels": exc.available_channels,
                },
                ensure_ascii=False,
            )
        if route.channel != "dingtalk":
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "unsupported",
                    "code": "CHANNEL_MEDIA_UNSUPPORTED",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "channel": route.channel,
                },
                ensure_ascii=False,
            )
        target_staff_id = str(route.member.external_id or "").strip()
        if not target_staff_id:
            return json.dumps(
                {
                    "type": "media_delivery_result",
                    "version": 1,
                    "status": "unsupported",
                    "code": "RECIPIENT_MEDIA_ROUTE_UNAVAILABLE",
                    "media_kind": media_kind,
                    "intent_id": intent_id,
                    "channel": route.channel,
                },
                ensure_ascii=False,
            )
        session = await find_or_create_channel_session(
            db=db,
            agent_id=agent_id,
            user_id=route.user.id,
            external_conv_id=f"dingtalk_p2p_{target_staff_id}",
            source_channel="dingtalk",
            first_message_title=caption.strip()[:30] or file_path.name[:30],
        )
        await db.commit()
        target_session_id = str(session.id)

    return await _send_media_to_session(
        agent_id=agent_id,
        session_id=target_session_id,
        file_path=file_path,
        workspace_path=workspace_path,
        media_kind=media_kind,
        caption=caption,
        cover_path=cover_path,
        intent_id=intent_id,
        origin_session_id=origin_session_id,
        origin_turn_anchor_id=origin_turn_anchor_id,
        allow_download=allow_download,
        source_mode=source_mode,
        tool_args=tool_args,
    )






async def _send_file_to_session(
    agent_id: uuid.UUID,
    file_path: Path,
    session_id: str,
    message: str = "",
) -> tuple[str, IMDeliveryResult]:
    """Deliver a generic file through one exact Agent-owned IM Session."""
    try:
        target_session_id = uuid.UUID(session_id)
    except (TypeError, ValueError):
        error = RecipientResolutionError(
            "session_not_found_or_forbidden",
            "Target Session does not exist or is not accessible to this Agent",
        )
        return error.as_json(), IMDeliveryResult.failed("im", error.code)

    async with async_session() as db:
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == target_session_id,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            error = RecipientResolutionError(
                "session_not_found_or_forbidden",
                "Target Session does not exist or is not accessible to this Agent",
            )
            return error.as_json(), IMDeliveryResult.failed("im", error.code)

        channel = str(session.source_channel or "").strip()
        external_conv_id = str(session.external_conv_id or "").strip()
        if not external_conv_id or "__archived_" in external_conv_id:
            error = RecipientResolutionError(
                "session_route_unavailable",
                "Target Session has no active delivery route",
            )
            return error.as_json(), IMDeliveryResult.failed(channel or "im", error.code)
        config_channel = "microsoft_teams" if channel == "teams" else channel
        config = (
            await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == config_channel,
                    ChannelConfig.is_configured.is_(True),
                )
            )
        ).scalar_one_or_none()

    if not config:
        error = RecipientResolutionError(
            "channel_unconfigured",
            f"Source agent has no configured {channel} channel",
        )
        return error.as_json(), IMDeliveryResult.failed(channel or "im", error.code)

    if channel == "feishu":
        expected_prefix = "feishu_group_" if session.is_group else "feishu_p2p_"
        if not external_conv_id.startswith(expected_prefix):
            error = RecipientResolutionError(
                "session_route_mismatch",
                "Target Session route does not match its person/group type",
            )
            return error.as_json(), IMDeliveryResult.failed("feishu", error.code)
        receive_id = external_conv_id[len(expected_prefix) :].strip()
        if not receive_id:
            error = RecipientResolutionError(
                "session_route_unavailable", "Target Session has no active delivery route"
            )
            return error.as_json(), IMDeliveryResult.failed("feishu", error.code)
        receive_id_type = (
            "chat_id" if session.is_group else ("open_id" if receive_id.startswith("ou_") else "user_id")
        )
        from app.services.feishu_service import feishu_service
        delivery_parts: list[IMDeliveryPart] = []

        async def _record_feishu_result(
            artifact_role: str,
            provider_result: dict,
        ) -> None:
            provider_id = str(
                (provider_result.get("data") or {}).get("message_id")
                or provider_result.get("message_id")
                or ""
            ) or None
            part = IMDeliveryPart(
                transport="feishu_message",
                provider_message_id=provider_id,
                conversation_ref=receive_id,
                artifact_role=artifact_role,
                recallable=bool(provider_id),
            )
            delivery_parts.append(part)
            await record_channel_file_part(part)

        try:
            await feishu_service.upload_and_send_file(
                config.app_id,
                config.app_secret,
                receive_id,
                file_path,
                receive_id_type=receive_id_type,
                accompany_msg=message,
                on_result=_record_feishu_result,
            )
            if not delivery_parts:
                part = IMDeliveryPart(
                    transport="feishu_message",
                    conversation_ref=receive_id,
                    artifact_role="channel_file",
                    recallable=False,
                )
                delivery_parts.append(part)
                await record_channel_file_part(part)
            return (
                f"File '{file_path.name}' sent to Session {session.id} via Feishu.",
                IMDeliveryResult.sent("feishu", *delivery_parts),
            )
        except DeliveryReceiptPersistenceError:
            raise
        except Exception as exc:
            return (
                f"Failed to send file via Feishu: {exc}",
                IMDeliveryResult.from_exception("feishu", exc),
            )

    if channel == "slack":
        if not external_conv_id.startswith("slack_"):
            error = RecipientResolutionError(
                "session_route_mismatch", "Target Session route is not a Slack conversation"
            )
            return error.as_json(), IMDeliveryResult.failed("slack", error.code)
        slack_channel_id = external_conv_id[len("slack_") :].strip()
        if not slack_channel_id:
            error = RecipientResolutionError(
                "session_route_unavailable", "Target Session has no active delivery route"
            )
            return error.as_json(), IMDeliveryResult.failed("slack", error.code)
        return await _send_file_via_slack_channel(
            config,
            file_path,
            slack_channel_id,
            message,
            display_name=f"Session {session.id}",
        )

    if channel == "dingtalk":
        expected_prefix = "dingtalk_group_" if session.is_group else "dingtalk_p2p_"
        if not external_conv_id.startswith(expected_prefix):
            error = RecipientResolutionError(
                "session_route_mismatch",
                "Target Session route does not match its person/group type",
            )
            return error.as_json(), IMDeliveryResult.failed("dingtalk", error.code)
        target_id = external_conv_id[len(expected_prefix) :].strip()
        if not target_id:
            error = RecipientResolutionError(
                "session_route_unavailable", "Target Session has no active delivery route"
            )
            return error.as_json(), IMDeliveryResult.failed("dingtalk", error.code)
        from app.services.dingtalk_stream import (
            _send_dingtalk_media_message,
            _upload_dingtalk_media,
        )

        media_type = (
            "image"
            if file_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
            else "file"
        )
        media_id = await _upload_dingtalk_media(
            config.app_id, config.app_secret, str(file_path), media_type
        )
        if not media_id:
            return (
                "Failed to send file via DingTalk: media upload failed",
                IMDeliveryResult.failed("dingtalk", "media_upload_failed"),
            )
        delivery_parts: list[IMDeliveryPart] = []

        async def _record_dingtalk_result(provider_result: dict) -> None:
            process_key = str(provider_result.get("processQueryKey") or "") or None
            part = IMDeliveryPart(
                transport=(
                    "dingtalk_openapi_group"
                    if session.is_group
                    else "dingtalk_openapi_oto"
                ),
                provider_message_id=process_key,
                conversation_ref=target_id,
                artifact_role="channel_file",
                recallable=bool(process_key),
            )
            delivery_parts.append(part)
            await record_channel_file_part(part)

        sent = await _send_dingtalk_media_message(
            config.app_id,
            config.app_secret,
            target_id,
            media_id,
            media_type,
            "2" if session.is_group else "1",
            filename=file_path.name,
            raise_on_transport_error=True,
            on_result=_record_dingtalk_result,
        )
        if not sent:
            return (
                "Failed to send file via DingTalk: media send failed",
                IMDeliveryResult.failed("dingtalk", "media_send_failed"),
            )
        if not delivery_parts:
            part = IMDeliveryPart(
                transport=(
                    "dingtalk_openapi_group"
                    if session.is_group
                    else "dingtalk_openapi_oto"
                ),
                conversation_ref=target_id,
                artifact_role="channel_file",
                recallable=False,
            )
            delivery_parts.append(part)
            await record_channel_file_part(part)
        caption_error: str | None = None
        caption_uncertain = False
        if message:
            try:
                from app.services.turn_runtime import send_dingtalk_proactive_markdown

                result = await send_dingtalk_proactive_markdown(
                    app_id=config.app_id,
                    app_secret=config.app_secret,
                    target_id=target_id,
                    is_group=bool(session.is_group),
                    message=message,
                )
                if result.get("errcode") != 0:
                    caption_error = str(
                        result.get("errmsg")
                        or result.get("errcode")
                        or "caption_send_failed"
                    )
                    logger.warning(
                        "[send_channel_file] DingTalk caption delivery failed: {}",
                        result.get("errcode"),
                    )
                else:
                    process_key = str(result.get("processQueryKey") or "") or None
                    part = IMDeliveryPart(
                        transport=(
                            "dingtalk_openapi_group"
                            if session.is_group
                            else "dingtalk_openapi_oto"
                        ),
                        provider_message_id=process_key,
                        conversation_ref=target_id,
                        artifact_role="file_caption",
                        recallable=bool(process_key),
                    )
                    delivery_parts.append(part)
                    await record_channel_file_part(part)
            except DeliveryReceiptPersistenceError:
                raise
            except Exception as exc:
                caption_error = type(exc).__name__
                caption_uncertain = (
                    IMDeliveryResult.from_exception("dingtalk", exc).status == "unknown"
                )
                logger.warning("[send_channel_file] DingTalk caption delivery failed")
        if caption_error:
            caption_status = "unknown" if caption_uncertain else "partial"
            caption_outcome = (
                "its caption delivery is uncertain"
                if caption_uncertain
                else "its caption failed"
            )
            return (
                f"File '{file_path.name}' sent to Session {session.id} via DingTalk, "
                f"but {caption_outcome}.",
                IMDeliveryResult(
                    ok=False,
                    channel="dingtalk",
                    parts=tuple(delivery_parts),
                    status=caption_status,
                    error=f"caption_failed:{caption_error}",
                ),
            )
        return (
            f"File '{file_path.name}' sent to Session {session.id} via DingTalk.",
            IMDeliveryResult.sent("dingtalk", *delivery_parts),
        )

    error = RecipientResolutionError(
        "file_route_unsupported",
        f"File delivery is not implemented for {channel}",
        available_channels=[channel] if channel else [],
    )
    return error.as_json(), IMDeliveryResult.failed(channel or "im", error.code)


async def _send_file_to_recipient(
    agent_id: uuid.UUID,
    file_path: Path,
    user_id: str,
    message: str = "",
    *,
    channel: str | None = None,
) -> tuple[str, IMDeliveryResult]:
    """Resolve one canonical recipient route and send without name lookup."""
    async with async_session() as db:
        try:
            route = await resolve_human_channel_recipient(db, agent_id, user_id, channel=channel)
        except RecipientResolutionError as exc:
            return exc.as_json(), IMDeliveryResult.failed(channel or "im", exc.code)
        config_result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == ("microsoft_teams" if route.channel == "teams" else route.channel),
                ChannelConfig.is_configured.is_(True),
            )
        )
        config = config_result.scalar_one_or_none()
    if not config:
        error = RecipientResolutionError(
            "channel_unconfigured",
            f"Source agent has no configured {route.channel} channel",
        )
        return error.as_json(), IMDeliveryResult.failed(route.channel, error.code)
    if route.channel == "feishu":
        return await _send_file_via_feishu(agent_id, config, file_path, route.member, route.user.display_name, message)
    if route.channel == "slack":
        return await _send_file_via_slack(
            config, file_path, route.member, route.user.display_name, message
        )
    error = RecipientResolutionError(
        "file_route_unsupported",
        f"File delivery is not implemented for {route.channel}",
        available_channels=[route.channel],
    )
    return error.as_json(), IMDeliveryResult.failed(route.channel, error.code)


async def _send_file_via_feishu(
    agent_id, config, file_path: Path, member: OrgMember, display_name: str, message: str
) -> tuple[str, IMDeliveryResult]:
    """Send a file to an already-authorized internal Feishu endpoint."""
    receive_id = (member.external_id or member.open_id or "").strip()
    id_type = "user_id" if member.external_id else "open_id"
    if not receive_id:
        error = RecipientResolutionError(
            "recipient_unreachable", "Canonical user has no usable Feishu endpoint"
        )
        return error.as_json(), IMDeliveryResult.failed("feishu", error.code)
    from app.services.feishu_service import feishu_service
    delivery_parts: list[IMDeliveryPart] = []

    async def _record_result(artifact_role: str, provider_result: dict) -> None:
        provider_id = str(
            (provider_result.get("data") or {}).get("message_id")
            or provider_result.get("message_id")
            or ""
        ) or None
        part = IMDeliveryPart(
            transport="feishu_message",
            provider_message_id=provider_id,
            conversation_ref=receive_id,
            artifact_role=artifact_role,
            recallable=bool(provider_id),
        )
        delivery_parts.append(part)
        await record_channel_file_part(part)
    try:
        await feishu_service.upload_and_send_file(
            config.app_id,
            config.app_secret,
            receive_id,
            file_path,
            receive_id_type=id_type,
            accompany_msg=message,
            on_result=_record_result,
        )
        if not delivery_parts:
            part = IMDeliveryPart(
                transport="feishu_message",
                conversation_ref=receive_id,
                artifact_role="channel_file",
                recallable=False,
            )
            delivery_parts.append(part)
            await record_channel_file_part(part)
        return (
            f"File '{file_path.name}' sent to {display_name} via Feishu.",
            IMDeliveryResult.sent("feishu", *delivery_parts),
        )
    except DeliveryReceiptPersistenceError:
        raise
    except Exception as e:
        # If upload fails, try sending a download link as fallback
        import json as _j
        from app.config import get_settings as _gs

        _s = _gs()
        safe_upload_error = sanitize_user_visible_text(str(e)).strip()[:200] or type(e).__name__
        base_url = getattr(_s, 'BASE_URL', '').rstrip('/') or ''
        base_abs = _agent_workspace_root(agent_id).resolve()
        try:
            _rel = str(file_path.resolve().relative_to(base_abs))
        except ValueError:
            _rel = file_path.name
        parts = []
        if message:
            parts.append(message)
        if base_url:
            dl_url = f"{base_url}/api/agents/{agent_id}/files/download?path={_rel}"
            parts.append(f"{file_path.name}\n{dl_url}")
        parts.append(
            f"File upload failed ({safe_upload_error}). If you need direct file sending, "
            "enable im:resource permission in Feishu."
        )
        try:
            fallback_result = await feishu_service.send_message(
                config.app_id, config.app_secret,
                receive_id, "text",
                _j.dumps({"text": "\n\n".join(parts)}, ensure_ascii=False),
                receive_id_type=id_type,
            )
            provider_id = str(
                (fallback_result.get("data") or {}).get("message_id")
                or fallback_result.get("message_id")
                or ""
            ) or None
            part = IMDeliveryPart(
                transport="feishu_message",
                provider_message_id=provider_id,
                conversation_ref=receive_id,
                artifact_role="file_fallback",
                recallable=bool(provider_id),
            )
            await record_channel_file_part(part)
            return (
                f"File upload to Feishu failed, sent download link to {display_name} instead.",
                IMDeliveryResult.sent("feishu", part),
            )
        except Exception as fallback_exc:
            return (
                f"Failed to send file to {display_name} via Feishu: {e}",
                IMDeliveryResult.from_exception("feishu", fallback_exc),
            )


async def _send_file_via_slack(
    config, file_path: Path, member: OrgMember, display_name: str, message: str
) -> tuple[str, IMDeliveryResult]:
    """Send file to an already-authorized internal Slack endpoint."""
    import httpx

    bot_token = config.app_secret or ""
    if not bot_token:
        error = RecipientResolutionError(
            "channel_unconfigured", "Slack bot token is missing"
        )
        return error.as_json(), IMDeliveryResult.failed("slack", error.code)
    slack_user_id = (member.external_id or "").strip()
    if not slack_user_id:
        error = RecipientResolutionError(
            "recipient_unreachable", "Canonical user has no usable Slack endpoint"
        )
        return error.as_json(), IMDeliveryResult.failed("slack", error.code)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Open a DM channel
            dm_resp = await client.post(
                "https://slack.com/api/conversations.open",
                headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                json={"users": slack_user_id},
            )
            dm_data = dm_resp.json()
            if not dm_data.get("ok"):
                error = str(dm_data.get("error") or "conversations_open_failed")
                return f"Slack conversations.open failed: {error}", IMDeliveryResult.failed("slack", error)
            channel_id = dm_data["channel"]["id"]

        return await _send_file_via_slack_channel(
            config,
            file_path,
            channel_id,
            message,
            display_name=display_name,
        )
    except Exception as e:
        return (
            f"Failed to send file via Slack: {e}",
            IMDeliveryResult.from_exception("slack", e),
        )


async def _send_file_via_slack_channel(
    config,
    file_path: Path,
    channel_id: str,
    message: str,
    *,
    display_name: str,
) -> tuple[str, IMDeliveryResult]:
    """Upload a generic file to one already-resolved Slack conversation."""
    import httpx

    bot_token = config.app_secret or ""
    if not bot_token:
        error = RecipientResolutionError(
            "channel_unconfigured", "Slack bot token is missing"
        )
        return error.as_json(), IMDeliveryResult.failed("slack", error.code)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            upload_url_resp = await client.post(
                "https://slack.com/api/files.getUploadURLExternal",
                headers={"Authorization": f"Bearer {bot_token}"},
                data={"filename": file_path.name, "length": str(file_path.stat().st_size)},
            )
            upload_data = upload_url_resp.json()
            if not upload_data.get("ok"):
                error = str(upload_data.get("error") or "file_upload_failed")
                return f"Slack file upload failed: {error}", IMDeliveryResult.failed("slack", error)
            await client.post(
                upload_data["upload_url"],
                content=file_path.read_bytes(),
                headers={"Content-Type": "application/octet-stream"},
            )
            complete = await client.post(
                "https://slack.com/api/files.completeUploadExternal",
                headers={"Authorization": f"Bearer {bot_token}"},
                json={
                    "files": [{"id": upload_data["file_id"]}],
                    "channel_id": channel_id,
                    "initial_comment": message or "",
                },
            )
            if not complete.json().get("ok"):
                error = str(complete.json().get("error") or "file_upload_complete_failed")
                return f"Slack file upload complete failed: {error}", IMDeliveryResult.failed("slack", error)
            part = IMDeliveryPart(
                transport="slack_file",
                provider_message_id=str(upload_data["file_id"]),
                conversation_ref=channel_id,
                artifact_role="channel_file",
                recallable=False,
            )
            await record_channel_file_part(part)
            return (
                f"File '{file_path.name}' sent to {display_name} via Slack.",
                IMDeliveryResult.sent("slack", part),
            )
    except Exception as e:
        return f"Failed to send file via Slack: {e}", IMDeliveryResult.from_exception("slack", e)







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
async def _send_exact_session_message(
    agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
    require_group: bool = False,
    legacy_group_contract: bool = False,
) -> str:
    """Authorize and send text through one exact human ChatSession route."""
    raw_session_id = str(args.get("session_id") or "").strip()
    message_text = sanitize_user_visible_text(
        str(args.get("message") or "")
    ).strip()
    raw_mention_user_ids = args.get("mention_user_ids")
    if "mention_all" in args:
        raw_mention_all = args["mention_all"]
        if type(raw_mention_all) is not bool:
            return "❌ mention_all must be a boolean"
        mention_all = raw_mention_all
    else:
        mention_all = False
    if not raw_session_id:
        qualifier = "group " if require_group else ""
        return f"❌ Please provide the exact {qualifier}session_id"
    if not message_text:
        return "❌ Please provide message content"
    if raw_mention_user_ids is None:
        mention_user_ids: list[str] = []
    elif not isinstance(raw_mention_user_ids, list):
        return "❌ mention_user_ids must be an array of canonical platform user_ids"
    else:
        mention_user_ids = list(dict.fromkeys(str(value or "").strip() for value in raw_mention_user_ids))
        if not all(mention_user_ids):
            return "❌ mention_user_ids cannot contain empty values"
        if len(mention_user_ids) > 20:
            return "❌ mention_user_ids supports at most 20 people"
    if mention_all and mention_user_ids:
        return "❌ mention_all=true cannot be combined with mention_user_ids"
    try:
        target_session_id = uuid.UUID(raw_session_id)
    except (TypeError, ValueError):
        return _GROUP_SESSION_DENIAL if require_group else _SESSION_MESSAGE_DENIAL

    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=tool_call_id,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    target_name = "group" if require_group else "conversation"
    target_channel = ""
    target_is_group = require_group
    mentioned_names: list[str] = []
    mention_intent: MentionIntent | None = None
    mentions_meta: dict | None = None

    try:
        async with async_session() as db:
            await _lock_outbound_operation(db, operation_key)
            if operation_key:
                existing = (
                    await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == operation_key))
                ).scalar_one_or_none()
                if existing is not None:
                    meta = existing.message_meta if isinstance(existing.message_meta, dict) else {}
                    replay_mentions = meta.get("mentions")
                    if not isinstance(replay_mentions, dict):
                        replay_mentions = None
                    return _session_message_result(
                        status=_session_receipt_replay_status(existing),
                        session_id=str(existing.conversation_id),
                        channel=str(meta.get("source_channel") or ""),
                        target_name=str(meta.get("target_name") or target_name),
                        is_group=bool(meta.get("target_is_group", require_group)),
                        legacy_group_contract=legacy_group_contract,
                        mentioned_users=list(meta.get("mentioned_users") or []),
                        mentions=replay_mentions,
                        message_id=str(existing.id),
                    )

            conditions = [
                ChatSession.id == target_session_id,
                ChatSession.agent_id == agent_id,
            ]
            if require_group:
                conditions.append(ChatSession.is_group.is_(True))
            session = (await db.execute(select(ChatSession).where(*conditions).with_for_update())).scalar_one_or_none()
            if session is None:
                return _GROUP_SESSION_DENIAL if require_group else _SESSION_MESSAGE_DENIAL

            target_channel = str(session.source_channel or "").strip()
            external_conv_id = str(session.external_conv_id or "").strip()
            target_is_group = bool(session.is_group)
            target_kind = "group" if target_is_group else "person"
            if target_channel not in _PLATFORM_SESSION_CHANNELS and (
                "__archived_" in external_conv_id or not external_conv_id
            ):
                qualifier = "群" if target_is_group else ""
                return f"❌ 无法投递：该{qualifier}会话已归档或通道绑定已失效。"
            if legacy_group_contract and target_channel not in _LEGACY_GROUP_SESSION_CHANNELS:
                return f"❌ Group Session delivery is not supported for channel: {target_channel or 'unknown'}"
            capabilities = _SESSION_MESSAGE_CAPABILITIES.get(target_channel, frozenset())
            if target_kind not in capabilities:
                label = "Group Session" if target_is_group else "Session"
                return f"❌ {label} delivery is not supported for channel: {target_channel or 'unknown'}"

            if mention_all or mention_user_ids:
                if not target_is_group or target_channel != "dingtalk":
                    return "❌ 原生 @ 当前仅支持钉钉群 Session。"
                if mention_all:
                    mention_intent = MentionIntent(scope="all")
                    mentions_meta = {"scope": "all"}
                else:
                    try:
                        target_ids, mentioned_names = await prepare_group_user_mentions(
                            db,
                            agent_id=agent_id,
                            canonical_user_ids=mention_user_ids,
                        )
                    except RecipientResolutionError as exc:
                        return exc.as_json()
                    except ValueError as exc:
                        if str(exc) == "dingtalk_staff_id_unavailable":
                            return "❌ 指定用户缺少可用的钉钉 userId，无法 @。"
                        raise
                    mention_intent = MentionIntent(
                        scope="users",
                        target_ids=tuple(target_ids),
                        target_names=tuple(mentioned_names),
                    )
                    mentions_meta = {
                        "scope": "users",
                        "user_ids": mention_user_ids,
                        "display_names": mentioned_names,
                    }

            if target_is_group:
                target_name = str(session.group_name or session.title or "group")
                target_user_id = None
            else:
                if session.user_id is None:
                    return _SESSION_MESSAGE_DENIAL
                try:
                    recipient = await resolve_human_recipient(db, agent_id, session.user_id)
                except RecipientResolutionError as exc:
                    return exc.as_json()
                target_user_id = recipient.user.id
                target_name = str(recipient.user.display_name or session.title or "person")

            runtime = TurnRuntime(
                session_found=True,
                source_channel=target_channel,
                conversation_id=str(session.id),
                external_conv_id=external_conv_id or None,
                is_group=target_is_group,
            )
            if mention_all:
                message_for_history = f"{message_text}\n\n@所有人"
            elif mentioned_names:
                message_for_history = (
                    f"{message_text}\n\n"
                    f"{' '.join(f'@{name}' for name in mentioned_names)}"
                )
            else:
                message_for_history = message_text
            message_for_delivery = message_text if mention_intent is not None else message_for_history
            delivery_kwargs = {
                "agent_id": agent_id,
                "runtime": runtime,
                "message": message_for_delivery,
                "allow_wecom_group_actor_fallback": False,
            }
            if mention_intent is not None:
                delivery_kwargs["mention"] = mention_intent
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=target_user_id,
                session=session,
                content=message_for_history,
                source_channel=target_channel,
                actor_ref=external_conv_id or str(target_user_id or ""),
                target_name=target_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending(target_channel),
            )
            if should_deliver:
                receipt_meta = dict(receipt.message_meta or {})
                receipt_meta["target_is_group"] = target_is_group
                if mentions_meta:
                    receipt_meta["mentions"] = mentions_meta
                if mentioned_names:
                    receipt_meta["mention_user_ids"] = mention_user_ids
                    receipt_meta["mentioned_users"] = mentioned_names
                receipt.message_meta = receipt_meta
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                replay_meta = receipt.message_meta if isinstance(receipt.message_meta, dict) else {}
                replay_mentions = replay_meta.get("mentions")
                if not isinstance(replay_mentions, dict):
                    replay_mentions = None
                return _session_message_result(
                    status=_session_receipt_replay_status(receipt),
                    session_id=str(receipt.conversation_id),
                    channel=target_channel,
                    target_name=target_name,
                    is_group=target_is_group,
                    legacy_group_contract=legacy_group_contract,
                    mentioned_users=mentioned_names,
                    mentions=replay_mentions,
                    message_id=str(receipt.id),
                )

        # The provider call runs outside the row/advisory lock. The pending
        # receipt above is the durable idempotency claim and crash marker.
        async def _record_part(part: IMDeliveryPart) -> None:
            await append_delivery_part(receipt_id, part)

        delivery_kwargs["on_part"] = _record_part
        try:
            delivery_result = await deliver_message_with_receipt(**delivery_kwargs)
        except Exception as exc:
            await register_delivery(
                receipt_id,
                IMDeliveryResult.from_exception(target_channel, exc),
            )
            raise
        await register_delivery(receipt_id, delivery_result)
        if not delivery_result.ok:
            label = "Group message" if target_is_group else "Session message"
            return f"❌ {label} delivery failed via {target_channel}; receipt status is failed."

        if target_channel not in _PLATFORM_SESSION_CHANNELS:
            try:
                from app.api.websocket import manager as ws_manager

                await ws_manager.send_to_session(
                    str(agent_id),
                    str(target_session_id),
                    {
                        "type": "assistant_message_committed",
                        "id": str(receipt_id),
                        "role": "assistant",
                        "content": message_for_history,
                        "session_id": str(target_session_id),
                    },
                )
            except Exception:
                logger.opt(exception=True).warning("[SessionMessage] Web live mirror failed after external delivery")

        return _session_message_result(
            status="sent",
            session_id=str(target_session_id),
            channel=target_channel,
            target_name=target_name,
            is_group=target_is_group,
            legacy_group_contract=legacy_group_contract,
            mentioned_users=mentioned_names,
            mentions=mentions_meta,
            message_id=str(receipt_id),
        )
    except Exception as exc:
        logger.opt(exception=True).error(
            "[SessionMessage] delivery failed agent={} session={}",
            agent_id,
            target_session_id,
        )
        label = "Group Session" if require_group else "Session"
        return f"❌ {label} message error: {type(exc).__name__}: {str(exc)[:200]}"


async def _send_session_message(
    agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send text through an exact person or group Session route."""
    return await _send_exact_session_message(
        agent_id,
        args,
        origin_session_id=origin_session_id,
        tool_call_id=tool_call_id,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )


async def _send_group_session_message(
    agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Backward-compatible group-only wrapper around exact Session delivery."""
    return await _send_exact_session_message(
        agent_id,
        args,
        origin_session_id=origin_session_id,
        tool_call_id=tool_call_id,
        origin_turn_anchor_id=origin_turn_anchor_id,
        require_group=True,
        legacy_group_contract=True,
    )


async def _send_channel_message(
    agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send an external-channel message by canonical platform user_id."""
    canonical_user_id = str(args.get("user_id") or "").strip()
    message_text = sanitize_user_visible_text(
        str(args.get("message") or "")
    ).strip()
    raw_target_channel = (args.get("channel") or "").strip().lower()
    target_channel = "teams" if raw_target_channel == "microsoft_teams" else raw_target_channel

    if not canonical_user_id:
        return "❌ Please provide canonical user_id"
    if not message_text:
        return "❌ Please provide message content"

    try:
        async with async_session() as db:
            try:
                route = await resolve_human_channel_recipient(
                    db,
                    agent_id,
                    canonical_user_id,
                    channel=target_channel or None,
                )
            except RecipientResolutionError as exc:
                return exc.as_json()
            target_member = route.member
            provider_type = route.channel
            member_name = route.user.display_name
            logger.info(
                "[ChannelMessage] canonical user %s via %s",
                canonical_user_id,
                provider_type,
            )

            if provider_type == "feishu":
                return await _send_feishu_message(
                    agent_id,
                    {
                        "user_id": canonical_user_id,
                        "message": message_text,
                    },
                    origin_session_id=origin_session_id,
                    origin_user_id=origin_user_id,
                    tool_call_id=tool_call_id,
                    origin_turn_anchor_id=origin_turn_anchor_id,
                )
            elif provider_type == "dingtalk":
                return await _send_dingtalk_message(
                    agent_id,
                    member_name,
                    message_text,
                    target_member,
                    origin_session_id=origin_session_id,
                    origin_user_id=origin_user_id,
                    tool_call_id=tool_call_id,
                    origin_turn_anchor_id=origin_turn_anchor_id,
                )
            elif provider_type == "wecom":
                return await _send_wecom_message(
                    agent_id,
                    member_name,
                    message_text,
                    target_member,
                    origin_session_id=origin_session_id,
                    origin_user_id=origin_user_id,
                    tool_call_id=tool_call_id,
                    origin_turn_anchor_id=origin_turn_anchor_id,
                )
            elif provider_type == "slack":
                return await _send_slack_message(
                    agent_id,
                    member_name,
                    message_text,
                    target_member,
                    origin_session_id=origin_session_id,
                    origin_user_id=origin_user_id,
                    tool_call_id=tool_call_id,
                    origin_turn_anchor_id=origin_turn_anchor_id,
                )
            elif provider_type == "teams":
                return await _send_teams_channel_message(
                    agent_id,
                    member_name,
                    message_text,
                    target_member,
                    origin_session_id=origin_session_id,
                    origin_user_id=origin_user_id,
                    tool_call_id=tool_call_id,
                    origin_turn_anchor_id=origin_turn_anchor_id,
                )
            elif provider_type == "wechat":
                return await _send_wechat_channel_message(
                    agent_id,
                    member_name,
                    message_text,
                    target_member,
                    origin_session_id=origin_session_id,
                    origin_user_id=origin_user_id,
                    tool_call_id=tool_call_id,
                    origin_turn_anchor_id=origin_turn_anchor_id,
                )
            else:
                return f"❌ Unsupported channel type: {provider_type}"

    except Exception as e:
        logger.exception("[ChannelMessage] Error")
        return f"❌ Channel message error: {str(e)[:200]}"




























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


async def _discover_resources(agent_id: uuid.UUID, arguments: dict) -> str:
    """Search Smithery registry for MCP servers."""
    query = arguments.get("query", "")
    if not query:
        return "❌ Please provide a search query describing the capability you need."
    max_results = min(arguments.get("max_results", 5), 10)

    from app.services.resource_discovery import search_smithery

    return await search_smithery(query, max_results, agent_id=agent_id)


async def _import_mcp_server(agent_id: uuid.UUID, arguments: dict) -> str:
    """Import an MCP server.

    Direct import (no third-party account required) is the primary path; the
    Smithery registry path is only used when the caller explicitly passes a
    `server_id` that resolves on Smithery.

    Accepted shapes (any of):
      • mcp_url   = "https://..."                                     bare URL
      • mcp_config = {"url": "...", "headers": {...}}                 single-server spec
      • mcp_config = {"mcpServers": {"<name>": {...}}}                standard MCP config
      • mcp_config = "<JSON-stringified version of any of the above>"
      • config = "<same as mcp_config>"                               legacy field name
      • server_id = "@anthropic/brave-search"                         Smithery (advanced)
    """
    import json as _json
    from app.services.mcp_config_parser import parse_mcp_input

    reauthorize = bool(arguments.get("reauthorize", False))

    # Sniff every plausible direct-import field
    parsed = None
    for field in ("mcp_url", "mcp_config", "config", "url"):
        candidate = arguments.get(field)
        parsed = parse_mcp_input(candidate)
        if parsed:
            break

    if parsed:
        if parsed.get("error") and not parsed.get("url"):
            return f"❌ {parsed['error']}"
        if parsed.get("transport") == "stdio":  # stdio self-install via aio-sandbox hub
            from app.services.resource_discovery import import_mcp_stdio_direct

            return await import_mcp_stdio_direct(agent_id, parsed)
        if parsed.get("url"):
            from app.services.resource_discovery import import_mcp_direct

            server_name = arguments.get("server_name") or parsed.get("name") or arguments.get("server_id")
            api_key = arguments.get("api_key") or parsed.get("api_key")
            headers = parsed.get("headers")
            warning = parsed.get("_warning")
            result = await import_mcp_direct(
                mcp_url=parsed["url"],
                agent_id=agent_id,
                server_name=server_name,
                api_key=api_key,
                headers=headers,
            )
            if warning:
                result = f"ℹ️ {warning}\n\n{result}"
            return result

    # Smithery path — opt-in, only when caller explicitly provided server_id
    server_id = (arguments.get("server_id") or "").strip()
    if server_id:
        # Legacy callers may still pass `config` as JSON string; normalize before forwarding.
        smithery_config = arguments.get("config")
        if isinstance(smithery_config, str):
            try:
                smithery_config = _json.loads(smithery_config) if smithery_config else None
            except _json.JSONDecodeError:
                smithery_config = None
        if smithery_config is not None and not isinstance(smithery_config, dict):
            smithery_config = None
        from app.services.resource_discovery import import_mcp_from_smithery

        return await import_mcp_from_smithery(server_id, agent_id, smithery_config, reauthorize=reauthorize)

    return (
        "❌ Provide one of:\n"
        "• `mcp_url`: full http/https endpoint of the MCP server, e.g. "
        "`https://mcp-gw.dingtalk.com/server/<id>?key=<token>`\n"
        "• `mcp_config`: standard `mcpServers` JSON config (object or JSON string)\n"
        "• `server_id`: Smithery registry ID (advanced — only if you want to discover via the Smithery registry)"
    )


async def _handle_cancel_trigger(
    agent_id: uuid.UUID,
    arguments: dict,
    *,
    user_id: uuid.UUID | None = None,
) -> str:
    """Cancel (disable) a trigger by name."""
    from app.models.trigger import AgentTrigger

    name = arguments.get("name", "").strip()
    if not name:
        return "❌ Missing required argument 'name'"

    try:
        async with async_session() as db:
            result = await db.execute(
                select(AgentTrigger).where(
                    AgentTrigger.agent_id == agent_id,
                    AgentTrigger.name == name,
                )
            )
            trigger = result.scalar_one_or_none()
            if not trigger:
                return f"❌ Trigger '{name}' not found"
            if not trigger.is_enabled:
                return f"ℹ️ Trigger '{name}' is already disabled"

            if user_id is not None:
                from app.services.execution_identity import align_background_execution_user

                await align_background_execution_user(
                    db,
                    agent_id=agent_id,
                    resource_type="trigger",
                    resource_id=trigger.id,
                    execution_user_id=user_id,
                )
            trigger.is_enabled = False
            await db.commit()

        try:
            from app.services.audit_logger import write_audit_log

            await write_audit_log("trigger_cancelled", {"name": name}, agent_id=agent_id)
        except Exception:
            pass

        return f"✅ Trigger '{name}' cancelled. It will no longer fire."

    except Exception as e:
        return f"❌ Failed to cancel trigger: {e}"


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
