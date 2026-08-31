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
from app.services import agent_tools_code_runtime as _agent_tools_code_runtime_module
from app.services import agent_tools_image_ops as _agent_tools_image_ops_module
from app.services import agent_tools_mcp_runtime as _agent_tools_mcp_runtime_module
from app.services import agent_tools_media_delivery_support as _agent_tools_media_delivery_support_module
from app.services import agent_tools_outbound_core as _agent_tools_outbound_core_module
from app.services import agent_tools_sandbox_web_ops as _agent_tools_sandbox_web_ops_module
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
channel_file_sender: ContextVar = ContextVar('channel_file_sender', default=None)
channel_file_part_recorder: ContextVar = ContextVar(
    "channel_file_part_recorder", default=None
)
channel_audio_sender: ContextVar = ContextVar('channel_audio_sender', default=None)
channel_video_sender: ContextVar = ContextVar('channel_video_sender', default=None)
# For web chat: agent_id needed to build download URL
channel_web_agent_id: ContextVar = ContextVar("channel_web_agent_id", default=None)
# Set by Feishu channel handler — open_id of the message sender so calendar tool
# can auto-invite them as attendee when no explicit attendee list is given
channel_feishu_sender_open_id: ContextVar = ContextVar("channel_feishu_sender_open_id", default=None)
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










async def _claim_channel_file_receipt(
    *,
    agent_id: uuid.UUID,
    tool_call_id: str,
    origin_session_id: str,
    origin_turn_anchor_id: uuid.UUID | None,
) -> uuid.UUID | None:
    """Put the existing tool-call row in pending before any channel side effect."""
    if not tool_call_id or not origin_session_id:
        return None
    operation_key = _build_outbound_operation_key(
        agent_id=agent_id,
        origin_session_id=origin_session_id,
        tool_call_id=tool_call_id,
        origin_turn_anchor_id=origin_turn_anchor_id,
    )
    if not operation_key:
        return None
    async with async_session() as db:
        existing = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.external_event_key == operation_key)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None
        candidates = (
            await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.agent_id == agent_id,
                    ChatMessage.conversation_id == str(origin_session_id),
                    ChatMessage.role == "tool_call",
                    ChatMessage.external_event_key.is_(None),
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(20)
                .with_for_update()
            )
        ).scalars().all()
        for candidate in candidates:
            try:
                payload = json.loads(candidate.content or "")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if (
                isinstance(payload, dict)
                and str(payload.get("name") or "") == "send_channel_file"
                and str(payload.get("call_id") or "") == tool_call_id
            ):
                current_meta = (
                    candidate.message_meta
                    if isinstance(candidate.message_meta, dict)
                    else {}
                )
                current_delivery = (
                    current_meta.get("delivery")
                    if isinstance(current_meta.get("delivery"), dict)
                    else {}
                )
                candidate.external_event_key = operation_key
                if str(payload.get("status") or "") != "running" or str(
                    current_delivery.get("status")
                    or current_meta.get("delivery_status")
                    or ""
                ) not in {"", "pending"}:
                    await db.commit()
                    return None
                candidate.message_meta = attach_delivery_to_meta(
                    {
                        **dict(current_meta),
                        "direction": "outbound",
                        "artifact_role": "channel_file",
                        "tool_call_id": tool_call_id,
                        "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
                    },
                    IMDeliveryResult.pending("im"),
                )
                await db.commit()
                return candidate.id
    return None


async def record_channel_file_part(part: IMDeliveryPart) -> None:
    """Persist one observed file artifact through the active tool receipt."""
    recorder = channel_file_part_recorder.get()
    if recorder is not None:
        try:
            await recorder(part)
        except DeliveryReceiptPersistenceError:
            raise
        except Exception as exc:
            raise DeliveryReceiptPersistenceError(
                "provider artifact receipt persistence failed"
            ) from exc


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


async def _supports_exact_file_session_route(
    agent_id: uuid.UUID,
    session_id: str,
) -> bool:
    try:
        target_session_id = uuid.UUID(session_id)
    except (TypeError, ValueError):
        return False
    async with async_session() as db:
        channel = (
            await db.execute(
                select(ChatSession.source_channel).where(
                    ChatSession.id == target_session_id,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
    return str(channel or "").strip() in {"dingtalk", "feishu", "slack"}


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


async def _preflight_managed_media_target(
    *,
    agent_id: uuid.UUID,
    session_id: str,
    user_id: str,
    channel: str | None,
    media_kind: str,
    intent_id: str,
) -> dict | None:
    """Reject unusable destinations before a managed URL consumes bandwidth."""
    base = {
        "type": "media_delivery_result",
        "version": 1,
        "media_kind": media_kind,
        "intent_id": intent_id,
    }
    async with async_session() as db:
        if user_id:
            try:
                route = await resolve_human_channel_recipient(
                    db,
                    agent_id,
                    user_id,
                    channel=channel,
                )
            except RecipientResolutionError as exc:
                return {
                    **base,
                    "status": "unsupported",
                    "code": exc.code,
                    "message": exc.message,
                    "available_channels": exc.available_channels,
                }
            if route.channel != "dingtalk":
                return {
                    **base,
                    "status": "unsupported",
                    "code": "CHANNEL_MEDIA_UNSUPPORTED",
                    "channel": route.channel,
                }
            if not str(route.member.external_id or "").strip():
                return {
                    **base,
                    "status": "unsupported",
                    "code": "RECIPIENT_MEDIA_ROUTE_UNAVAILABLE",
                    "channel": route.channel,
                }
            if not await _has_configured_dingtalk_media_channel(db, agent_id):
                return {
                    **base,
                    "status": "unsupported",
                    "code": "CHANNEL_MEDIA_UNSUPPORTED",
                    "channel": route.channel,
                }
            return None

        try:
            target_session_id = uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            return {
                **base,
                "status": "failed",
                "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
            }
        session = (
            await db.execute(
                select(ChatSession).where(
                    ChatSession.id == target_session_id,
                    ChatSession.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if session is None:
            return {
                **base,
                "status": "failed",
                "code": "SESSION_NOT_FOUND_OR_FORBIDDEN",
            }
        resolved_channel = str(session.source_channel or "").strip()
        session_fields = {"session_id": str(session.id), "channel": resolved_channel}
        if resolved_channel in _PLATFORM_SESSION_CHANNELS:
            return None
        if resolved_channel != "dingtalk":
            return {
                **base,
                **session_fields,
                "status": "unsupported",
                "code": "CHANNEL_MEDIA_UNSUPPORTED",
            }
        external_conv_id = str(session.external_conv_id or "").strip()
        if not external_conv_id or "__archived_" in external_conv_id:
            return {
                **base,
                **session_fields,
                "status": "failed",
                "code": "SESSION_ROUTE_UNAVAILABLE",
            }
        expected_prefix = "dingtalk_group_" if session.is_group else "dingtalk_p2p_"
        wrong_prefix = "dingtalk_p2p_" if session.is_group else "dingtalk_group_"
        if not external_conv_id.startswith(expected_prefix) or external_conv_id.startswith(wrong_prefix):
            return {
                **base,
                **session_fields,
                "status": "failed",
                "code": "SESSION_ROUTE_MISMATCH",
            }
        if not await _has_configured_dingtalk_media_channel(db, agent_id):
            return {
                **base,
                **session_fields,
                "status": "unsupported",
                "code": "CHANNEL_MEDIA_UNSUPPORTED",
            }
    return None


async def _has_configured_dingtalk_media_channel(db, agent_id: uuid.UUID) -> bool:
    config = (
        await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "dingtalk",
                ChannelConfig.is_configured.is_(True),
            )
        )
    ).scalar_one_or_none()
    return bool(config and config.app_id and config.app_secret)


_MEDIA_DELIVERY_MESSAGES = {
    "INVALID_MEDIA_TYPE": "媒体类型必须是 audio 或 video。",
    "COVER_NOT_ALLOWED_FOR_AUDIO": "音频不支持封面参数。",
    "INVALID_MEDIA_SOURCE": "必须且只能提供一个媒体来源：file_path，或 url 与 url_mode。",
    "INVALID_URL_MODE": "使用 url 时，url_mode 必须是 external 或 managed。",
    "INVALID_MEDIA_HEADERS": ("headers 只支持 managed URL，且请求头名称和值必须是合法的 HTTP 字符串。"),
    "INVALID_MEDIA_URL": "媒体 URL 无效；external 仅支持 HTTPS，managed 支持 HTTP(S)。",
    "MEDIA_URL_FORBIDDEN_TARGET": "媒体 URL 指向内网、本机或其他受保护地址，平台拒绝访问。",
    "MEDIA_URL_DNS_FAILED": "媒体 URL 的域名无法解析。",
    "MEDIA_URL_TOO_MANY_REDIRECTS": "媒体 URL 重定向次数过多。",
    "MEDIA_URL_HTTP_ERROR": "下载第三方媒体时远端返回了错误状态。",
    "MEDIA_URL_TOO_LARGE": "第三方媒体超过平台允许的大小。",
    "MEDIA_URL_EMPTY": "第三方媒体内容为空。",
    "MEDIA_URL_FETCH_TIMEOUT": "下载第三方媒体超时。",
    "MEDIA_URL_FETCH_FAILED": "下载第三方媒体失败。",
    "MEDIA_STORAGE_FAILED": "第三方媒体写入 Agent 工作区失败。",
    "EXTERNAL_URL_NOT_SUPPORTED_BY_CHANNEL": "目标通道不支持直接发送第三方媒体 URL；可改用 managed 托管模式。",
    "INVALID_FILE_PATH": "媒体文件路径无效，必须使用当前 Agent 工作区内的相对路径。",
    "MEDIA_NOT_FOUND": "工作区中找不到要发送的媒体文件。",
    "MEDIA_KIND_MISMATCH": "声明的媒体类型与文件实际内容不一致。",
    "INVALID_COVER_PATH": "视频封面路径无效。",
    "INVALID_VIDEO_COVER": "视频封面不存在或不是支持的图片格式。",
    "AMBIGUOUS_MEDIA_TARGET": "不能同时指定 session_id 和 user_id。",
    "CHANNEL_REQUIRES_USER_TARGET": "channel 只能与 user_id 一起使用。",
    "SESSION_REQUIRED": "未找到当前会话；请指定有效的 session_id 或 user_id。",
    "MISSING_DELIVERY_INTENT_ID": "缺少稳定的工具调用 ID，平台未执行发送。",
    "SESSION_NOT_FOUND_OR_FORBIDDEN": "目标 Session 不存在或当前 Agent 无权访问。",
    "SESSION_ROUTE_UNAVAILABLE": "目标 Session 没有可用的 IM 投递路由。",
    "SESSION_ROUTE_MISMATCH": "目标 Session 的人员/群类型与 IM 路由不一致。",
    "CHANNEL_MEDIA_UNSUPPORTED": "目标通道暂不支持此音视频发送能力。",
    "RECIPIENT_MEDIA_ROUTE_UNAVAILABLE": "目标人员没有可用的音视频投递路由。",
    "RECIPIENT_NOT_FOUND": "未找到目标人员。",
    "MEDIA_TOO_LARGE": "媒体文件超过目标通道允许的大小。",
    "VIDEO_COVER_TOO_LARGE": "视频封面超过允许的大小。",
    "MEDIA_BUNDLE_TOO_LARGE": "视频与封面的合计大小超过允许值。",
    "MEDIA_UPLOAD_FAILED": "媒体上传到目标通道失败。",
    "MEDIA_SEND_FAILED": "目标通道拒绝或未完成媒体发送。",
    "MEDIA_DELIVERY_FAILED": "媒体发送失败。",
    "MEDIA_DELIVERY_STATE_UNKNOWN": "发送结果不确定，媒体可能已经送达；不要自动重试，以免重复发送。",
    "MEDIA_SENT_CAPTION_FAILED": "媒体已发送，但后续说明文字发送失败；不要重发媒体。",
}


def _describe_media_delivery_result(payload: dict) -> dict:
    """Add stable human/Agent-facing error semantics to a media result."""
    status = str(payload.get("status") or "")
    code = str(payload.get("code") or "")
    if status in {"sent", "already_sent"} and code != "MEDIA_SENT_CAPTION_FAILED":
        return payload
    message = _MEDIA_DELIVERY_MESSAGES.get(
        code,
        "媒体发送未完成，请根据 status 和 code 向用户说明结果。",
    )
    if code == "MEDIA_SENT_CAPTION_FAILED":
        agent_action = "Tell the user the media was sent but its caption failed; do not resend the media."
    elif status == "unknown":
        agent_action = "Report the uncertain result and do not retry this call automatically."
    else:
        agent_action = "Tell the user the media delivery did not complete; do not claim success."
    return {
        **payload,
        "message": payload.get("message") or message,
        "retryable": False,
        "agent_action": agent_action,
    }


def _sniff_media_file_kind(file_path: Path) -> str | None:
    """Recognize common playable containers from bytes, not the filename."""
    mime = _sniff_media_file_mime(file_path)
    return mime.split("/", 1)[0] if mime else None


def _sniff_media_file_mime(file_path: Path) -> str | None:
    """Return the concrete media MIME represented by the file bytes."""
    try:
        with file_path.open("rb") as handle:
            size = file_path.stat().st_size
            if size <= MEDIA_PROBE_CHUNK_BYTES * 2:
                probe = handle.read()
            else:
                head = handle.read(MEDIA_PROBE_CHUNK_BYTES)
                handle.seek(max(0, size - MEDIA_PROBE_CHUNK_BYTES))
                probe = head + handle.read(MEDIA_PROBE_CHUNK_BYTES)
    except OSError:
        return None
    return sniff_media_mime_bytes(probe, file_path.name)


def _sniff_image_file(file_path: Path) -> str | None:
    """Return the common image format represented by the file bytes."""
    try:
        with file_path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return None
    return sniff_image_kind_bytes(head)


def _external_media_filename(media_url: str, media_kind: str) -> str:
    name = PurePosixPath(unquote(urlsplit(media_url).path)).name.strip()
    name = name.replace("\\", "_").replace("/", "_")
    return name[:180] or f"external-{media_kind}"


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


def _normalize_tool_workspace_rel_path(raw_path: str) -> str | None:
    path = raw_path.strip().replace("\\", "/")
    if not path:
        return None
    if path.startswith("/") or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", path):
        return None
    normalized = PurePosixPath(path)
    if normalized.is_absolute() or any(part == ".." for part in normalized.parts):
        return None
    return normalized.as_posix()


def _platform_file_delivery_result(file_path: Path, rel_path: str, message: str = "") -> str:
    payload = {
        "type": "platform_file_delivery",
        "path": rel_path,
        "filename": file_path.name,
        "message": message or "",
        "mime_type": mimetypes.guess_type(file_path.name)[0] or "application/octet-stream",
        "size": file_path.stat().st_size,
    }
    return json.dumps(payload, ensure_ascii=False)


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







async def _send_feishu_message(
    agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send Feishu IM by canonical platform user_id."""
    canonical_user_id = (args.get("user_id") or "").strip()
    message_text = sanitize_user_visible_text(
        str(args.get("message") or "")
    ).strip()
    if not canonical_user_id or not message_text:
        return "❌ Please provide canonical user_id and message content"
    try:
        from app.services.feishu_service import FeishuAPIError, feishu_service

        async with async_session() as db:
            try:
                route = await resolve_human_channel_recipient(db, agent_id, canonical_user_id, channel="feishu")
            except RecipientResolutionError as exc:
                return exc.as_json()
            config_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "feishu",
                    ChannelConfig.is_configured.is_(True),
                )
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no Feishu channel configured"
            receive_id = (route.member.external_id or route.member.open_id or "").strip()
            receive_id_type = "user_id" if route.member.external_id else "open_id"
            if not receive_id:
                return RecipientResolutionError(
                    "recipient_unreachable", "Canonical user has no usable Feishu endpoint"
                ).as_json()
            session = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=route.user.id,
                external_conv_id=f"feishu_p2p_{receive_id}",
                source_channel="feishu",
                first_message_title=f"[Agent → {route.user.display_name}]",
            )
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=route.user.id,
                session=session,
                content=message_text,
                source_channel="feishu",
                actor_ref=receive_id,
                target_name=route.user.display_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending("feishu"),
            )
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)
            try:
                resp = await feishu_service.send_message(
                    config.app_id,
                    config.app_secret,
                    receive_id=receive_id,
                    msg_type="text",
                    content=json.dumps({"text": message_text}, ensure_ascii=False),
                    receive_id_type=receive_id_type,
                )
            except FeishuAPIError as exc:
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.from_exception("feishu", exc),
                )
                return f"❌ Feishu send failed: {exc.user_message}"
            if resp.get("code") != 0:
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.failed("feishu", str(resp.get("code") or "send_failed")),
                )
                return f"❌ Feishu send failed: {resp.get('msg')} (code {resp.get('code')})"

            external_message_id = str(
                ((resp.get("data") or {}).get("message_id"))
                or resp.get("message_id")
                or ""
            ) or None
            await register_delivery(
                receipt_id,
                IMDeliveryResult.sent(
                    "feishu",
                    IMDeliveryPart(
                        transport="feishu_message",
                        provider_message_id=external_message_id,
                        conversation_ref=receive_id,
                        recallable=bool(external_message_id),
                    ),
                ),
            )
            return (
                f"✅ Message sent to {route.user.display_name} via Feishu\n"
                f"message_id: {receipt_id}"
            )
    except Exception as e:
        logger.exception("[Feishu] canonical send failed")
        return f"❌ Message send error: {str(e)[:200]}"
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
_PLATFORM_SESSION_CHANNELS = frozenset({"web", "miniprogram", "wechat_miniprogram"})
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


async def _send_dingtalk_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: "OrgMember",
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send message via DingTalk channel using Open API."""
    from app.services.dingtalk_service import send_dingtalk_message

    try:
        async with async_session() as db:
            # 1. Get DingTalk channel config
            config_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "dingtalk",
                    ChannelConfig.is_configured == True,
                )
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no DingTalk channel configured"

            # 2. Get recipient's user_id (external_id)
            user_id = target_member.external_id
            if not user_id:
                # Try to use unionid or openid as fallback
                user_id = target_member.unionid or target_member.open_id
                if not user_id:
                    return f"❌ {member_name} has no DingTalk user_id"

            logger.info(f"[DingTalk] Sending to user_id: {user_id}")

            # Get agent_id from extra_config (required for DingTalk API)
            agent_id_dingtalk = config.extra_config.get("agent_id") if config.extra_config else None

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent_obj = agent_r.scalar_one_or_none()
            platform_user = await get_platform_user_by_org_member(
                db=db,
                org_member=target_member,
                agent_tenant_id=agent_obj.tenant_id if agent_obj else None,
            )
            conv_id = f"dingtalk_p2p_{user_id}"
            sess = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=platform_user.id,
                external_conv_id=conv_id,
                source_channel="dingtalk",
                first_message_title=message_text[:30],
            )
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=platform_user.id,
                session=sess,
                content=message_text,
                source_channel="dingtalk",
                actor_ref=str(user_id),
                target_name=member_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending("dingtalk"),
            )
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)

            # 3. Send message via DingTalk service
            result = await send_dingtalk_message(
                app_id=config.app_id,
                app_secret=config.app_secret,
                user_id=user_id,
                message=message_text,
                agent_id=agent_id_dingtalk,
            )

            if result.get("errcode") == 0:
                process_key = str(result.get("processQueryKey") or "") or None
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.sent(
                        "dingtalk",
                        IMDeliveryPart(
                            transport="dingtalk_openapi_oto",
                            provider_message_id=process_key,
                            conversation_ref=str(user_id),
                            recallable=bool(process_key),
                        ),
                    ),
                )
                logger.info(f"[DingTalk] Proactive message saved to session {sess.id}")
                return f"✅ Message sent to {member_name} via DingTalk\nmessage_id: {receipt_id}"
            else:
                errmsg = result.get("errmsg", "Unknown error")
                logger.error(f"[DingTalk] Send failed: {result}")
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.failed("dingtalk", str(result.get("errcode") or errmsg)),
                )
                return f"❌ DingTalk send failed: {errmsg}"

    except Exception as e:
        logger.exception("[DingTalk] Error")
        return f"❌ DingTalk message error: {str(e)[:200]}"


async def _send_wecom_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: "OrgMember",
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send message via WeCom channel using Open API."""
    from app.services.wecom_service import send_wecom_message

    try:
        async with async_session() as db:
            # 1. Get WeCom channel config
            config_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "wecom",
                    ChannelConfig.is_configured == True,
                )
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no WeCom channel configured"

            # 2. Get recipient's user_id
            user_id = target_member.external_id
            if not user_id:
                user_id = target_member.open_id
                if not user_id:
                    return f"❌ {member_name} has no WeCom user_id"

            logger.info(f"[WeCom] Sending to user_id: {user_id}")

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = agent_r.scalar_one_or_none()
            platform_user = await get_platform_user_by_org_member(
                db=db,
                org_member=target_member,
                agent_tenant_id=agent.tenant_id if agent else None,
            )
            conv_id = f"wecom_p2p_{user_id}"
            sess = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=platform_user.id,
                external_conv_id=conv_id,
                source_channel="wecom",
                first_message_title=message_text[:30],
            )
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=platform_user.id,
                session=sess,
                content=message_text,
                source_channel="wecom",
                actor_ref=str(user_id),
                target_name=member_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending("wecom"),
            )
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)

            # 3. Send message via WeCom service
            result = await send_wecom_message(
                config.app_id,
                config.app_secret,
                user_id,
                message_text,
                agent_id=str((config.extra_config or {}).get("wecom_agent_id") or "") or None,
            )

            if result.get("errcode") == 0:
                msgid = str(result.get("msgid") or "") or None
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.sent(
                        "wecom",
                        IMDeliveryPart(
                            transport="wecom_app",
                            provider_message_id=msgid,
                            conversation_ref=str(user_id),
                            recallable=bool(msgid),
                        ),
                    ),
                )
                logger.info(f"[WeCom] Proactive message saved to session {sess.id}")
                return f"✅ Message sent to {member_name} via WeCom\nmessage_id: {receipt_id}"
            else:
                errmsg = result.get("errmsg", "Unknown error")
                logger.error(f"[WeCom] Send failed: {result}")
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.failed("wecom", str(result.get("errcode") or errmsg)),
                )
                return f"❌ WeCom send failed: {errmsg}"

    except Exception as e:
        logger.exception("[WeCom] Error")
        return f"❌ WeCom message error: {str(e)[:200]}"


async def _send_slack_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: "OrgMember",
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send proactive Slack DM via conversations.open + chat.postMessage."""
    import httpx

    from app.api.slack import _send_slack_messages

    try:
        async with async_session() as db:
            config_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "slack",
                    ChannelConfig.is_configured == True,
                )
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no Slack channel configured"

            user_id = (target_member.external_id or "").strip()
            if not user_id:
                return f"❌ {member_name} has no Slack user_id"

            bot_token = (config.app_secret or "").strip()
            if not bot_token:
                return "❌ Slack bot token is missing"

            async with httpx.AsyncClient(timeout=10) as client:
                open_resp = await client.post(
                    "https://slack.com/api/conversations.open",
                    headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                    json={"users": user_id},
                )
                data = open_resp.json()
                if open_resp.status_code >= 400 or not data.get("ok"):
                    err = data.get("error") or open_resp.text[:200]
                    return f"❌ Slack conversations.open failed: {err}"
                channel_id = ((data.get("channel") or {}).get("id") or "").strip()

            if not channel_id:
                return f"❌ Slack DM channel unavailable for {member_name}"

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent_obj = agent_r.scalar_one_or_none()
            platform_user = await get_platform_user_by_org_member(
                db=db,
                org_member=target_member,
                agent_tenant_id=agent_obj.tenant_id if agent_obj else None,
            )
            sess = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=platform_user.id,
                external_conv_id=f"slack_{channel_id}",
                source_channel="slack",
                first_message_title=message_text[:30],
            )
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=platform_user.id,
                session=sess,
                content=message_text,
                source_channel="slack",
                actor_ref=str(user_id),
                target_name=member_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending("slack"),
            )
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)

            async def _record_slack_part(response: dict) -> None:
                part = IMDeliveryPart(
                    transport="slack",
                    provider_message_id=str(response.get("ts") or "") or None,
                    conversation_ref=str(response.get("channel") or channel_id),
                    artifact_role="chunk",
                    recallable=bool(response.get("ts")),
                )
                await append_delivery_part(receipt_id, part)

            try:
                slack_responses = await _send_slack_messages(
                    bot_token,
                    channel_id,
                    message_text,
                    on_result=_record_slack_part,
                )
            except Exception as exc:
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.from_exception("slack", exc),
                )
                raise
            await register_delivery(
                receipt_id,
                IMDeliveryResult.sent(
                    "slack",
                    *(
                        IMDeliveryPart(
                            transport="slack",
                            provider_message_id=str(response.get("ts") or "") or None,
                            conversation_ref=str(response.get("channel") or channel_id),
                            artifact_role="chunk",
                            recallable=bool(response.get("ts")),
                        )
                        for response in slack_responses
                    ),
                ),
            )
            logger.info(f"[Slack] Proactive message saved to session {sess.id}")
            return f"✅ Message sent to {member_name} via Slack\nmessage_id: {receipt_id}"
    except Exception as e:
        logger.exception("[Slack] Error")
        return f"❌ Slack message error: {str(e)[:200]}"


async def _send_teams_channel_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: "OrgMember",
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send proactive Teams message using the latest known conversation context."""
    from app.api.teams import _send_teams_message

    try:
        async with async_session() as db:
            config_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "microsoft_teams",
                    ChannelConfig.is_configured == True,
                )
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no Teams channel configured"

            service_url = str((config.extra_config or {}).get("service_url") or "").strip()
            if not service_url:
                return "❌ Teams proactive send requires an existing inbound conversation to capture service_url"

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent_obj = agent_r.scalar_one_or_none()
            platform_user = await get_platform_user_by_org_member(
                db=db,
                org_member=target_member,
                agent_tenant_id=agent_obj.tenant_id if agent_obj else None,
            )

            session_result = await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.user_id == platform_user.id,
                    ChatSession.source_channel == "microsoft_teams",
                    ChatSession.is_group == False,
                )
                .order_by(ChatSession.last_message_at.desc(), ChatSession.created_at.desc())
                .limit(1)
            )
            session = session_result.scalar_one_or_none()
            conversation_id = str(session.external_conv_id or "").strip() if session else ""
            if not conversation_id:
                return f"❌ Teams proactive send to {member_name} requires them to message the bot first"

            actor_ref = str(target_member.external_id or target_member.open_id or platform_user.id)
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=platform_user.id,
                session=session,
                content=message_text,
                source_channel="microsoft_teams",
                actor_ref=actor_ref,
                target_name=member_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending("microsoft_teams"),
            )
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)

            async def _record_teams_part(response: dict) -> None:
                part = IMDeliveryPart(
                    transport="microsoft_teams",
                    provider_message_id=str(response.get("id") or "") or None,
                    conversation_ref=conversation_id,
                    artifact_role="chunk",
                    recallable=bool(response.get("id")),
                    metadata={"service_url": service_url},
                )
                await append_delivery_part(receipt_id, part)

            try:
                teams_responses = await _send_teams_message(
                    config,
                    conversation_id,
                    {
                        "type": "message",
                        "text": message_text,
                        "conversation": {"id": conversation_id},
                    },
                    on_result=_record_teams_part,
                )
            except Exception as exc:
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.from_exception("microsoft_teams", exc),
                )
                raise
            await register_delivery(
                receipt_id,
                IMDeliveryResult.sent(
                    "microsoft_teams",
                    *(
                        IMDeliveryPart(
                            transport="microsoft_teams",
                            provider_message_id=str(response.get("id") or "") or None,
                            conversation_ref=conversation_id,
                            artifact_role="chunk",
                            recallable=bool(response.get("id")),
                            metadata={"service_url": service_url},
                        )
                        for response in teams_responses
                    ),
                ),
            )
            logger.info(f"[Teams] Proactive message saved to session {session.id}")
            return f"✅ Message sent to {member_name} via Teams\nmessage_id: {receipt_id}"
    except Exception as e:
        logger.exception("[Teams] Error")
        return f"❌ Teams message error: {str(e)[:200]}"


async def _send_wechat_channel_message(
    agent_id: uuid.UUID,
    member_name: str,
    message_text: str,
    target_member: "OrgMember",
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send proactive WeChat message using the latest cached context_token."""
    from app.services.wechat_channel import (
        WECHAT_ILINK_BASE_URL,
        get_wechat_context_entry,
        send_wechat_text_message,
    )

    try:
        async with async_session() as db:
            config_result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.agent_id == agent_id,
                    ChannelConfig.channel_type == "wechat",
                    ChannelConfig.is_configured == True,
                )
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no WeChat channel configured"

            user_id = (target_member.external_id or "").strip()
            if not user_id:
                return f"❌ {member_name} has no WeChat user_id"

            ctx_entry = get_wechat_context_entry(config.extra_config, from_user_id=user_id)
            context_token = str((ctx_entry or {}).get("context_token") or "").strip()
            conv_id = str((ctx_entry or {}).get("conv_id") or f"wechat_{user_id}").strip()
            if not context_token:
                return f"❌ WeChat proactive send to {member_name} requires them to message the bot first"

            token = str((config.extra_config or {}).get("bot_token") or "").strip()
            base_url = str((config.extra_config or {}).get("baseurl") or WECHAT_ILINK_BASE_URL).strip()
            route_tag = str((config.extra_config or {}).get("route_tag") or "").strip() or None
            if not token:
                return "❌ WeChat bot token is missing"

            agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent_obj = agent_r.scalar_one_or_none()
            platform_user = await get_platform_user_by_org_member(
                db=db,
                org_member=target_member,
                agent_tenant_id=agent_obj.tenant_id if agent_obj else None,
            )
            sess = await find_or_create_channel_session(
                db=db,
                agent_id=agent_id,
                user_id=platform_user.id,
                external_conv_id=conv_id,
                source_channel="wechat",
                first_message_title=message_text[:30],
            )
            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=platform_user.id,
                session=sess,
                content=message_text,
                source_channel="wechat",
                actor_ref=str(user_id),
                target_name=member_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
                delivery_result=IMDeliveryResult.pending("wechat"),
            )
            receipt_id = receipt.id
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)

            async def _record_wechat_part(response: dict) -> None:
                part = IMDeliveryPart(
                    transport="wechat_ilink",
                    provider_message_id=str(response.get("client_id") or "") or None,
                    conversation_ref=user_id,
                    artifact_role="chunk",
                    recallable=False,
                )
                await append_delivery_part(receipt_id, part)

            try:
                wechat_responses = await send_wechat_text_message(
                    token=token,
                    base_url=base_url,
                    to_user_id=user_id,
                    context_token=context_token,
                    text=message_text,
                    route_tag=route_tag,
                    on_result=_record_wechat_part,
                )
            except Exception as exc:
                await register_delivery(
                    receipt_id,
                    IMDeliveryResult.from_exception("wechat", exc),
                )
                raise
            await register_delivery(
                receipt_id,
                IMDeliveryResult.sent(
                    "wechat",
                    *(
                        IMDeliveryPart(
                            transport="wechat_ilink",
                            provider_message_id=str(response.get("client_id") or "") or None,
                            conversation_ref=user_id,
                            artifact_role="chunk",
                            recallable=False,
                        )
                        for response in wechat_responses
                    ),
                ),
            )
            logger.info(f"[WeChat] Proactive message saved to session {sess.id}")
            return f"✅ Message sent to {member_name} via WeChat\nmessage_id: {receipt_id}"
    except Exception as e:
        logger.exception("[WeChat] Error")
        return f"❌ WeChat message error: {str(e)[:200]}"


async def _send_platform_message(
    agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    origin_user_id: uuid.UUID | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send a proactive message to a first-party platform user."""
    canonical_user_id = str(args.get("user_id") or "").strip()
    message_text = sanitize_user_visible_text(
        str(args.get("message") or "")
    ).strip()

    if not canonical_user_id or not message_text:
        return "❌ Please provide canonical user_id and message content"

    try:
        async with async_session() as db:
            try:
                recipient = await resolve_platform_user_recipient(db, agent_id, canonical_user_id)
            except RecipientResolutionError as exc:
                return exc.as_json()
            target_user = recipient.user

            # Agent-initiated platform messages should always go to the long-lived primary session
            # for this agent+user pair, so trigger-driven outreach does not fragment into dozens of
            # tiny one-off web sessions.
            from app.services.chat_session_service import ensure_primary_platform_session

            session = await ensure_primary_platform_session(db, agent_id, target_user.id)

            receipt, should_deliver = await _persist_outbound_channel_message(
                db,
                agent_id=agent_id,
                user_id=target_user.id,
                session=session,
                content=message_text,
                source_channel=session.source_channel,
                actor_ref=str(target_user.id),
                target_name=target_user.display_name,
                origin_session_id=origin_session_id,
                origin_source_channel=None,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
            )
            if should_deliver:
                try:
                    from app.api.websocket import maybe_mark_session_read_for_active_viewer

                    await maybe_mark_session_read_for_active_viewer(
                        db,
                        agent_id=agent_id,
                        session_id=str(session.id),
                        user_id=target_user.id,
                    )
                except Exception:
                    pass
            await db.commit()
            if not should_deliver:
                return _duplicate_outbound_claim_result(receipt)

            try:
                # Push via WebSocket if user has an active connection
                from app.api.websocket import manager as ws_manager

                await ws_manager.send_to_user(
                    str(agent_id),
                    str(target_user.id),
                    {
                        "type": "trigger_notification",
                        "content": message_text,
                        "triggers": ["web_message"],
                        "session_id": str(session.id),
                    },
                )
            except Exception:
                pass

            display = target_user.display_name
            return f"✅ Message sent to {display} on web platform. It has been saved to their chat history."

    except Exception as e:
        logger.exception("[PlatformMessage] Error")
        return f"❌ Web message send error: {str(e)[:200]}"


async def _send_file_to_agent(
    from_agent_id: uuid.UUID,
    args: dict,
    *,
    origin_session_id: str | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send a workspace file to another digital employee (agent)."""
    canonical_agent_id = str(args.get("agent_id") or "").strip()
    rel_path = (args.get("file_path") or "").strip()
    delivery_note = (args.get("message") or "").strip()

    if not canonical_agent_id or not rel_path:
        return "❌ Please provide both canonical agent_id and file_path"

    storage = get_storage_backend()
    source_key = current_agent_runtime_workspace(from_agent_id).storage_key(rel_path)
    if not await storage.is_file(source_key):
        return f"❌ Source file not found: {rel_path}"
    source_entry = await storage.stat(source_key)

    # File size limit (50 MB)
    MAX_FILE_SIZE = 50 * 1024 * 1024
    file_size = source_entry.size
    if file_size > MAX_FILE_SIZE:
        size_mb = file_size / (1024 * 1024)
        return f"❌ File too large ({size_mb:.1f} MB). Maximum allowed is 50 MB."
    source_bytes = await storage.read_bytes(source_key)
    source_name = Path(rel_path).name

    try:
        from app.services.activity_logger import log_activity
        from app.services.a2a_file_delivery import resolve_a2a_file_origin_scope

        async with async_session() as db:
            try:
                origin_scope = await resolve_a2a_file_origin_scope(
                    db,
                    origin_session_id=origin_session_id,
                    sender_agent_id=from_agent_id,
                )
                explicit_project_id = None
                if args.get("_project_id"):
                    try:
                        explicit_project_id = uuid.UUID(str(args["_project_id"]))
                    except (TypeError, ValueError):
                        return "❌ _project_id must be a complete platform UUID"
                if (
                    origin_scope.project_id is not None
                    and explicit_project_id is not None
                    and origin_scope.project_id != explicit_project_id
                ):
                    return "❌ The file delivery project does not match the originating conversation"
                project_id = origin_scope.project_id or explicit_project_id
                recipient = await resolve_agent_recipient(db, from_agent_id, canonical_agent_id, project_id=project_id)
            except RecipientResolutionError as exc:
                return exc.as_json()
            source_agent = recipient.source_agent
            target_agent = recipient.target_agent
            source_agent_name = source_agent.name
            source_creator_id = source_agent.creator_id
            target_name = target_agent.name
            target_id = target_agent.id

            if str(getattr(target_agent, "scope", "") or "").strip().lower() == "project":
                if project_id is None or getattr(target_agent, "project_id", None) != project_id:
                    return "❌ Project Agent file delivery must remain inside its owning project"
                target_workspace = project_agent_runtime_workspace(
                    agent_id=target_id,
                    tenant_id=target_agent.tenant_id,
                    project_id=project_id,
                )
            else:
                target_workspace = standard_agent_runtime_workspace(target_id)

        ts = datetime.now(timezone.utc)
        stamp = ts.strftime("%Y%m%d_%H%M%S_%f")
        delivered_name = source_name
        target_rel_path = f"workspace/inbox/files/{delivered_name}"
        target_key = target_workspace.storage_key(target_rel_path)
        while await storage.exists(target_key):
            delivered_name = f"{stamp}_{source_name}"
            target_rel_path = f"workspace/inbox/files/{delivered_name}"
            target_key = target_workspace.storage_key(target_rel_path)

        await storage.write_bytes(target_key, source_bytes)

        sender_short = str(from_agent_id)[:8]
        note_rel_path = f"workspace/inbox/{stamp}_{sender_short}_file_delivery.md"
        note_key = target_workspace.storage_key(note_rel_path)
        note_lines = [
            f"# File delivery from {source_agent_name}",
            "",
            f"- Time (UTC): {ts.isoformat()}",
            f"- Sender: {source_agent_name}",
            f"- Source path: {rel_path}",
            f"- Delivered file: {target_rel_path}",
            "",
        ]
        if delivery_note:
            note_lines.append("## Note")
            note_lines.append(delivery_note)
            note_lines.append("")
        note_lines.append("## Action")
        note_lines.append(f'- Read the file via `read_file(path="{target_rel_path}")`')
        await storage.write_text(note_key, "\n".join(note_lines), encoding="utf-8")

        from app.models.audit import AuditLog

        async with async_session() as db:
            db.add(
                AuditLog(
                    agent_id=from_agent_id,
                    action="collaboration:file_send",
                    details={
                        "to_agent": str(target_id),
                        "to_agent_name": target_name,
                        "source_file": rel_path,
                        "delivered_file": target_rel_path,
                    },
                )
            )
            db.add(
                AuditLog(
                    agent_id=target_id,
                    action="collaboration:file_receive",
                    details={
                        "from_agent": str(from_agent_id),
                        "from_agent_name": source_agent_name,
                        "source_file": rel_path,
                        "delivered_file": target_rel_path,
                    },
                )
            )
            await db.commit()

        await log_activity(
            from_agent_id,
            "agent_file_sent",
            f"Sent file to {target_name}",
            detail={"target_agent": target_name, "source_file": rel_path, "delivered_file": target_rel_path},
        )
        await log_activity(
            target_id,
            "agent_file_received",
            f"Received file from {source_agent_name}",
            detail={"source_agent": source_agent_name, "source_file": rel_path, "delivered_file": target_rel_path},
        )

        # Inject the file into the exact standard A2A conversation. Project
        # child executions resolve through their durable SubagentRun parent;
        # multiple legitimate threads for the same Agent pair remain distinct.
        logger.info(
            "[A2A-File] Injecting file delivery message: from=%s to=%s file=%s",
            source_name,
            target_name,
            delivered_name,
        )
        from app.services.a2a_file_delivery import append_a2a_file_delivery_message

        operation_key = _build_outbound_operation_key(
            agent_id=from_agent_id,
            origin_session_id=origin_session_id,
            tool_call_id=tool_call_id,
            origin_turn_anchor_id=origin_turn_anchor_id,
        )
        file_event_key = f"{operation_key}:file"[:500] if operation_key else None
        async with async_session() as db2:
            chat_session_id = await append_a2a_file_delivery_message(
                db2,
                sender_agent_id=from_agent_id,
                target_agent_id=target_id,
                sender_creator_id=source_creator_id,
                sender_name=source_agent_name,
                target_name=target_name,
                project_id=project_id,
                preferred_session_id=origin_scope.preferred_session_id,
                source_path=rel_path,
                delivered_path=target_rel_path,
                delivered_name=delivered_name,
                delivery_note=delivery_note,
                file_size=file_size,
                created_at=ts,
                external_event_key=file_event_key,
                origin_session_id=origin_session_id,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
            )
            await db2.commit()
        logger.info(
            "[A2A-File] Injected file delivery message into session %s for %s",
            chat_session_id,
            target_name,
        )

        return f"✅ File sent to {target_name}.\n- Delivered to: {target_rel_path}\n- Inbox note: {note_rel_path}"
    except Exception as e:
        logger.exception("[A2A-File] File delivery failed")
        return f"❌ Agent file send error: {str(e)[:200]}"


async def _resolve_a2a_target(db, from_agent_id: uuid.UUID, agent_id: str) -> tuple[AgentModel | None, str | None]:
    """Compatibility helper backed by the canonical exact-ID resolver."""
    try:
        recipient = await resolve_agent_recipient(db, from_agent_id, agent_id)
    except RecipientResolutionError as exc:
        return None, exc.as_json()
    return recipient.target_agent, None


async def _create_on_message_trigger(
    agent_id: uuid.UUID,
    trigger_name: str,
    from_agent_id_value: str | None,
    from_user_id_value: str | None = None,
    reason: str = "",
    focus_ref: str | None = None,
    notification_summary: str | None = None,
    origin_session_id: str | None = None,
    origin_user_id: str | None = None,
    origin_source_channel: str | None = None,
    origin_external_conv_id: str | None = None,
    origin_turn_anchor_id: str | None = None,
    watch_session_id: str | None = None,
    watch_source_channel: str | None = None,
    watch_actor_ref: str | None = None,
    outbound_message_id: str | None = None,
    outbound_external_message_id: str | None = None,
    consume_remote: bool = False,
    expires_in_minutes: int = 1440,
) -> None:
    """Programmatically create an on_message trigger for an agent."""
    from app.models.trigger import AgentTrigger

    creator_user_id = uuid.UUID(origin_user_id) if origin_user_id else None
    focus_ref = await ensure_focus_item(
        agent_id,
        focus_ref=focus_ref,
        description=reason or trigger_name,
    )

    config: dict = {}
    if bool(from_agent_id_value) == bool(from_user_id_value):
        raise ValueError("on_message requires exactly one canonical sender ID")
    if from_agent_id_value:
        config["from_agent_id"] = str(uuid.UUID(from_agent_id_value))
    if from_user_id_value:
        config["from_user_id"] = str(uuid.UUID(from_user_id_value))
    if notification_summary:
        config["_notification_summary"] = notification_summary
    if origin_session_id:
        config["_origin_session_id"] = origin_session_id
    if origin_user_id:
        config["_origin_user_id"] = origin_user_id
    if origin_source_channel:
        config["_origin_source_channel"] = origin_source_channel
    if origin_external_conv_id is not None:
        config["_origin_external_conv_id"] = origin_external_conv_id
    if origin_turn_anchor_id:
        config["_origin_turn_anchor_id"] = origin_turn_anchor_id
        # New exact subscriptions wait until the ordinary turn that armed them
        # has durably persisted its final assistant row.  Keep this explicit so
        # legacy triggers created before the completion marker was introduced
        # remain recoverable after an upgrade.
        config["_origin_completion_barrier"] = True
    if watch_session_id:
        config["_watch_session_id"] = watch_session_id
    if watch_source_channel:
        config["_watch_source_channel"] = watch_source_channel
    if watch_actor_ref:
        config["_watch_actor_ref"] = watch_actor_ref
    if outbound_message_id:
        config["_outbound_message_id"] = outbound_message_id
    if outbound_external_message_id:
        config["_outbound_external_message_id"] = outbound_external_message_id
        config["_correlation_mode"] = "reply_to"
    elif watch_session_id:
        config["_correlation_mode"] = "next_message"
    if watch_session_id:
        config["_consume_remote"] = bool(consume_remote)
    config["_set_trigger_context"] = {
        "name": trigger_name,
        "type": "on_message",
        "reason": reason,
        "focus_ref": focus_ref or "",
        "config": {key: value for key, value in config.items() if not str(key).startswith("_")},
    }

    try:
        from app.models.audit import ChatMessage as _CM
        from app.models.chat_session import ChatSession as _CS
        from sqlalchemy import cast as sa_cast, String as SaString

        async with async_session() as _snap_db:
            if origin_turn_anchor_id and origin_session_id:
                try:
                    _origin_anchor = await _snap_db.get(
                        _CM,
                        uuid.UUID(str(origin_turn_anchor_id)),
                    )
                except (TypeError, ValueError):
                    _origin_anchor = None
                if _origin_anchor is not None and _origin_anchor.conversation_id == str(origin_session_id):
                    _origin_meta = _origin_anchor.message_meta if isinstance(_origin_anchor.message_meta, dict) else {}
                    if _origin_meta.get("actor_ref"):
                        config["_origin_actor_ref"] = str(_origin_meta["actor_ref"])
                    if _origin_meta.get("actor_ref_type"):
                        config["_origin_actor_ref_type"] = str(_origin_meta["actor_ref_type"])
            _outbound_anchor = None
            if outbound_message_id:
                try:
                    _outbound_anchor = await _snap_db.get(
                        _CM,
                        uuid.UUID(str(outbound_message_id)),
                    )
                except (TypeError, ValueError):
                    _outbound_anchor = None
            if (
                _outbound_anchor is not None
                and _outbound_anchor.conversation_id == str(watch_session_id or "")
                and _outbound_anchor.created_at is not None
            ):
                # The remote outbound row, not the latest message in any other
                # active session, is the event cursor for this subscription.
                config["_since_ts"] = _outbound_anchor.created_at.isoformat()
            else:
                _snap_q = (
                    select(_CM.created_at)
                    .join(_CS, _CM.conversation_id == sa_cast(_CS.id, SaString))
                    .where(
                        _CS.agent_id == agent_id,
                        _CM.created_at.isnot(None),
                    )
                    .order_by(_CM.created_at.desc())
                    .limit(1)
                )
                _snap_r = await _snap_db.execute(_snap_q)
                _latest_ts = _snap_r.scalar_one_or_none()
                if _latest_ts:
                    config["_since_ts"] = _latest_ts.isoformat()
    except Exception:
        pass

    async with async_session() as db:
        result = await db.execute(
            select(AgentTrigger).where(
                AgentTrigger.agent_id == agent_id,
                AgentTrigger.name == trigger_name,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            if existing.is_enabled:
                existing_cfg = existing.config or {}
                if existing_cfg.get("_origin_session_id") == config.get("_origin_session_id") and existing_cfg.get(
                    "_outbound_message_id"
                ) == config.get("_outbound_message_id"):
                    trigger = existing
                else:
                    raise RuntimeError(
                        f"Trigger '{trigger_name}' already exists and is active; "
                        "use a distinct trigger name or cancel the existing trigger first."
                    )
            else:
                if creator_user_id is not None:
                    from app.services.execution_identity import align_background_execution_user

                    await align_background_execution_user(
                        db,
                        agent_id=agent_id,
                        resource_type="trigger",
                        resource_id=existing.id,
                        execution_user_id=creator_user_id,
                    )
                existing.type = "on_message"
                existing.config = config
                existing.reason = reason
                existing.focus_ref = focus_ref or None
                existing.is_enabled = True
                existing.fire_count = 0
                trigger = existing
        else:
            trigger = AgentTrigger(
                agent_id=agent_id,
                created_by_user_id=creator_user_id,
                execution_user_id=creator_user_id,
                name=trigger_name,
                type="on_message",
                config=config,
                reason=reason,
                focus_ref=focus_ref or None,
                max_fires=1,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=max(1, expires_in_minutes)),
            )
            db.add(trigger)
        await db.commit()

    if watch_session_id:
        from app.services.trigger_runtime.evaluator import recover_exact_on_message_events

        try:
            await recover_exact_on_message_events(trigger)
        except Exception as replay_error:
            # The durable trigger is already armed; daemon recovery will retry
            # this best-effort immediate close of the send→arm race.
            logger.warning(
                "[A2A] reply-before-arm recovery failed for %s: %s",
                trigger_name,
                replay_error,
            )


async def _arm_a2a_delegate_callback(
    *,
    from_agent_id: uuid.UUID,
    target: AgentModel,
    message_text: str,
    owner_id: uuid.UUID,
    origin_session_id: str | None,
    origin_source_channel: str,
    origin_external_conv_id: str | None,
    origin_turn_anchor_id: uuid.UUID | None,
    watch_session_id: str,
    watch_actor_ref: str,
    outbound_message_id: uuid.UUID,
) -> None:
    """Arm the existing exact callback path for one A2A delegation event."""
    from app.models.trigger import AgentTrigger
    from sqlalchemy.exc import IntegrityError

    correlation_suffix = outbound_message_id.hex[:12]
    focus_id = f"wait_{target.id.hex[:8]}_{correlation_suffix}_task"
    await _append_focus_item(
        from_agent_id,
        focus_id,
        f"Waiting for {target.name} to complete delegated task: {message_text[:100]}",
    )
    trigger_name = f"a2a_wait_{target.id.hex[:8]}_{correlation_suffix}"
    trigger_reason = (
        f"{target.name} has replied with the result of a delegated task. "
        f"Original task: {message_text[:200]}. "
        f"Steps: 1) Process {target.name}'s reply. "
        f"2) Mark focus item '{focus_id}' as completed. "
        f"3) Cancel this trigger. "
        f"USER-FACING OUTPUT RULES: Your reply goes directly to the user's chat. "
        f"Write in natural, conversational language as if talking to a colleague. "
        f"NEVER use technical terms like: trigger name, focus item, a2a_wait, "
        f"task_delegate, focus_ref, or any internal identifier. "
        f"NEVER mention your internal operations (canceling triggers, updating focus, "
        f"marking items complete, trigger status, etc.). "
        f"Just summarize the task result in plain language."
    )

    def _same_callback(existing: AgentTrigger) -> bool:
        existing_cfg = existing.config if isinstance(existing.config, dict) else {}
        return (
            existing.is_enabled
            and str(existing_cfg.get("_watch_session_id") or "") == watch_session_id
            and str(existing_cfg.get("_outbound_message_id") or "") == str(outbound_message_id)
            and str(existing_cfg.get("_origin_session_id") or "") == str(origin_session_id or "")
        )

    async def _load_existing() -> AgentTrigger | None:
        async with async_session() as db:
            return (
                await db.execute(
                    select(AgentTrigger).where(
                        AgentTrigger.agent_id == from_agent_id,
                        AgentTrigger.name == trigger_name,
                    )
                )
            ).scalar_one_or_none()

    existing = await _load_existing()
    if existing is not None:
        if _same_callback(existing):
            return
        raise RuntimeError("A2A delegate callback key is already bound to another route")

    try:
        await _create_on_message_trigger(
            agent_id=from_agent_id,
            trigger_name=trigger_name,
            from_agent_id_value=str(target.id),
            reason=trigger_reason,
            focus_ref=focus_id,
            notification_summary=f"等待{target.name}完成任务并回复",
            origin_session_id=origin_session_id,
            origin_user_id=str(owner_id),
            origin_source_channel=origin_source_channel,
            origin_external_conv_id=origin_external_conv_id,
            origin_turn_anchor_id=str(origin_turn_anchor_id or "") or None,
            watch_session_id=watch_session_id,
            watch_source_channel="agent",
            watch_actor_ref=watch_actor_ref,
            outbound_message_id=str(outbound_message_id),
            consume_remote=True,
        )
    except IntegrityError:
        # Concurrent replay of the same tool call may lose the unique trigger
        # insert race. The database winner is success only if it is the exact
        # same callback envelope.
        existing = await _load_existing()
        if existing is not None and _same_callback(existing):
            return
        raise


async def _append_focus_item(agent_id: uuid.UUID, identifier: str, description: str) -> None:
    """Create or update an in-progress Focus item."""
    try:
        await ensure_focus_item(agent_id, focus_ref=identifier, description=description)
    except Exception as e:
        logger.warning(f"[A2A] Failed to update Focus for agent {agent_id}: {e}")


async def _wake_agent_async(
    agent_id: uuid.UUID,
    reason_context: str,
    *,
    from_agent_id: uuid.UUID | None = None,
    skip_dedup: bool = False,
    a2a_session_id: str | None = None,
) -> None:
    """Wake an agent asynchronously via the trigger invocation path.

    Delegates to the public wake_agent_with_context API in trigger_daemon.
    """
    from app.services.trigger_daemon import wake_agent_with_context

    kwargs = {"from_agent_id": from_agent_id, "skip_dedup": skip_dedup}
    if a2a_session_id is not None:
        kwargs["a2a_session_id"] = a2a_session_id
    await wake_agent_with_context(agent_id, reason_context, **kwargs)


async def _send_message_to_agent(
    from_agent_id: uuid.UUID,
    args: dict,
    user_id: uuid.UUID | None = None,
    origin_session_id: str | None = None,
    tool_call_id: str | None = None,
    origin_turn_anchor_id: uuid.UUID | None = None,
) -> str:
    """Send a message to another digital employee.

    Behaviour depends on ``msg_type``:
    - notify:   fire-and-forget — message is saved, target is woken asynchronously.
                Returns immediately.
    - task_delegate: async with callback — message is saved, source agent sets up
                a focus item + on_message trigger so it is notified when the
                target completes the task.  Returns immediately.
    - consult:  synchronous request-response (original behaviour).

    ``user_id`` / ``origin_session_id`` (added by the A2A trigger-routing fix):
    attribute the A2A session to the real caller and let a task_delegate callback
    trigger remember WHERE the originating conversation lived so the eventual
    reply is routed back to it.
    """
    recoverable_anchor = None
    canonical_agent_id = str(args.get("agent_id") or "").strip()
    message_text = args.get("message", "").strip()
    msg_type = args.get("msg_type", "notify").strip().lower()
    force_async = bool(args.get("force_async"))
    new_conversation = bool(args.get("new_conversation"))
    project_id = None
    if args.get("_project_id"):
        try:
            project_id = uuid.UUID(str(args["_project_id"]))
        except (TypeError, ValueError):
            return "❌ _project_id must be a complete platform UUID"
    work_item_id = None
    if args.get("_work_item_id"):
        try:
            work_item_id = uuid.UUID(str(args["_work_item_id"]))
        except (TypeError, ValueError):
            return "❌ _work_item_id must be a complete platform UUID"
        if project_id is None:
            return "❌ _work_item_id requires a project-scoped message"

    if not canonical_agent_id or not message_text:
        return "❌ Please provide canonical agent_id and message content"

    try:
        from app.models.participant import Participant
        from datetime import datetime, timezone

        # Resolve the originating conversation's channel so a delegate callback
        # can route its result back to the right place (web vs IM vs trigger).
        origin_source_channel = "web"
        origin_external_conv_id = None

        async with async_session() as db:
            if origin_session_id:
                try:
                    _osr = await db.execute(select(ChatSession).where(ChatSession.id == uuid.UUID(origin_session_id)))
                    _osess = _osr.scalar_one_or_none()
                    if _osess:
                        origin_source_channel = _osess.source_channel
                        origin_external_conv_id = _osess.external_conv_id
                except Exception:
                    pass

            try:
                recipient = await resolve_agent_recipient(db, from_agent_id, canonical_agent_id, project_id=project_id)
            except RecipientResolutionError as exc:
                return exc.as_json()
            source_agent = recipient.source_agent
            target = recipient.target_agent
            source_name = source_agent.name

            src_part_r = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == from_agent_id)
            )
            src_participant = src_part_r.scalar_one_or_none()
            tgt_part_r = await db.execute(
                select(Participant).where(Participant.type == "agent", Participant.ref_id == target.id)
            )
            tgt_participant = tgt_part_r.scalar_one_or_none()

            outbound_operation_key = _build_outbound_operation_key(
                agent_id=from_agent_id,
                origin_session_id=origin_session_id,
                tool_call_id=tool_call_id,
                origin_turn_anchor_id=origin_turn_anchor_id,
            )
            recorded_openclaw_outbound = None
            recorded_project_outbound = None
            if getattr(target, "agent_type", "native") == "openclaw" and outbound_operation_key:
                # A row lock cannot protect the first insert because the row does
                # not exist yet. The transaction-scoped operation lock closes
                # that gap across replicas and is released by the receipt commit.
                await _lock_outbound_operation(db, outbound_operation_key)
                candidate = (
                    await db.execute(
                        select(ChatMessage)
                        .where(ChatMessage.external_event_key == outbound_operation_key)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if candidate is not None:
                    candidate_meta = candidate.message_meta if isinstance(candidate.message_meta, dict) else {}
                    if str(candidate_meta.get("target_agent_id") or "") != str(target.id):
                        return "❌ The replayed message receipt does not match the requested target"
                    if candidate_meta.get("delivery_status") == "queued":
                        return f"✅ Message already sent to {target.name} via agent (idempotent replay)."
                    if candidate_meta.get("delivery_status") != "recorded":
                        return "❌ The replayed message receipt has an invalid delivery state"
                    try:
                        replay_session = await db.get(
                            ChatSession,
                            uuid.UUID(str(candidate.conversation_id)),
                        )
                    except (TypeError, ValueError):
                        replay_session = None
                    if replay_session is None:
                        return "❌ The recorded message's conversation no longer exists"
                    expected_pair = {from_agent_id, target.id}
                    if (
                        {
                            replay_session.agent_id,
                            replay_session.peer_agent_id,
                        }
                        != expected_pair
                        or replay_session.source_channel != "agent"
                        or replay_session.project_id != project_id
                    ):
                        return "❌ The recorded message's conversation route changed"
                    recorded_openclaw_outbound = candidate
            elif project_id is not None and outbound_operation_key:
                await _lock_outbound_operation(db, outbound_operation_key)
                candidate = (
                    await db.execute(
                        select(ChatMessage)
                        .where(ChatMessage.external_event_key == outbound_operation_key)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if candidate is not None:
                    candidate_meta = candidate.message_meta if isinstance(candidate.message_meta, dict) else {}
                    if str(candidate_meta.get("target_agent_id") or "") != str(target.id):
                        return "❌ The replayed project message does not match the requested target"
                    if str(candidate_meta.get("work_item_id") or "") != str(work_item_id or ""):
                        return "❌ The replayed project message does not match the requested work item"
                    try:
                        replay_session = await db.get(
                            ChatSession,
                            uuid.UUID(str(candidate.conversation_id)),
                        )
                    except (TypeError, ValueError):
                        replay_session = None
                    expected_pair = {from_agent_id, target.id}
                    if (
                        replay_session is None
                        or replay_session.source_channel != "agent"
                        or replay_session.project_id != project_id
                        or {replay_session.agent_id, replay_session.peer_agent_id} != expected_pair
                    ):
                        return "❌ The replayed project message's conversation route changed"
                    recorded_project_outbound = candidate

            # Find or create ChatSession for this agent pair (ordered consistently)
            session_agent_id = min(from_agent_id, target.id, key=str)
            session_peer_id = max(from_agent_id, target.id, key=str)
            # Attribute the A2A session to the real caller when known (A2A
            # trigger-routing fix); fall back to the source agent's creator.
            owner_id = user_id or (source_agent.creator_id if source_agent else from_agent_id)

            # Only reuse an existing thread when not explicitly starting a fresh one
            chat_session = (
                replay_session
                if recorded_openclaw_outbound is not None or recorded_project_outbound is not None
                else None
            )
            if chat_session is None and not new_conversation:
                sess_r = await db.execute(
                    select(ChatSession)
                    .where(
                        ChatSession.agent_id == session_agent_id,
                        ChatSession.peer_agent_id == session_peer_id,
                        ChatSession.source_channel == "agent",
                        ChatSession.project_id == project_id if project_id else ChatSession.project_id.is_(None),
                    )
                    .order_by(
                        ChatSession.last_message_at.desc().nulls_last(),
                        ChatSession.created_at.desc(),
                    )
                    .limit(1)
                )
                chat_session = sess_r.scalars().first()

            if not chat_session:
                _ext = f"project-a2a:{project_id}:{session_peer_id}" if project_id else None
                _suffix = ""
                if new_conversation:
                    from sqlalchemy import func as _sa_func

                    _ext = f"a2a-{uuid.uuid4().hex[:8]}"
                    _cnt_r = await db.execute(
                        select(_sa_func.count())
                        .select_from(ChatSession)
                        .where(
                            ChatSession.agent_id == session_agent_id,
                            ChatSession.peer_agent_id == session_peer_id,
                            ChatSession.source_channel == "agent",
                            ChatSession.project_id == project_id if project_id else ChatSession.project_id.is_(None),
                        )
                    )
                    _suffix = f" #{(_cnt_r.scalar() or 0) + 1}"
                src_part_id = src_participant.id if src_participant else None
                chat_session = ChatSession(
                    agent_id=session_agent_id,
                    project_id=project_id,
                    user_id=owner_id,
                    title=f"{source_name} ↔ {target.name}{_suffix}",
                    source_channel="agent",
                    participant_id=src_part_id,
                    peer_agent_id=session_peer_id,
                    external_conv_id=_ext,
                )
                db.add(chat_session)
                await db.flush()

            session_id = str(chat_session.id)

            # ── OpenClaw target: queue message for gateway poll ──
            if getattr(target, "agent_type", "native") == "openclaw":
                # 1. Save the source message to the chat session
                outbound_a2a_message = recorded_openclaw_outbound
                if outbound_a2a_message is None:
                    outbound_a2a_message = ChatMessage(
                        id=uuid.uuid4(),
                        agent_id=session_agent_id,
                        user_id=owner_id,
                        sender_agent_id=from_agent_id,
                        role="user",
                        content=message_text,
                        conversation_id=session_id,
                        participant_id=src_participant.id if src_participant else None,
                        external_event_key=outbound_operation_key,
                        message_meta={
                            "direction": "outbound",
                            "source_channel": "agent",
                            "origin_session_id": str(origin_session_id or ""),
                            "origin_source_channel": origin_source_channel,
                            "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
                            "tool_call_id": str(tool_call_id or ""),
                            "actor_ref": str(tgt_participant.id if tgt_participant else target.id),
                            "target_agent_id": str(target.id),
                            "target_name": target.name,
                            "delivery_status": "recorded",
                        },
                    )
                    db.add(outbound_a2a_message)
                    chat_session.last_message_at = datetime.now(timezone.utc)
                    await db.commit()

                # A delegated OpenClaw task uses the same exact callback
                # subscription as a native target, armed before it can poll.
                if msg_type == "task_delegate":
                    try:
                        await _arm_a2a_delegate_callback(
                            from_agent_id=from_agent_id,
                            target=target,
                            message_text=message_text,
                            owner_id=owner_id,
                            origin_session_id=origin_session_id,
                            origin_source_channel=origin_source_channel,
                            origin_external_conv_id=origin_external_conv_id,
                            origin_turn_anchor_id=origin_turn_anchor_id,
                            watch_session_id=session_id,
                            watch_actor_ref=str(tgt_participant.id if tgt_participant else target.id),
                            outbound_message_id=outbound_a2a_message.id,
                        )
                    except Exception as e:
                        logger.exception(f"[A2A] Failed to create OpenClaw delegate callback: {e}")
                        # Keep the durable `recorded` receipt. A concurrent
                        # replay may already have completed the callback and
                        # queued transition; deleting here could erase its
                        # receipt while leaving a live GatewayMessage. A later
                        # replay safely resumes this same operation key.
                        return (
                            "❌ The delegated message was recorded, but its delivery state "
                            "could not be confirmed. Please retry; the operation is idempotent."
                        )

                # 2. Queue for Gateway only after the callback is durable.
                # Reacquire the same operation lock after the recorded receipt
                # commit. Concurrent replays may both finish the idempotent
                # callback step, but only one may transition recorded -> queued.
                await _lock_outbound_operation(db, outbound_operation_key)
                await db.refresh(outbound_a2a_message)
                refreshed_meta = (
                    outbound_a2a_message.message_meta if isinstance(outbound_a2a_message.message_meta, dict) else {}
                )
                if refreshed_meta.get("delivery_status") == "queued":
                    return f"✅ Message already sent to {target.name} via agent (idempotent replay)."
                if refreshed_meta.get("delivery_status") != "recorded":
                    return "❌ The replayed message receipt has an invalid delivery state"

                from app.models.gateway_message import GatewayMessage as GMsg

                gw_msg = GMsg(
                    agent_id=target.id,
                    sender_agent_id=from_agent_id,
                    content=f"[From {source_name}] {message_text}",
                    status="pending",
                    conversation_id=session_id,
                )
                db.add(gw_msg)
                outbound_a2a_message.message_meta = {
                    **refreshed_meta,
                    "delivery_status": "queued",
                }
                await db.commit()

                # 3. Log activity
                from app.services.activity_logger import log_activity

                await log_activity(
                    from_agent_id,
                    "agent_msg_sent",
                    f"Sent message to {target.name} (queued)",
                    detail={"partner": target.name, "message": message_text[:200]},
                )

                online = (
                    target.openclaw_last_seen
                    and (datetime.now(timezone.utc) - target.openclaw_last_seen).total_seconds() < 300
                )
                status_hint = "online" if online else "offline (message will be delivered on next heartbeat)"
                if msg_type == "task_delegate":
                    return (
                        f"✅ Task delegated to {target.name} (OpenClaw agent, currently {status_hint}). "
                        "You will be notified when they complete it."
                    )
                return f"✅ Message sent to {target.name} (OpenClaw agent, currently {status_hint}). The message has been queued and will be delivered when the agent polls for updates."

            # ── Native target: branch by msg_type ──

            # Resolve the effective branch before the common write. Synchronous
            # consult joins the caller's root turn; notify/delegate remain
            # independent durable side effects.
            _a2a_async = False
            if source_agent.tenant_id:
                try:
                    from app.models.tenant import Tenant

                    _t_r = await db.execute(select(Tenant).where(Tenant.id == source_agent.tenant_id))
                    _tenant = _t_r.scalar_one_or_none()
                    if _tenant:
                        _a2a_async = getattr(_tenant, "a2a_async_enabled", False)
                except Exception:
                    pass
            if not _a2a_async and not force_async:
                if msg_type in ("notify", "task_delegate"):
                    msg_type = "consult"

            if msg_type == "consult":
                from app.services.active_turns import ensure_active_turn

                await ensure_active_turn(
                    owner_user_id=owner_id,
                    agent_id=target.id,
                    session_id=session_id,
                    turn_type="agent",
                    title=message_text.strip()[:40] or None,
                )

            # Save source message. Ordinary native consults use the same
            # durable admission path as IM/Gateway so a concurrent delivery is
            # merged into the running turn or queued for promotion.
            outbound_a2a_message = recorded_project_outbound
            if (
                outbound_a2a_message is None
                and msg_type == "consult"
                and project_id is None
            ):
                from app.services.chat_history import ingest_incoming_chat_message

                ingested = await ingest_incoming_chat_message(
                    db,
                    session=chat_session,
                    agent_id=session_agent_id,
                    user_id=owner_id,
                    content=message_text,
                    source_channel="agent",
                    provider_event_id=(
                        outbound_operation_key or f"native-consult:{uuid.uuid4()}"
                    ),
                    channel_config_id="native-a2a",
                    actor_ref=str(
                        src_participant.id if src_participant else from_agent_id
                    ),
                    participant_id=(
                        src_participant.id if src_participant else None
                    ),
                    message_meta={
                        "origin_session_id": str(origin_session_id or ""),
                        "origin_source_channel": origin_source_channel,
                        "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
                        "tool_call_id": str(tool_call_id or ""),
                        "execution_agent_id": str(target.id),
                        "target_agent_id": str(target.id),
                        "target_name": target.name,
                    },
                )
                outbound_a2a_message = ingested.message
                outbound_a2a_message.sender_user_id = None
                outbound_a2a_message.sender_agent_id = from_agent_id
                chat_session.last_message_at = datetime.now(timezone.utc)
                if ingested.consumed_by_onmessage:
                    await db.commit()
                    inbox_mode = dict(
                        outbound_a2a_message.message_meta or {}
                    ).get("turn_inbox_mode")
                    return (
                        f"✅ Message to {target.name} was "
                        + (
                            "merged into the current durable conversation turn."
                            if inbox_mode == "current_turn"
                            else "queued for the next durable conversation turn."
                        )
                    )
            elif outbound_a2a_message is None:
                outbound_a2a_message = ChatMessage(
                    id=uuid.uuid4(),
                    agent_id=session_agent_id,
                    user_id=owner_id,
                    sender_agent_id=from_agent_id,
                    role="user",
                    content=message_text,
                    conversation_id=session_id,
                    participant_id=src_participant.id if src_participant else None,
                    external_event_key=outbound_operation_key,
                    message_meta={
                        "direction": "outbound",
                        "source_channel": "agent",
                        "origin_session_id": str(origin_session_id or ""),
                        "origin_source_channel": origin_source_channel,
                        "origin_turn_anchor_id": str(origin_turn_anchor_id or ""),
                        "tool_call_id": str(tool_call_id or ""),
                        "actor_ref": str(tgt_participant.id if tgt_participant else target.id),
                        **(
                            {"execution_agent_id": str(target.id)}
                            if msg_type == "consult" and project_id is None
                            else {}
                        ),
                        "target_agent_id": str(target.id),
                        "target_name": target.name,
                        "work_item_id": str(work_item_id) if work_item_id else None,
                    },
                )
                db.add(outbound_a2a_message)
                chat_session.last_message_at = datetime.now(timezone.utc)
            if msg_type == "consult":
                from app.services.active_turns import commit_current_turn_anchor

                await commit_current_turn_anchor(
                    db.commit,
                    agent_id=session_agent_id,
                    session_id=session_id,
                    message_id=outbound_a2a_message.id,
                )
                if project_id is None:
                    recoverable_anchor = outbound_a2a_message
            else:
                await db.commit()

            # Project A2A must execute in the same durable, project-scoped child
            # runtime as group mentions.  A generic trigger Session has no
            # ProjectMemberSnapshot authority and therefore cannot use project
            # tools.  Keep the exact A2A Session as the visible timeline while
            # waking only the explicitly addressed member child.
            if project_id is not None:
                from app.services.subagent_runtime import enqueue_project_a2a_run

                raw_project_run_id = args.get("_project_run_id") or dict(outbound_a2a_message.message_meta or {}).get(
                    "project_run_id"
                )
                try:
                    scoped_project_run_id = uuid.UUID(str(raw_project_run_id)) if raw_project_run_id else None
                except (TypeError, ValueError):
                    return "❌ _project_run_id must be a complete platform UUID"
                raw_parent_project_run_id = args.get("_parent_project_run_id")
                try:
                    parent_project_run_id = (
                        uuid.UUID(str(raw_parent_project_run_id)) if raw_parent_project_run_id else None
                    )
                except (TypeError, ValueError):
                    return "❌ _parent_project_run_id must be a complete platform UUID"
                try:
                    dispatch_result = await enqueue_project_a2a_run(
                        project_id=project_id,
                        a2a_session_id=chat_session.id,
                        outbound_message_id=outbound_a2a_message.id,
                        from_agent_id=from_agent_id,
                        to_agent_id=target.id,
                        execution_user_id=owner_id,
                        message=message_text,
                        mode=msg_type,
                        project_run_id=scoped_project_run_id,
                        parent_project_run_id=parent_project_run_id,
                        work_item_id=work_item_id,
                        run_title=str(args.get("_run_title") or "").strip() or None,
                    )
                except Exception as exc:
                    logger.exception("[project-a2a] durable target dispatch failed: {}", exc)
                    return "❌ 成员协作请求未能提交，请稍后重试。"
                return json.dumps(
                    {
                        "status": "queued",
                        "message": f"已向 {target.name} 提交成员协作请求",
                        "session_id": session_id,
                        "a2a_session_id": session_id,
                        "project_run_id": str(scoped_project_run_id or dispatch_result.get("project_run_id") or ""),
                        "subagent_run_id": dispatch_result.get("subagent_run_id"),
                        "subagent_session_id": dispatch_result.get("subagent_session_id"),
                        "awakened_agent_ids": [str(target.id)],
                    },
                    ensure_ascii=False,
                )

            # ── notify: fire-and-forget ──
            if msg_type == "notify":
                try:
                    from app.services.activity_logger import log_activity

                    await log_activity(
                        from_agent_id,
                        "agent_msg_sent",
                        f"Sent notification to {target.name}",
                        detail={"partner": target.name, "message": message_text[:200], "msg_type": "notify"},
                    )
                except Exception:
                    pass

                try:
                    await _wake_agent_async(
                        target.id,
                        f"[From {source_name}] {message_text}",
                        from_agent_id=from_agent_id,
                        skip_dedup=True,
                        a2a_session_id=session_id,
                    )
                except Exception as e:
                    logger.warning(f"[A2A] Failed to wake {target.name} for notify: {e}")

                return f"✅ Notification sent to {target.name}. They will process it asynchronously."

            # ── task_delegate: async with callback ──
            if msg_type == "task_delegate":
                try:
                    await _arm_a2a_delegate_callback(
                        from_agent_id=from_agent_id,
                        target=target,
                        message_text=message_text,
                        owner_id=owner_id,
                        origin_session_id=origin_session_id,
                        origin_source_channel=origin_source_channel,
                        origin_external_conv_id=origin_external_conv_id,
                        origin_turn_anchor_id=origin_turn_anchor_id,
                        watch_session_id=session_id,
                        watch_actor_ref=str(tgt_participant.id if tgt_participant else target.id),
                        outbound_message_id=outbound_a2a_message.id,
                    )
                except Exception as e:
                    logger.exception(f"[A2A] Failed to create trigger for delegate: {e}")
                    await db.delete(outbound_a2a_message)
                    await db.commit()
                    return (
                        "❌ The delegated message was recorded, but its reply subscription "
                        "could not be created. The target was not awakened; please retry."
                    )

                try:
                    from app.services.activity_logger import log_activity

                    await log_activity(
                        from_agent_id,
                        "agent_msg_sent",
                        f"Delegated task to {target.name}",
                        detail={"partner": target.name, "message": message_text[:200], "msg_type": "task_delegate"},
                    )
                except Exception:
                    pass

                try:
                    await _wake_agent_async(
                        target.id,
                        f"[From {source_name}] {message_text}",
                        from_agent_id=from_agent_id,
                        skip_dedup=True,
                        a2a_session_id=session_id,
                    )
                except Exception as e:
                    logger.warning(f"[A2A] Failed to wake {target.name} for delegate: {e}")

                return f"✅ Task delegated to {target.name}. You will be notified when they complete it."

            # ── consult (default): synchronous request-response via the UNIFIED loop ──
            # Same loop core (call_llm_with_failover) as web/IM/trigger. A2A specifics:
            #   run model = target agent's model;  store/agent_id of every A2A row = session_agent_id.
            from app.models.agent import DEFAULT_CONTEXT_WINDOW_SIZE
            from app.models.llm import LLMModel
            from app.services.chat_history import (
                load_history_for_llm,
                persist_tool_call,
                strip_leading_orphan_tool_messages,
            )
            from app.services.llm import call_llm_with_failover

            # Resolve target primary + fallback model (skip disabled)
            target_model = None
            if target.primary_model_id:
                _m = await db.execute(select(LLMModel).where(LLMModel.id == target.primary_model_id))
                target_model = _m.scalar_one_or_none()
                if target_model and not target_model.enabled:
                    target_model = None
            target_fallback = None
            if target.fallback_model_id:
                _fb = await db.execute(select(LLMModel).where(LLMModel.id == target.fallback_model_id))
                target_fallback = _fb.scalar_one_or_none()
                if target_fallback and not target_fallback.enabled:
                    target_fallback = None
            if not target_model and target_fallback:
                target_model, target_fallback = target_fallback, None
            if not target_model:
                return f"⚠️ {target.name} has no LLM model configured"

            # 1) The inbound user message is already persisted by the common pre-branch
            #    code (committed at the outer db.commit() above). No second write needed.
            #    Target context is built inside call_llm_with_failover (agent_id=target.id);
            #    the A2A file-delivery guidance rides in A2A_DELIVERY_GUIDANCE
            #    appended to the turn message below (not a separate inline system prompt).

            # 2) Structured history (tool_call rows auto-expand; NO sanitize poisoning)
            ctx_size = target.context_window_size or DEFAULT_CONTEXT_WINDOW_SIZE
            history = await load_history_for_llm(
                db,
                agent_id=session_agent_id,
                conversation_id=session_id,
                ctx_size=ctx_size,
            )
            # The shared loader protects complete recent turns. Never re-slice
            # its expanded assistant/tool pairs by message count.
            messages = strip_leading_orphan_tool_messages(history)
            turn_text = "[From " + source_name + "] " + message_text + "\n\n" + A2A_DELIVERY_GUIDANCE
            if messages and messages[-1].get("role") == "user":
                messages[-1] = {"role": "user", "content": turn_text}
            else:
                messages.append({"role": "user", "content": turn_text})

            from app.services.llm.turn_partition import effective_keep_recent_turns

            protected_keep_recent_turns = effective_keep_recent_turns(
                target_model,
                target_fallback,
            )

            async def _a2a_context_recovery(_recovery_model, dispatch_budget):
                from app.services.chat_history import load_recoverable_history_for_turn
                from app.services.llm.compactor import (
                    COMPACTION_NOT_APPLICABLE_REASONS,
                    ContextRecoveryMessages,
                    maybe_compact,
                )

                compacted = await maybe_compact(
                    agent_id=session_agent_id,
                    conversation_id=session_id,
                    model=_recovery_model,
                    last_prompt_tokens=getattr(dispatch_budget, "authoritative_prompt_tokens", None),
                    current_anchor_id=outbound_a2a_message.id,
                    force_required=getattr(dispatch_budget, "provider_overflow", False),
                    keep_recent_turns_override=(
                        getattr(dispatch_budget, "keep_recent_turns_override", None)
                        if getattr(dispatch_budget, "provider_overflow", False)
                        else protected_keep_recent_turns
                    ),
                )
                preflight_not_applicable = (
                    not compacted.triggered
                    and compacted.skipped_reason
                    in COMPACTION_NOT_APPLICABLE_REASONS
                    and dispatch_budget.fits
                )
                if not compacted.triggered and not preflight_not_applicable:
                    logger.warning(
                        f"[A2A] context recovery could not compact session={session_id}: {compacted.skipped_reason}"
                    )
                    return None
                async with async_session() as recovery_db:
                    recovered = await load_recoverable_history_for_turn(
                        recovery_db,
                        agent_id=session_agent_id,
                        conversation_id=session_id,
                        turn_anchor_id=outbound_a2a_message.id,
                        ctx_size=ctx_size,
                    )
                if not recovered:
                    logger.warning(f"[A2A] context recovery lost latest-anchor race session={session_id}")
                    return None
                return ContextRecoveryMessages(
                    recovered,
                    preflight_not_applicable=preflight_not_applicable,
                )

            # 3) persist callback stores tool calls under session_agent_id, RAW
            async def _a2a_persist(evt: dict):
                await persist_tool_call(
                    async_session,
                    agent_id=session_agent_id,
                    user_id=owner_id,
                    conversation_id=session_id,
                    evt=evt,
                    turn_anchor_id=outbound_a2a_message.id,
                )

            # Collect the target's reasoning/thinking for UI persistence — shown
            # when a human views the A2A session, NEVER fed back into the LLM.
            # Same accumulate-across-rounds pattern as the web chat path.
            _a2a_thinking: list[str] = []

            async def _a2a_on_thinking(text: str):
                _a2a_thinking.append(text)

            from app.services.subagent_runtime import (
                build_parent_subagent_before_round,
            )

            before_round = build_parent_subagent_before_round(
                parent_session_id=session_id,
                active_turn_anchor_id=outbound_a2a_message.id,
                execution_agent_id=target.id,
                execution_user_id=owner_id,
                turn_anchor_agent_id=session_agent_id,
                include_turn_inbox=True,
            )

            # 4) Run target via the unified, failover-aware loop. NO outer wait_for.
            #    agent_id=target.id so build_agent_context loads the right soul/system
            #    prompt; tool calls are stored under session_agent_id via _a2a_persist.
            target_reply = await call_llm_with_failover(
                primary_model=target_model,
                fallback_model=target_fallback,
                messages=messages,
                agent_name=target.name,
                role_description=target.role_description or "",
                agent_id=target.id,
                user_id=owner_id,
                session_id=session_id,
                on_tool_call=_a2a_persist,
                on_thinking=_a2a_on_thinking,
                turn_anchor_id=outbound_a2a_message.id,
                turn_anchor_agent_id=session_agent_id,
                context_recovery=_a2a_context_recovery,
                before_round=before_round,
            )

            if not target_reply:
                return f"⚠️ {target.name} did not respond (LLM returned empty)"

            # Save target reply
            async with async_session() as db2:
                part_r = await db2.execute(
                    select(Participant).where(Participant.type == "agent", Participant.ref_id == target.id)
                )
                tgt_part = part_r.scalar_one_or_none()
                from app.services.chat_history import (
                    persist_assistant_reply_row,
                )

                assistant_message_id = await persist_assistant_reply_row(
                    db2,
                    agent_id=session_agent_id,
                    user_id=owner_id,
                    conversation_id=session_id,
                    content=target_reply,
                    turn_anchor_id=outbound_a2a_message.id,
                    sender_agent_id=target.id,
                    participant_id=tgt_part.id if tgt_part else None,
                    thinking="".join(_a2a_thinking),
                )
                await db2.commit()

            from app.services.conversation_turn_lifecycle import (
                publish_committed_turn_terminal,
            )

            await publish_committed_turn_terminal(
                agent_id=session_agent_id,
                conversation_id=session_id,
                turn_anchor_id=outbound_a2a_message.id,
                message_id=assistant_message_id,
                content=target_reply,
            )

            # Log activity
            from app.services.activity_logger import log_activity

            await log_activity(
                target.id,
                "agent_msg_sent",
                f"Replied to message from {source_name}",
                detail={"partner": source_name, "message": message_text[:200], "reply": target_reply[:200]},
            )
            await log_activity(
                from_agent_id,
                "agent_msg_sent",
                f"Sent message to {target.name} and received reply",
                detail={"partner": target.name, "message": message_text[:200], "reply": target_reply[:200]},
            )

            return f"💬 {target.name} replied:\n{target_reply}"

    except Exception as e:
        from app.services.redis_lease_lock import RedisLeaseError

        if recoverable_anchor is not None and isinstance(e, RedisLeaseError):
            from app.services.turn_inbox import schedule_durable_turn_resume

            schedule_durable_turn_resume(recoverable_anchor)
        logger.exception(f"[A2A] send_message_to_agent failed: from={from_agent_id}, to={args.get('agent_id', '')}")
        error_type = type(e).__name__
        error_detail = (str(e) or "").strip()
        if not error_detail:
            timeout_types = {"ReadTimeout", "ConnectTimeout", "TimeoutException"}
            if error_type in timeout_types:
                error_detail = "LLM request timed out while waiting for target agent response"
            else:
                error_detail = "No detailed error message returned from upstream"
        return f"❌ Message send error ({error_type}): {error_detail[:200]}"


# Plaza Tools — Agent Square social feed
# ═══════════════════════════════════════════════════════

# Plaza Tools — Agent Square social feed
# ═══════════════════════════════════════════════════════


async def _plaza_get_new_posts(agent_id: uuid.UUID, arguments: dict) -> str:
    """Get recent posts from the Agent Plaza, scoped to agent's tenant."""
    from app.models.plaza import PlazaPost, PlazaComment
    from app.models.agent import Agent as AgentModel
    from sqlalchemy import desc

    limit = min(arguments.get("limit", 10), 20)

    try:
        async with async_session() as db:
            # Resolve agent's tenant_id
            ar = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = ar.scalar_one_or_none()
            if not agent:
                return "Error: Agent not found."
            if agent.is_system:
                return "System agents cannot access Plaza."

            if (getattr(agent, "access_mode", None) or "company") != "company":
                return "Only company-wide agents can access Plaza."

            tenant_id = agent.tenant_id if agent else None

            q = select(PlazaPost).order_by(desc(PlazaPost.created_at)).limit(limit)
            if tenant_id:
                q = q.where(PlazaPost.tenant_id == tenant_id)
            result = await db.execute(q)
            posts = result.scalars().all()

            if not posts:
                return "📭 No posts in the plaza yet. Be the first to share something!"

            output = []
            for p in posts:
                # Load comments
                cr = await db.execute(
                    select(PlazaComment).where(PlazaComment.post_id == p.id).order_by(PlazaComment.created_at).limit(5)
                )
                comments = cr.scalars().all()
                icon = "🤖" if p.author_type == "agent" else "👤"
                time_str = p.created_at.strftime("%m-%d %H:%M") if p.created_at else ""
                post_text = f"{icon} **{p.author_name}** ({time_str}) [post_id: {p.id}]\n{p.content}\n❤️ {p.likes_count}  💬 {p.comments_count}"
                if comments:
                    for c in comments:
                        c_icon = "🤖" if c.author_type == "agent" else "👤"
                        post_text += f"\n  └─ {c_icon} {c.author_name}: {c.content}"
                output.append(post_text)

            return "🏛️ Agent Plaza — Recent Posts:\n\n" + "\n\n---\n\n".join(output)

    except Exception as e:
        return f"❌ Failed to load plaza posts: {str(e)[:200]}"


async def _plaza_create_post(agent_id: uuid.UUID, arguments: dict) -> str:
    """Create a new post in the Agent Plaza.

    System agents (is_system=True) are intentionally excluded from Plaza to
    keep the social feed clean — the OKR Agent communicates through Chat and
    reports, not through Plaza posts.
    """
    from app.models.plaza import PlazaPost
    from app.models.agent import Agent as AgentModel

    content = arguments.get("content", "").strip()
    if not content:
        return "Error: Post content cannot be empty."
    if len(content) > 500:
        content = content[:500]

    try:
        async with async_session() as db:
            # Get agent and check is_system
            ar = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = ar.scalar_one_or_none()
            if not agent:
                return "Error: Agent not found."

            # System agents (e.g. OKR Agent) must not post to Plaza
            if agent.is_system:
                return (
                    "System agents are not allowed to post to Plaza. "
                    "Use send_platform_message to communicate with users directly."
                )

            if (getattr(agent, "access_mode", None) or "company") != "company":
                return "Only company-wide agents are allowed to post to Plaza."
            post = PlazaPost(
                author_id=agent_id,
                author_type="agent",
                author_name=agent.name,
                content=content,
                tenant_id=agent.tenant_id,
            )
            db.add(post)
            await db.flush()  # get post.id

            # Extract @mentions
            try:
                import re

                mentions = re.findall(r"@(\S+)", content)
                if mentions:
                    from app.services.notification_service import send_notification

                    a_q = select(AgentModel).where(AgentModel.id != agent_id)
                    if agent.tenant_id:
                        a_q = a_q.where(AgentModel.tenant_id == agent.tenant_id)
                    a_map = {a.name.lower(): a for a in (await db.execute(a_q)).scalars().all()}
                    notified = set()
                    for m in mentions:
                        ma = a_map.get(m.lower())
                        if ma and ma.id not in notified:
                            notified.add(ma.id)
                            await send_notification(
                                db,
                                agent_id=ma.id,
                                type="mention",
                                title=f"{agent.name} mentioned you in a plaza post",
                                body=content[:150],
                                link=f"/plaza?post={post.id}",
                                ref_id=post.id,
                                sender_name=agent.name,
                            )
            except Exception:
                pass

            await db.commit()
            await db.refresh(post)
            return f"Post published! (ID: {post.id})"

    except Exception as e:
        return f"Failed to create post: {str(e)[:200]}"


async def _plaza_add_comment(agent_id: uuid.UUID, arguments: dict) -> str:
    """Add a comment to a plaza post."""
    from app.models.plaza import PlazaPost, PlazaComment
    from app.models.agent import Agent as AgentModel

    post_id = arguments.get("post_id", "")
    content = arguments.get("content", "").strip()
    if not content:
        return "Error: Comment content cannot be empty."
    if len(content) > 300:
        content = content[:300]

    try:
        pid = uuid.UUID(str(post_id))
    except Exception:
        return "Error: Invalid post_id format."

    try:
        async with async_session() as db:
            # Verify post exists
            pr = await db.execute(select(PlazaPost).where(PlazaPost.id == pid))
            post = pr.scalar_one_or_none()
            if not post:
                return "Error: Post not found."

            # Get agent name
            ar = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
            agent = ar.scalar_one_or_none()
            if not agent:
                return "Error: Agent not found."
            if agent.is_system:
                return "System agents are not allowed to comment on Plaza posts."

            if (getattr(agent, "access_mode", None) or "company") != "company":
                return "Only company-wide agents are allowed to comment on Plaza posts."

            comment = PlazaComment(
                post_id=pid,
                author_id=agent_id,
                author_type="agent",
                author_name=agent.name,
                content=content,
            )
            db.add(comment)
            post.comments_count = (post.comments_count or 0) + 1

            # Notify post author (if not self)
            if post.author_id != agent_id:
                try:
                    from app.services.notification_service import send_notification

                    if post.author_type == "agent":
                        await send_notification(
                            db,
                            agent_id=post.author_id,
                            type="plaza_reply",
                            title=f"{agent.name} commented on your post",
                            body=content[:150],
                            link=f"/plaza?post={pid}",
                            ref_id=pid,
                            sender_name=agent.name,
                        )
                        # Also notify human creator
                        pa = (
                            await db.execute(select(AgentModel).where(AgentModel.id == post.author_id))
                        ).scalar_one_or_none()
                        if pa and pa.creator_id:
                            await send_notification(
                                db,
                                user_id=pa.creator_id,
                                type="plaza_comment",
                                title=f"{agent.name} commented on {pa.name}'s post",
                                body=content[:100],
                                link=f"/plaza?post={pid}",
                                ref_id=pid,
                                sender_name=agent.name,
                            )
                    elif post.author_type == "human":
                        await send_notification(
                            db,
                            user_id=post.author_id,
                            type="plaza_reply",
                            title=f"{agent.name} commented on your post",
                            body=content[:150],
                            link=f"/plaza?post={pid}",
                            ref_id=pid,
                            sender_name=agent.name,
                        )
                except Exception:
                    pass

            # Notify other agents who commented on this post
            try:
                from app.services.notification_service import send_notification

                other_crs = await db.execute(
                    select(PlazaComment.author_id, PlazaComment.author_type)
                    .where(PlazaComment.post_id == pid)
                    .distinct()
                )
                notified = {post.author_id, agent_id}
                for row in other_crs.fetchall():
                    cid, ctype = row
                    if cid in notified:
                        continue
                    notified.add(cid)
                    if ctype == "agent":
                        await send_notification(
                            db,
                            agent_id=cid,
                            type="plaza_reply",
                            title=f"{agent.name} also commented on a post you commented on",
                            body=content[:150],
                            link=f"/plaza?post={pid}",
                            ref_id=pid,
                            sender_name=agent.name,
                        )
            except Exception:
                pass

            # Extract @mentions
            try:
                import re

                mentions = re.findall(r"@(\S+)", content)
                if mentions:
                    from app.services.notification_service import send_notification
                    from app.models.user import User

                    # Load agents in tenant
                    a_q = select(AgentModel).where(AgentModel.id != agent_id)
                    if agent.tenant_id:
                        a_q = a_q.where(AgentModel.tenant_id == agent.tenant_id)
                    a_map = {a.name.lower(): a for a in (await db.execute(a_q)).scalars().all()}
                    notified_m = set()
                    for m in mentions:
                        ma = a_map.get(m.lower())
                        if ma and ma.id not in notified_m:
                            notified_m.add(ma.id)
                            await send_notification(
                                db,
                                agent_id=ma.id,
                                type="mention",
                                title=f"{agent.name} mentioned you in a comment",
                                body=content[:150],
                                link=f"/plaza?post={pid}",
                                ref_id=pid,
                                sender_name=agent.name,
                            )
            except Exception:
                pass

            await db.commit()
            return f"Comment added to post by {post.author_name}."

    except Exception as e:
        return f"Failed to add comment: {str(e)[:200]}"


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


async def _get_feishu_token(agent_id: uuid.UUID) -> tuple[str, str] | None:
    """Get (app_id, app_access_token) for the agent's configured Feishu channel."""
    import httpx
    from app.models.channel_config import ChannelConfig

    async with async_session() as db:
        result = await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.channel_type == "feishu",
                ChannelConfig.is_configured == True,
            )
        )
        config = result.scalar_one_or_none()

    if not config or not config.app_id or not config.app_secret:
        return None

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": config.app_id, "app_secret": config.app_secret},
        )
        token = resp.json().get("tenant_access_token", "")

    return (config.app_id, token) if token else None


async def _get_agent_calendar_id(token: str) -> tuple[str | None, str | None]:
    """Get (calendar_id, error_msg) for the agent app's primary calendar.

    Returns (calendar_id, None) on success, or (None, human_readable_error) on failure.
    """
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/calendar/v4/calendars/primary",
            headers={"Authorization": f"Bearer {token}"},
        )
    data = resp.json()
    code = data.get("code", -1)
    if code == 0:
        cals = data.get("data", {}).get("calendars", [])
        if cals:
            cal_id = cals[0].get("calendar", {}).get("calendar_id")
            return cal_id, None
        return None, "日历列表为空，请确认应用有 calendar:calendar 权限并已发布新版本"
    if code == 99991672:
        return None, (
            "❌ 飞书日历权限未开通（错误码 99991672）\n\n"
            "请在飞书开放平台为应用 cli_a9257c5136781ceb 开通以下权限并发布新版本：\n"
            "• calendar:calendar:readonly（应用身份权限）\n"
            "• calendar:calendar.event:create（应用身份权限）\n"
            "• calendar:calendar.event:read（用户身份权限）\n"
            "• calendar:calendar.event:update（用户身份权限）\n"
            "• calendar:calendar.event:delete（用户身份权限）\n\n"
            "开通步骤：飞书开放平台 → 权限管理 → 批量导入权限 → 添加以上权限 → 创建版本 → 确认发布"
        )
    return None, f"获取日历 ID 失败：{data.get('msg')} (code {code})"


async def _feishu_resolve_open_id(token: str, email: str) -> str | None:
    """Resolve a user's open_id from their email."""
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
            json={"emails": [email]},
            headers={"Authorization": f"Bearer {token}"},
            params={"user_id_type": "open_id"},
        )
    data = resp.json()
    if data.get("code") != 0:
        return None
    for u in data.get("data", {}).get("user_list", []):
        oid = u.get("user_id")
        if oid:
            return oid
    return None


def _iso_to_ts(iso_str: str) -> float:
    """Convert ISO 8601 string to Unix timestamp."""
    from datetime import datetime as _dt

    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            if iso_str.endswith("Z"):
                d = _dt.fromisoformat(iso_str.replace("Z", "+00:00"))
            else:
                d = _dt.strptime(iso_str, fmt)
            return d.timestamp()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {iso_str!r}")


async def _get_feishu_credentials(agent_id: uuid.UUID) -> tuple[str, str]:
    """Retrieve Feishu app_id and app_secret for an agent.
    1. Try Agent-specific ChannelConfig
    2. Fallback to global settings (.env)
    """
    from app.models.channel_config import ChannelConfig
    from app.config import get_settings

    settings = get_settings()
    app_id = settings.FEISHU_APP_ID
    app_secret = settings.FEISHU_APP_SECRET

    try:
        async with async_session() as db:
            result = await db.execute(
                select(ChannelConfig).where(ChannelConfig.agent_id == agent_id, ChannelConfig.channel_type == "feishu")
            )
            config = result.scalar_one_or_none()
            if config and config.app_id and config.app_secret:
                app_id = config.app_id
                app_secret = config.app_secret
    except Exception:
        pass

    return app_id, app_secret


async def _get_feishu_tenant_doc_url(tenant_token: str, doc_token: str, doc_type: str = "docx") -> str:
    """Build a user-accessible document URL using the tenant's actual domain.

    The API gateway (open.feishu.cn) cannot serve user documents - we must use
    the tenant's own domain (e.g. xxx.feishu.cn or xxx.larksuite.com).
    Falls back to generating a search link if the tenant domain cannot be resolved.

    Args:
        tenant_token: A valid tenant_access_token.
        doc_token:    The document_id (docx) or wiki node token.
        doc_type:     'docx' or 'wiki' - controls the URL path prefix.
    Returns:
        A fully-formed URL string.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://open.feishu.cn/open-apis/tenant/v2/tenant/query",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
        data = resp.json()
        if data.get("code") == 0:
            domain = data.get("data", {}).get("tenant", {}).get("domain", "")
            if domain:
                return f"https://{domain}/{doc_type}/{doc_token}"
    except Exception:
        pass
    # Fallback: construct a search URL so the user can locate the document
    return f"https://feishu.cn/{doc_type}/{doc_token}"


async def _get_feishu_bitable_url(tenant_token: str, app_token: str, table_id: str = "") -> str:
    """Build a user-accessible Bitable URL using the tenant's actual domain.

    Constructs https://{tenant_domain}/base/{app_token}?table={table_id}
    Falls back to https://feishu.cn/base/{app_token} if domain resolution fails.

    Args:
        tenant_token: A valid tenant_access_token.
        app_token:    The Bitable app token.
        table_id:     Optional table ID to deep-link to a specific sheet.
    Returns:
        A fully-formed URL string.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://open.feishu.cn/open-apis/tenant/v2/tenant/query",
                headers={"Authorization": f"Bearer {tenant_token}"},
            )
        data = resp.json()
        if data.get("code") == 0:
            domain = data.get("data", {}).get("tenant", {}).get("domain", "")
            if domain:
                base_url = f"https://{domain}/base/{app_token}"
                if table_id:
                    base_url += f"?table={table_id}"
                return base_url
    except Exception:
        pass
    # Fallback
    base_url = f"https://feishu.cn/base/{app_token}"
    if table_id:
        base_url += f"?table={table_id}"
    return base_url


def _parse_feishu_url(url: str) -> dict:
    """Parse various Feishu URLs to extract tokens.
    Supports Bitable (table, view) and Docx.
    """
    import re

    result = {}

    # Bitable URL regex: e.g., https://example.feishu.cn/base/{app_token}?table={table_id}&view={view_id}
    base_match = re.search(r"/base/([a-zA-Z0-9_]+)", url)
    if base_match:
        result["app_token"] = base_match.group(1)

    table_match = re.search(r"table=([a-zA-Z0-9_]+)", url)
    if table_match:
        result["table_id"] = table_match.group(1)

    # support URL with /tblxxxxxx
    if not "table_id" in result:
        tbl_match = re.search(r"/(tbl[a-zA-Z0-9_]+)", url)
        if tbl_match:
            result["table_id"] = tbl_match.group(1)

    view_match = re.search(r"view=([a-zA-Z0-9_]+)", url)
    if view_match:
        result["view_id"] = view_match.group(1)

    # Docx URL regex
    docx_match = re.search(r"/docx/([a-zA-Z0-9_]+)", url)
    if docx_match:
        result["document_token"] = docx_match.group(1)

    # Wiki URL regex
    wiki_match = re.search(r"/wiki/([a-zA-Z0-9_]+)", url)
    if wiki_match:
        result["wiki_token"] = wiki_match.group(1)

    return result


# ─── Feishu Bitable Tools ──────────────────────────────────────────


async def _resolve_bitable_app_token(agent_id: uuid.UUID, parsed_url: dict) -> str | None:
    app_token = parsed_url.get("app_token")
    if app_token:
        return app_token
    wiki_token = parsed_url.get("wiki_token")
    if wiki_token:
        app_id, app_secret = await _get_feishu_credentials(agent_id)
        if app_id and app_secret:
            from app.services.feishu_service import feishu_service

            token = await feishu_service.get_tenant_access_token(app_id, app_secret)
            node_info = await _feishu_wiki_get_node(wiki_token, token)
            if node_info and node_info.get("obj_token"):
                return node_info["obj_token"]
    return None


def _check_feishu_err(resp: dict) -> str | None:
    """Check Feishu API response for errors and return a user-friendly message.

    For permission-related errors, returns detailed step-by-step instructions
    guiding the user through the Feishu UI to grant the bot app access to
    the target document or Bitable.
    """
    code = resp.get("code")
    if code != 0:
        msg = str(resp.get("msg", ""))
        msg_lower = msg.lower()
        # Common Feishu permission/access error codes:
        #   99991663 - no permission to access resource
        #   99991661 - token invalid / access denied
        #   99991668 - permission denied on resource
        #   10006    - no permission
        #   91403    - Forbidden (bitable write without app access)
        #   91404    - bitable record not found (sometimes permission)
        #   1063001  - doc permission denied
        #   1063004  - doc operation forbidden
        _perm_codes = {99991663, 10006, 99991661, 99991668, 91403, 1063001, 1063004}
        _perm_keywords = ("permission", "forbidden", "no access", "access denied", "403")
        is_perm_error = code in _perm_codes or any(kw in msg_lower for kw in _perm_keywords)
        if is_perm_error:
            return (
                f"Failed: Permission denied (code: {code}, msg: {msg}). "
                "The bot app does not have access to this document/Bitable. "
                "CRITICAL INSTRUCTION for the Agent: You MUST explicitly output ALL of the following "
                "step-by-step instructions to the user in your reply. Do NOT omit or summarize any step:\n\n"
                "---\n"
                "**The bot needs to be added to this document/table as an authorized app. "
                "Please follow these steps:**\n\n"
                "1. Open the target document or Bitable in the Feishu client (web or desktop).\n"
                "2. Click the **「...」** menu button in the top-right corner of the page.\n"
                "3. In the dropdown menu, hover over **「更多」** (More) at the bottom.\n"
                "4. In the sub-menu that appears, click **「添加文档应用」** (Add Document App).\n"
                "5. In the search box, type the name of your Feishu bot app (the one bound to this Agent's channel), then click to add it.\n"
                "6. After adding, retry the same operation.\n\n"
                "If you cannot find 「添加文档应用」, it means the document owner may need to enable this option, "
                "or you can try: click **「分享」** (Share) button -> invite the bot app directly.\n"
                "---"
            )
        return f"Failed: API Error {code} - {msg}"
    return None


async def _bitable_list_tables(agent_id: uuid.UUID, arguments: dict) -> str:
    """List all tables in a Feishu Bitable app."""
    url = arguments.get("url", "")
    parsed = _parse_feishu_url(url)
    app_token = await _resolve_bitable_app_token(agent_id, parsed)
    if not app_token:
        return "Failed: Could not extract Bitable app_token from the URL (also could not resolve wiki_token)."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.bitable_list_tables(app_id, app_secret, app_token)
        err = _check_feishu_err(resp)
        if err:
            return err

        tables = resp.get("data", {}).get("items", [])
        if not tables:
            return "OK: No tables found in this Bitable."
        lines = [f"- {t.get('name')} (ID: {t.get('table_id')})" for t in tables]
        # Provide a user-accessible link so the user can open the Bitable directly
        tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)
        bitable_url = await _get_feishu_bitable_url(tenant_token, app_token)
        return "OK: Tables in this Bitable:\n" + "\n".join(lines) + f"\n\n🔗 多维表格链接: {bitable_url}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _bitable_create_app(agent_id: uuid.UUID, arguments: dict) -> str:
    """Create a new Feishu Bitable (多维表格) app.

    Calls the Bitable v1 apps API: POST /open-apis/bitable/v1/apps
    The API response includes a user-accessible URL with the tenant's own domain.
    """
    name = arguments.get("name", "").strip()
    if not name:
        return "Failed: Missing required argument 'name' — please provide a name for the new Bitable."

    folder_token = arguments.get("folder_token", "").strip()

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.bitable_create_app(app_id, app_secret, name, folder_token)
        err = _check_feishu_err(resp)
        if err:
            return err

        # API response structure: data.app.{app_token, name, url, default_table_id, folder_token}
        app_info = resp.get("data", {}).get("app", {})
        app_token = app_info.get("app_token", "")
        bitable_url = app_info.get("url", "")
        default_table_id = app_info.get("default_table_id", "")
        if not app_token:
            return f"Failed: Bitable created but could not extract app_token from response: {resp}"

        # Fallback URL resolution if the API didn't return one
        if not bitable_url:
            tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)
            bitable_url = await _get_feishu_bitable_url(tenant_token, app_token)

        result = f"OK: Bitable created successfully!\nName: {name}\nApp Token: {app_token}\nURL: {bitable_url}"
        if default_table_id:
            result += f"\nDefault Table ID: {default_table_id}"
        return result
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _bitable_list_fields(agent_id: uuid.UUID, arguments: dict) -> str:
    """List all fields (columns) in a specific Bitable table."""
    url = arguments.get("url", "")
    table_id = arguments.get("table_id", "")

    parsed = _parse_feishu_url(url)
    app_token = await _resolve_bitable_app_token(agent_id, parsed)
    table_id = table_id or parsed.get("table_id")

    if not app_token:
        return "Failed: Could not extract Bitable app_token from the URL."
    if not table_id:
        return "Failed: table_id is required. Provide it as a parameter or include it in the URL."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.bitable_list_fields(app_id, app_secret, app_token, table_id)
        err = _check_feishu_err(resp)
        if err:
            return err

        fields = resp.get("data", {}).get("items", [])
        if not fields:
            return "OK: No fields found in this table."
        lines = [f"- {f.get('field_name')} (type: {f.get('type')}, ID: {f.get('field_id')})" for f in fields]
        return "OK: Fields in this table:\n" + "\n".join(lines)
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _bitable_query_records(agent_id: uuid.UUID, arguments: dict) -> str:
    """Query records (rows) from a Bitable table, with optional FQL filter."""
    url = arguments.get("url", "")
    table_id = arguments.get("table_id", "")
    filter_info = arguments.get("filter_info", "")
    max_results = arguments.get("max_results", 100)

    parsed = _parse_feishu_url(url)
    app_token = await _resolve_bitable_app_token(agent_id, parsed)
    table_id = table_id or parsed.get("table_id")

    if not app_token or not table_id:
        return "Failed: Could not resolve app_token or table_id from the provided parameters/URL."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    from app.services.feishu_service import feishu_service

    try:
        import json

        filters_dict = {}
        if isinstance(filter_info, dict):
            filters_dict = filter_info
        elif isinstance(filter_info, str) and filter_info.strip():
            try:
                filters_dict = json.loads(filter_info)
            except:
                pass

        resp = await feishu_service.bitable_query_records(app_id, app_secret, app_token, table_id, filters_dict)
        err = _check_feishu_err(resp)
        if err:
            return err

        records = resp.get("data", {}).get("items", [])
        if not records:
            return "OK: No matching records found."

        lines = []
        for r in records[:max_results]:
            lines.append(f"Record {r.get('record_id')}: {json.dumps(r.get('fields', {}), ensure_ascii=False)}")
        return "OK: Query results:\n" + "\n".join(lines)
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _bitable_create_record(agent_id: uuid.UUID, arguments: dict) -> str:
    """Create a new record (row) in a Bitable table."""
    url = arguments.get("url", "")
    table_id = arguments.get("table_id", "")
    fields_str = arguments.get("fields", "{}")

    parsed = _parse_feishu_url(url)
    app_token = await _resolve_bitable_app_token(agent_id, parsed)
    table_id = table_id or parsed.get("table_id")

    if not app_token or not table_id:
        return "Failed: Could not resolve app_token or table_id from the provided parameters/URL."

    import json

    try:
        fields = json.loads(fields_str)
    except json.JSONDecodeError:
        return "Failed: The 'fields' parameter is not valid JSON."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.bitable_create_record(app_id, app_secret, app_token, table_id, fields)
        err = _check_feishu_err(resp)
        if err:
            return err

        record = resp.get("data", {}).get("record", {})
        # Provide a user-accessible link so they can verify the new row in the table
        tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)
        bitable_url = await _get_feishu_bitable_url(tenant_token, app_token, table_id)
        return (
            f"OK: Record created. Record ID: {record.get('record_id')}\n"
            f"Fields: {json.dumps(record.get('fields', {}), ensure_ascii=False)}\n"
            f"🔗 多维表格链接: {bitable_url}"
        )
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _bitable_update_record(agent_id: uuid.UUID, arguments: dict) -> str:
    """Update an existing record in a Bitable table by record_id."""
    url = arguments.get("url", "")
    table_id = arguments.get("table_id", "")
    record_id = arguments.get("record_id", "")
    fields_str = arguments.get("fields", "{}")

    parsed = _parse_feishu_url(url)
    app_token = await _resolve_bitable_app_token(agent_id, parsed)
    table_id = table_id or parsed.get("table_id")

    if not app_token or not table_id or not record_id:
        return "Failed: Missing required parameters. Need app_token (from URL), table_id, and record_id."

    import json

    try:
        fields = json.loads(fields_str)
    except json.JSONDecodeError:
        return "Failed: The 'fields' parameter is not valid JSON."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.bitable_update_record(app_id, app_secret, app_token, table_id, record_id, fields)
        err = _check_feishu_err(resp)
        if err:
            return err

        record = resp.get("data", {}).get("record", {})
        # Provide a user-accessible link so they can verify the updated row
        tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)
        bitable_url = await _get_feishu_bitable_url(tenant_token, app_token, table_id)
        return (
            f"OK: Record updated. Record ID: {record.get('record_id')}\n"
            f"Fields: {json.dumps(record.get('fields', {}), ensure_ascii=False)}\n"
            f"🔗 多维表格链接: {bitable_url}"
        )
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _bitable_delete_record(agent_id: uuid.UUID, arguments: dict) -> str:
    """Delete a record from a Bitable table by record_id."""
    url = arguments.get("url", "")
    table_id = arguments.get("table_id", "")
    record_id = arguments.get("record_id", "")

    parsed = _parse_feishu_url(url)
    app_token = await _resolve_bitable_app_token(agent_id, parsed)
    table_id = table_id or parsed.get("table_id")

    if not app_token or not table_id or not record_id:
        return "Failed: Missing required parameters. Need app_token (from URL), table_id, and record_id."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.bitable_delete_record(app_id, app_secret, app_token, table_id, record_id)
        err = _check_feishu_err(resp)
        if err:
            return err

        # Provide a user-accessible link so they can verify the deletion
        tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)
        bitable_url = await _get_feishu_bitable_url(tenant_token, app_token, table_id)
        return f"OK: Record {record_id} deleted successfully.\n🔗 多维表格链接: {bitable_url}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


# ─── Feishu Document Tools ──────────────────────────────────────────


async def _resolve_docx_document_token(agent_id: uuid.UUID, parsed_url: dict) -> str | None:
    doc_token = parsed_url.get("document_token")
    if doc_token:
        return doc_token
    wiki_token = parsed_url.get("wiki_token")
    if wiki_token:
        app_id, app_secret = await _get_feishu_credentials(agent_id)
        if app_id and app_secret:
            from app.services.feishu_service import feishu_service

            token = await feishu_service.get_tenant_access_token(app_id, app_secret)
            node_info = await _feishu_wiki_get_node(wiki_token, token)
            if node_info and node_info.get("obj_token"):
                return node_info["obj_token"]
    return None


async def _feishu_read_doc(agent_id: uuid.UUID, arguments: dict) -> str:
    """Read full text content of a Feishu Docx."""
    url = arguments.get("url", "")
    parsed = _parse_feishu_url(url)
    doc_token = await _resolve_docx_document_token(agent_id, parsed)
    if not doc_token:
        return "Failed: Could not extract Document token from the URL."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.read_feishu_doc(app_id, app_secret, doc_token)
        err = _check_feishu_err(resp)
        if err:
            return err

        content = resp.get("data", {}).get("content", "")
        if not content:
            return "OK: Document is empty or content is unavailable."
        return f"OK: Document Content:\n{content}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _feishu_create_doc(agent_id: uuid.UUID, arguments: dict) -> str:
    """Create a new blank Feishu Docx."""
    title = arguments.get("title", "Untitled Document")
    folder_token = arguments.get("folder_token", "")

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.create_feishu_doc(app_id, app_secret, folder_token or None, title)
        err = _check_feishu_err(resp)
        if err:
            return err

        doc = resp.get("data", {}).get("document", {})
        doc_id = doc.get("document_id")
        # Get the tenant's actual domain (open.feishu.cn is the API gateway, not for users)
        tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)
        url = await _get_feishu_tenant_doc_url(tenant_token, doc_id)
        return f"OK: Document created perfectly. Document ID: {doc_id}\nURL: {url}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _feishu_append_doc(agent_id: uuid.UUID, arguments: dict) -> str:
    """Append text to the bottom of a Feishu Docx."""
    url = arguments.get("url", "")
    content = arguments.get("content", "")
    if not content:
        return "Failed: Content to append cannot be empty."

    parsed = _parse_feishu_url(url)
    doc_token = await _resolve_docx_document_token(agent_id, parsed)
    if not doc_token:
        return "Failed: Could not extract Document token from the URL."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    try:
        # Feishu uses the document_id as the root block_id to append entirely to the document
        resp = await feishu_service.append_feishu_doc(app_id, app_secret, doc_token, content)
        err = _check_feishu_err(resp)
        if err:
            return err

        return "OK: Content appended successfully to the end of the document."
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


# ─── Feishu Wiki Tools ───────────────────────────────────────────────────────


async def _feishu_wiki_get_node(token_str: str, auth_token: str) -> dict | None:
    """Call wiki get_node API to resolve a wiki node token → {obj_token, space_id, has_child, title}.
    Returns None if the token is not a wiki node."""
    import httpx

    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(
            "https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node",
            headers={"Authorization": f"Bearer {auth_token}"},
            params={"token": token_str, "obj_type": "wiki"},
        )
    d = r.json()
    if d.get("code") != 0:
        return None
    node = d.get("data", {}).get("node", {})
    return {
        "obj_token": node.get("obj_token", ""),
        "space_id": node.get("origin_space_id", node.get("space_id", "")),
        "has_child": node.get("has_child", False),
        "title": node.get("title", ""),
        "node_token": node.get("node_token", token_str),
    }


async def _feishu_doc_search(agent_id: uuid.UUID, arguments: dict) -> str:
    """Search Feishu documents by keyword using the official document search API."""
    import httpx

    query = (arguments.get("query") or arguments.get("search_key") or "").strip()
    if not query:
        return "❌ Missing required argument 'query'"

    count = max(1, min(int(arguments.get("count", 10)), 50))
    offset = max(0, int(arguments.get("offset", 0)))
    docs_types = arguments.get("docs_types") or []
    if docs_types and not isinstance(docs_types, list):
        return "❌ 'docs_types' must be an array of strings."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(app_id, app_secret)
    payload: dict[str, object] = {
        "search_key": query,
        "count": count,
        "offset": offset,
    }
    if docs_types:
        payload["docs_types"] = docs_types

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://open.feishu.cn/open-apis/suite/docs-api/search/object",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    data = resp.json()
    err = _check_feishu_err(data)
    if err:
        return err

    result = data.get("data", {})
    entities = result.get("docs_entities", []) or []
    total = result.get("total", len(entities))
    has_more = bool(result.get("has_more", False))
    if not entities:
        return (
            f"🔎 未找到与 `{query}` 匹配的飞书文档。"
            "\n可以尝试："
            "\n1. 缩短关键词"
            "\n2. 换同义词"
            "\n3. 指定 docs_types 过滤，例如 ['docx'] 或 ['bitable']"
        )

    lines = [
        f"🔎 飞书文档搜索结果：关键词 `{query}`",
        f"返回 {len(entities)} 条，total={total}，offset={offset}，has_more={str(has_more).lower()}",
        "",
    ]
    for idx, item in enumerate(entities, start=offset + 1):
        title = item.get("title") or "(无标题)"
        docs_token = item.get("docs_token") or ""
        docs_type = item.get("docs_type") or "unknown"
        owner_id = item.get("owner_id") or ""
        lines.append(
            f"{idx}. **{title}**\n"
            f"   - docs_type: `{docs_type}`\n"
            f"   - docs_token: `{docs_token}`\n"
            f"   - owner_id: `{owner_id}`"
        )

    lines.append("")
    lines.append("💡 后续操作建议：")
    lines.append('- 读取普通文档/知识库页：`feishu_doc_read(document_token="...")`')
    lines.append('- 管理权限：`feishu_drive_share(document_token="...", doc_type="...", action="list|add|remove")`')
    lines.append('- 删除文件：`feishu_drive_delete(file_token="...", file_type="...")`')
    if has_more:
        lines.append(f'- 下一页：`feishu_doc_search(query="{query}", offset={offset + len(entities)}, count={count})`')

    return "\n".join(lines)


async def _feishu_wiki_list(agent_id: uuid.UUID, arguments: dict) -> str:
    """List sub-pages of a Feishu Wiki node, optionally recursive."""
    import httpx

    node_token = (arguments.get("node_token") or "").strip()
    recursive = bool(arguments.get("recursive", False))

    if not node_token:
        return "❌ Missing required argument 'node_token'"

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(app_id, app_secret)
    headers = {"Authorization": f"Bearer {token}"}

    # Resolve node → space_id
    node_info = await _feishu_wiki_get_node(node_token, token)
    if not node_info:
        return (
            f"❌ 无法解析 Wiki 节点 `{node_token}`。\n"
            "请确认 token 来自飞书知识库 URL（https://xxx.feishu.cn/wiki/NodeToken），"
            "而非普通文档 URL。"
        )

    space_id = node_info["space_id"]
    if not space_id:
        return f"❌ 无法获取知识库 space_id，请检查 token 是否正确。"

    async def _list_children(parent_token: str, depth: int) -> list[dict]:
        """Return flat list of {title, node_token, obj_token, has_child, depth}."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/nodes",
                headers=headers,
                params={"parent_node_token": parent_token, "page_size": 50},
            )
        data = resp.json()
        if data.get("code") != 0:
            return []
        items = data.get("data", {}).get("items", [])
        result = []
        for item in items:
            entry = {
                "title": item.get("title", "(无标题)"),
                "node_token": item.get("node_token", ""),
                "obj_token": item.get("obj_token", ""),
                "has_child": item.get("has_child", False),
                "depth": depth,
            }
            result.append(entry)
            if recursive and entry["has_child"] and depth < 2:
                children = await _list_children(entry["node_token"], depth + 1)
                result.extend(children)
        return result

    pages = await _list_children(node_token, 0)
    if not pages:
        return f"📂 Wiki 页面 `{node_token}` 下没有子页面。"

    lines = [f"📂 Wiki 页面 `{node_token}` 的子页面（共 {len(pages)} 个）：\nspace_id: `{space_id}`\n"]
    for p in pages:
        indent = "  " * p["depth"]
        child_hint = " _(有子页面)_" if p["has_child"] else ""
        lines.append(
            f"{indent}• **{p['title']}**{child_hint}\n"
            f"{indent}  node_token: `{p['node_token']}`\n"
            f"{indent}  obj_token: `{p['obj_token']}`"
        )
    lines.append(
        '\n💡 用 `feishu_doc_read(document_token="<node_token>")` 读取每个子页面的内容。'
        '\n   对有子页面的条目，再次调用 `feishu_wiki_list(node_token="...")` 继续展开。'
    )
    return "\n".join(lines)


async def _feishu_doc_read(agent_id: uuid.UUID, arguments: dict) -> str:
    document_token = arguments.get("document_token", "").strip()
    if not document_token:
        url = arguments.get("url", "")
        parsed = _parse_feishu_url(url)
        document_token = parsed.get("document_token", parsed.get("wiki_token", ""))

    if not document_token:
        return "Failed: Missing required argument 'document_token'"
    max_chars = min(int(arguments.get("max_chars", 6000)), 20000)

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    read_token = document_token
    wiki_hint = ""
    node_info = await _feishu_wiki_get_node(document_token, tenant_token)
    if node_info and node_info.get("obj_token"):
        read_token = node_info["obj_token"]
        if node_info.get("has_child"):
            wiki_hint = (
                "\n\n> 💡 这是一个 Wiki 目录页，它有多个子页面。"
                "使用 `feishu_wiki_list` 工具（传入相同的 node_token）可以查看所有子页面列表。"
            )

    try:
        resp = await feishu_service.read_feishu_doc(app_id, app_secret, read_token)
        err = _check_feishu_err(resp)
        if err:
            return err

        content = resp.get("data", {}).get("content", "")
        if not content:
            return f"📄 Document '{document_token}' is empty.{wiki_hint}"

        truncated = ""
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = f"\n\n_(Truncated to {max_chars} chars)_"

        return f"📄 **Document content** (`{document_token}`):\n\n{content}{truncated}{wiki_hint}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _feishu_doc_create(agent_id: uuid.UUID, arguments: dict) -> str:
    title = arguments.get("title", "").strip()
    if not title:
        return "Failed: Missing required argument 'title'"

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    folder_token = (arguments.get("folder_token") or "").strip()
    wiki_space_id = (arguments.get("wiki_space_id") or "").strip()
    parent_node_token = (arguments.get("parent_node_token") or "").strip()

    from app.services.feishu_service import feishu_service

    tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    try:
        import httpx

        # ── Smart fallback: if folder_token is actually a wiki node token,
        #    auto-redirect to wiki creation branch. This handles LLMs that
        #    pass the wiki node token via the old folder_token param.
        if folder_token and not wiki_space_id and not parent_node_token:
            probe = await _feishu_wiki_get_node(folder_token, tenant_token)
            if probe and probe.get("space_id"):
                wiki_space_id = probe["space_id"]
                parent_node_token = probe.get("node_token", folder_token)
                folder_token = ""  # Don't use as Drive folder

        # ── Wiki branch: create as a wiki node ──────────────────────────
        # If parent_node_token is given but wiki_space_id is not,
        # resolve space_id from the parent node automatically.
        if parent_node_token and not wiki_space_id:
            node_info = await _feishu_wiki_get_node(parent_node_token, tenant_token)
            if node_info and node_info.get("space_id"):
                wiki_space_id = node_info["space_id"]

        if wiki_space_id:
            body: dict = {
                "obj_type": "docx",
                "node_type": "origin",  # Required by Feishu Wiki API: "origin" = new entity
                "title": title,
            }
            if parent_node_token:
                body["parent_node_token"] = parent_node_token

            import logging

            _wiki_log = logging.getLogger("feishu_wiki_create")
            _wiki_log.info(f"Creating wiki node in space={wiki_space_id}, body={body}")

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{wiki_space_id}/nodes",
                    json=body,
                    headers={"Authorization": f"Bearer {tenant_token}"},
                )
            result = resp.json()
            _wiki_log.info(f"Wiki create response: code={result.get('code')}, msg={result.get('msg')}")
            err = _check_feishu_err(result)
            if err:
                return err

            node = result.get("data", {}).get("node", {})
            # obj_token is the underlying docx token used by feishu_doc_append
            doc_token = node.get("obj_token", "")
            node_token = node.get("node_token", "")
            # Wiki docs are accessed via /wiki/{node_token}, not /docx/{obj_token}
            doc_url = await _get_feishu_tenant_doc_url(tenant_token, node_token, doc_type="wiki")

            return (
                f"✅ 知识库文档创建成功！\n"
                f"标题：{title}\n"
                f"文档 Token（用于 feishu_doc_append）：{doc_token}\n"
                f"Wiki Node Token：{node_token}\n"
                f"🔗 访问链接：{doc_url}\n"
                f'下一步：调用 feishu_doc_append(document_token="{doc_token}", content="...") 写入正文内容。'
            )

        # ── Regular Drive branch (original behavior) ─────────────────────
        resp = await feishu_service.create_feishu_doc(app_id, app_secret, folder_token, title)
        err = _check_feishu_err(resp)
        if err:
            return err

        doc = resp.get("data", {}).get("document", {})
        doc_token = doc.get("document_id", "")
        doc_url = await _get_feishu_tenant_doc_url(tenant_token, doc_token)

        # Auto-share with the Feishu sender so they can access the document.
        # channel_feishu_sender_open_id is a module-level ContextVar defined in this file;
        # no import needed — it is already in scope.
        share_note = ""
        try:
            sender_open_id = channel_feishu_sender_open_id.get(None)
            if sender_open_id and doc_token:
                async with httpx.AsyncClient(timeout=10) as client:
                    share_resp = await client.post(
                        f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_token}/members",
                        params={"type": "docx"},
                        json={
                            "member_type": "openid",
                            "member_id": sender_open_id,
                            "perm": "full_access",
                        },
                        headers={"Authorization": f"Bearer {tenant_token}"},
                    )
                sr = share_resp.json()
                if sr.get("code") == 0:
                    share_note = "\n✅ 已自动为你开通访问权限。"
                else:
                    share_note = f"\n⚠️ 自动授权失败（{sr.get('code')}），你可能需要手动在飞书前端搜索此文件。"
        except Exception as _e:
            share_note = f"\n⚠️ 自动授权异常: {_e}"

        return (
            f"✅ 文档创建成功！{share_note}\n"
            f"标题：{title}\n"
            f"Token：{doc_token}\n"
            f"🔗 访问链接：{doc_url}\n"
            f'下一步：调用 feishu_doc_append(document_token="{doc_token}", content="...") 写入正文内容。'
        )
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


def _parse_inline_markdown(text: str) -> list[dict]:
    """Parse inline markdown (bold, italic, strikethrough) into Feishu text_run elements.
    Note: inline `code` is deliberately NOT rendered as inline_code style because
    Feishu's API rejects inline_code inside heading blocks (field validation error).
    Instead, backtick-wrapped text is returned as plain text.
    Empty text_element_style dicts are intentionally omitted to avoid API validation errors.
    """
    import re as _re

    def _make_run(content: str, style: dict | None = None) -> dict:
        run: dict = {"content": content}
        if style:
            run["text_element_style"] = style
        return {"text_run": run}

    elements = []
    # Only handle **bold**, *italic*, ~~strikethrough~~; backticks become plain text
    pattern = r"(\*\*(.+?)\*\*|\*(.+?)\*|~~(.+?)~~|`(.+?)`)"
    pos = 0
    for m in _re.finditer(pattern, text):
        if m.start() > pos:
            elements.append(_make_run(text[pos : m.start()]))
        raw = m.group(0)
        if raw.startswith("**"):
            elements.append(_make_run(m.group(2), {"bold": True}))
        elif raw.startswith("~~"):
            elements.append(_make_run(m.group(4), {"strikethrough": True}))
        elif raw.startswith("`"):
            # Render as plain text to avoid inline_code validation issues in headings
            elements.append(_make_run(m.group(5)))
        else:
            elements.append(_make_run(m.group(3), {"italic": True}))
        pos = m.end()
    if pos < len(text):
        elements.append(_make_run(text[pos:]))
    if not elements:
        elements.append(_make_run(text or " "))
    return elements


def _markdown_to_feishu_blocks(markdown: str) -> list[dict]:
    """Convert Markdown text to Feishu docx v1 block list.

    Supported:
      # / ## / ### / ####  → heading1-4 (block_type 3-6)
      - / * / + text       → bullet      (block_type 12)
      1. text              → ordered     (block_type 13)
      > text               → quote       (block_type 15)
      --- / ***            → divider     (block_type 22)
      ``` ... ```          → code block  (block_type 14)
      plain text           → text        (block_type 2)
      inline **bold** *italic* `code` ~~strike~~  → text_element_style
    """
    import re as _re

    _HEADING_BLOCK = {1: (3, "heading1"), 2: (4, "heading2"), 3: (5, "heading3"), 4: (6, "heading4")}

    def _text_block(bt: int, key: str, line: str) -> dict:
        # Omit "style" entirely to avoid Feishu field validation errors on empty style dicts
        return {
            "block_type": bt,
            key: {"elements": _parse_inline_markdown(line)},
        }

    blocks: list[dict] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        # ── Code fence ──────────────────────────────────────────────────────
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            blocks.append(
                {
                    "block_type": 14,
                    "code": {
                        "elements": [{"text_run": {"content": "\n".join(code_lines)}}],
                        "style": {
                            "language": 1
                            if not lang
                            else {
                                "python": 49,
                                "javascript": 22,
                                "js": 22,
                                "typescript": 56,
                                "ts": 56,
                                "bash": 4,
                                "sh": 4,
                                "sql": 53,
                                "java": 21,
                                "go": 17,
                                "rust": 51,
                                "json": 25,
                                "yaml": 60,
                                "html": 19,
                                "css": 10,
                            }.get(lang.lower(), 1)
                        },
                    },
                }
            )
            i += 1
            continue

        # ── Divider ──────────────────────────────────────────────────────────
        if _re.fullmatch(r"[-*_]{3,}", line.strip()):
            # NOTE: block_type 22 (Feishu native divider) is rejected by the batch children
            # creation API with error 99992402 (field validation failed).  Render as a plain
            # text block containing a visual em-dash separator instead — always accepted.
            blocks.append(
                {
                    "block_type": 2,
                    "text": {"elements": [{"text_run": {"content": "\u2500" * 24}}]},
                }
            )
            i += 1
            continue

        # ── Headings ─────────────────────────────────────────────────────────
        hm = _re.match(r"^(#{1,4})\s+(.*)", line)
        if hm:
            level = min(len(hm.group(1)), 4)
            bt, key = _HEADING_BLOCK[level]
            blocks.append(_text_block(bt, key, hm.group(2)))
            i += 1
            continue

        # ── Bullet list ──────────────────────────────────────────────────────
        if _re.match(r"^[\-\*\+]\s+", line):
            text = _re.sub(r"^[\-\*\+]\s+", "", line)
            blocks.append(_text_block(12, "bullet", text))
            i += 1
            continue

        # ── Ordered list ─────────────────────────────────────────────────────
        if _re.match(r"^\d+\.\s+", line):
            text = _re.sub(r"^\d+\.\s+", "", line)
            blocks.append(_text_block(13, "ordered", text))
            i += 1
            continue

        # ── Blockquote ───────────────────────────────────────────────────────
        if line.startswith("> "):
            blocks.append(_text_block(15, "quote", line[2:]))
            i += 1
            continue

        # ── Empty line → empty text block ────────────────────────────────────
        if line.strip() == "":
            blocks.append(
                {
                    "block_type": 2,
                    "text": {"elements": [{"text_run": {"content": " "}}]},
                }
            )
            i += 1
            continue

        # ── Markdown table separator line (|---|---| ) → skip ───────────────
        if _re.match(r"^\|[\s\-:]+(\|[\s\-:]+)*\|?\s*$", line.strip()):
            i += 1
            continue

        # ── Markdown table row → plain text ──────────────────────────────────
        if line.strip().startswith("|") and line.strip().endswith("|"):
            # Strip pipe separators and render each cell as plain text
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            cell_text = "  |  ".join(c for c in cells if c)
            blocks.append(_text_block(2, "text", cell_text))
            i += 1
            continue

        # ── Plain text (with inline formatting) ──────────────────────────────
        blocks.append(_text_block(2, "text", line))
        i += 1

    return blocks


async def _feishu_doc_append(agent_id: uuid.UUID, arguments: dict) -> str:
    document_token = arguments.get("document_token", "").strip()
    if not document_token:
        url = arguments.get("url", "")
        parsed = _parse_feishu_url(url)
        document_token = parsed.get("document_token", parsed.get("wiki_token", ""))

    content = arguments.get("content", "").strip()
    if not document_token:
        return "Failed: Missing required argument 'document_token'"
    if not content:
        return "Failed: Missing required argument 'content'"

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "Failed: Feishu app credentials not configured for this agent."

    from app.services.feishu_service import feishu_service

    tenant_token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    # For wiki node tokens, use the obj_token for the docx API
    node_info = await _feishu_wiki_get_node(document_token, tenant_token)
    docx_token = node_info["obj_token"] if (node_info and node_info.get("obj_token")) else document_token

    try:
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            meta_resp = (
                await client.get(
                    f"https://open.feishu.cn/open-apis/docx/v1/documents/{docx_token}",
                    headers={"Authorization": f"Bearer {tenant_token}"},
                )
            ).json()
            err = _check_feishu_err(meta_resp)
            if err:
                return err

            body_block_id = meta_resp.get("data", {}).get("document", {}).get("body", {}).get("block_id") or docx_token

            children = _markdown_to_feishu_blocks(content)

            result = (
                await client.post(
                    f"https://open.feishu.cn/open-apis/docx/v1/documents/{docx_token}/blocks/{body_block_id}/children",
                    # Do NOT pass index: -1.  Omitting the field lets Feishu default to
                    # append-at-end, which is always valid.  Passing -1 explicitly can
                    # trigger error 1770001 (invalid param) with certain block type mixes.
                    json={"children": children},
                    headers={"Authorization": f"Bearer {tenant_token}"},
                )
            ).json()

            err = _check_feishu_err(result)
            if err:
                return err

        doc_url = await _get_feishu_tenant_doc_url(tenant_token, docx_token)
        return f"✅ 已写入 {len(children)} 个段落到文档。\n🔗 文档直链（原文发给用户，勿修改）：{doc_url}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


# ─── Feishu Drive Share (All File Types) ────────────────────────────────────────


async def _feishu_drive_share(agent_id: uuid.UUID, arguments: dict) -> str:
    """Manage Feishu drive file collaborators.
    Automatically handles both regular docs/files (Drive permissions API)
    and Wiki node documents (Wiki space members API).
    """
    import httpx
    import re as _re

    document_token = (arguments.get("document_token") or "").strip()
    doc_type = (arguments.get("doc_type") or "docx").strip()
    action = (arguments.get("action") or "list").strip()
    permission = (arguments.get("permission") or "edit").strip()

    if not document_token:
        return "❌ Missing required argument 'document_token'"

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(app_id, app_secret)
    headers = {"Authorization": f"Bearer {token}"}

    # ── Detect if this is a Wiki node token ─────────────────────────────────
    node_info = await _feishu_wiki_get_node(document_token, token)
    is_wiki = node_info is not None
    space_id = node_info.get("space_id", "") if node_info else ""
    obj_token = node_info.get("obj_token", "") if node_info else ""

    # Permission level mapping: Feishu API uses "view" / "edit" / "full_access"
    api_perm = {"view": "view", "edit": "edit", "full_access": "full_access"}.get(permission, "edit")
    # Wiki space role mapping: only "admin" / "member" are valid roles
    wiki_role = "admin" if api_perm in ("edit", "full_access") else "member"

    # ── LIST collaborators ────────────────────────────────────────────────────
    if action == "list":
        use_token = obj_token if (is_wiki and obj_token) else document_token
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://open.feishu.cn/open-apis/drive/v1/permissions/{use_token}/members",
                params={"type": doc_type},
                headers=headers,
            )
        data = resp.json()
        if data.get("code") != 0:
            _c = data.get("code")
            if _c == 1063003 and is_wiki:
                return (
                    f"ℹ️ 文档 `{document_token}` 是知识库页面，其权限由知识库空间统一管理。\n"
                    "知识库空间 ID：`" + space_id + "`\n"
                    "请直接在飞书知识库中管理成员权限。"
                )
            if _c in (99991672, 99991668):
                return f"❌ 权限不足（code {_c}）\n需要在飞书开放平台开通：\n• drive:drive（云文档权限管理）"
            return f"❌ 获取协作者列表失败：{data.get('msg')} (code {_c})"

        members = data.get("data", {}).get("items", [])
        if not members:
            return f"📄 文档 `{document_token}` 当前没有其他协作者。"

        provider_ids = [
            str(m.get("member_id") or "") for m in members if m.get("member_type") == "openid" and m.get("member_id")
        ]
        identity_map: dict[str, tuple[str, str]] = {}
        if provider_ids:
            async with async_session() as identity_db:
                agent_result = await identity_db.execute(select(AgentModel).where(AgentModel.id == agent_id))
                source_agent = agent_result.scalar_one_or_none()
                if source_agent:
                    rows = await identity_db.execute(
                        select(OrgMember, UserModel)
                        .join(UserModel, OrgMember.user_id == UserModel.id)
                        .where(
                            OrgMember.tenant_id == source_agent.tenant_id,
                            OrgMember.open_id.in_(provider_ids),
                        )
                    )
                    for member, user in rows.all():
                        identity_map[str(member.open_id)] = (str(user.id), user.display_name)
        lines = [f"📄 文档 `{document_token}` 的协作者列表（共 {len(members)} 人）：\n"]
        for m in members:
            perm = m.get("perm", "")
            member_type = m.get("member_type", "")
            member_id = m.get("member_id", "")
            canonical = identity_map.get(str(member_id))
            if member_type == "openid" and canonical:
                user_id, display_name = canonical
                lines.append(f"• {display_name} | user_id: `{user_id}` | 权限: **{perm}**")
            else:
                lines.append(f"• 非用户协作者或未映射用户 | 权限: **{perm}**")
        return "\n".join(lines)

    # ── ADD / REMOVE collaborators ─────────────────────────────────────────────
    user_ids: list[str] = list(arguments.get("user_ids") or [])
    if not user_ids:
        return "❌ 请提供 canonical user_ids"

    resolved: list[tuple[str, str]] = []  # (display_name, open_id)
    async with async_session() as identity_db:
        for canonical_user_id in user_ids:
            try:
                route = await resolve_human_channel_recipient(
                    identity_db, agent_id, canonical_user_id, channel="feishu"
                )
            except RecipientResolutionError:
                resolved.append((str(canonical_user_id), ""))
                continue
            open_id = str(route.member.open_id or "")
            resolved.append((route.user.display_name, open_id))

    results = []
    async with httpx.AsyncClient(timeout=15) as client:
        for display, oid in resolved:
            if not oid:
                results.append(f"❌ 无法将 canonical user_id「{display}」解析到唯一 Feishu open_id，跳过")
                continue

            if action == "add":
                # ── Wiki node: use wiki space members API ──────────────────
                if is_wiki and space_id:
                    resp = await client.post(
                        f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/members",
                        json={"member_type": "openid", "member_id": oid, "member_role": wiki_role},
                        headers=headers,
                    )
                    d = resp.json()
                    _c = d.get("code")
                    if _c == 0:
                        results.append(f"✅ 已将「{display}」加入知识库空间（角色：{wiki_role}）")
                    elif _c == 131008:
                        results.append(f"ℹ️ 「{display}」已经是知识库成员，无需重复添加")
                    elif _c == 131101:
                        # Public wiki space — everyone already has access
                        results.append(f"ℹ️ 这是一个**公开知识库**，所有人已可访问。\n「{display}」无需单独添加权限。")
                    else:
                        results.append(f"❌ 添加「{display}」到知识库失败：{d.get('msg')} (code {_c})")
                    continue

                # ── Regular docx: use Drive permissions API ────────────────
                body = {
                    "member_type": "openid",
                    "member_id": oid,
                    "perm": api_perm,
                }
                resp = await client.post(
                    f"https://open.feishu.cn/open-apis/drive/v1/permissions/{document_token}/members",
                    json=body,
                    headers=headers,
                    params={"type": doc_type},
                )
                d = resp.json()
                if d.get("code") == 0:
                    results.append(f"✅ 已将「{display}」添加为**{permission}**权限协作者")
                else:
                    _c = d.get("code")
                    if _c == 99992402:
                        # Feishu platform policy: you cannot add yourself as a collaborator via API.
                        # Permissions must be granted by others, or set manually in the UI.
                        results.append(
                            f"⚠️ 飞书平台安全限制：无法通过 API 为自己添加协作权限。\n"
                            f"请手动操作：打开文档 → 右上角「分享」→ 添加自己并设置权限。"
                        )
                    elif _c in (99991672, 99991668):
                        return f"❌ 权限不足（code {_c}）\n需要在飞书开放平台开通：\n• drive:drive（云文档权限管理）"
                    else:
                        results.append(f"❌ 添加「{display}」失败：{d.get('msg')} (code {_c})")

            elif action == "remove":
                if is_wiki and space_id:
                    resp = await client.delete(
                        f"https://open.feishu.cn/open-apis/wiki/v2/spaces/{space_id}/members/{oid}",
                        headers=headers,
                        params={"member_type": "openid"},
                    )
                    d = resp.json()
                    if d.get("code") == 0:
                        results.append(f"✅ 已将「{display}」从知识库移除")
                    else:
                        results.append(f"❌ 移除「{display}」失败：{d.get('msg')} (code {d.get('code')})")
                    continue

                resp = await client.delete(
                    f"https://open.feishu.cn/open-apis/drive/v1/permissions/{document_token}/members/{oid}",
                    headers=headers,
                    params={"type": doc_type, "member_type": "openid"},
                )
                d = resp.json()
                if d.get("code") == 0:
                    results.append(f"✅ 已移除「{display}」的协作权限")
                else:
                    results.append(f"❌ 移除「{display}」失败：{d.get('msg')} (code {d.get('code')})")

    return "\n".join(results) if results else "没有需要处理的成员"


# ─── Feishu Drive Delete ──────────────────────────────────────────────────────


async def _feishu_drive_delete(agent_id: uuid.UUID, arguments: dict) -> str:
    """Delete a file or folder from Feishu Drive (cloud space).
    The file is moved to the recycle bin, not permanently deleted.
    For folders, the deletion is asynchronous and returns a task_id.
    """
    import httpx

    file_token = (arguments.get("file_token") or "").strip()
    file_type = (arguments.get("file_type") or "").strip()

    if not file_token:
        return "❌ Missing required argument 'file_token'"
    if not file_type:
        return "❌ Missing required argument 'file_type'. Valid values: file, docx, bitable, folder, doc, sheet, mindnote, shortcut, slides"

    valid_types = {"file", "docx", "bitable", "folder", "doc", "sheet", "mindnote", "shortcut", "slides"}
    if file_type not in valid_types:
        return f"❌ Invalid file_type '{file_type}'. Valid values: {', '.join(sorted(valid_types))}"

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    # Type label mapping for user-friendly output
    type_labels = {
        "file": "文件",
        "docx": "文档",
        "bitable": "多维表格",
        "folder": "文件夹",
        "doc": "旧版文档",
        "sheet": "电子表格",
        "mindnote": "思维笔记",
        "shortcut": "快捷方式",
        "slides": "幻灯片",
    }
    type_label = type_labels.get(file_type, file_type)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.delete(
                f"https://open.feishu.cn/open-apis/drive/v1/files/{file_token}",
                params={"type": file_type},
                headers={"Authorization": f"Bearer {token}"},
            )
        data = resp.json()
        code = data.get("code", -1)

        if code == 0:
            # Folder deletion returns a task_id for async tracking
            task_id = data.get("data", {}).get("task_id")
            if task_id:
                return (
                    f"✅ 已提交{type_label}删除任务（异步执行中）。\n"
                    f"📋 任务 ID: `{task_id}`\n"
                    f"文件夹删除为异步操作，文件会被移至回收站。"
                )
            return f"✅ {type_label} `{file_token}` 已删除（移至回收站）。"

        # Error handling with specific codes
        msg = data.get("msg", "Unknown error")
        if code == 1061003:
            return f"❌ 未找到文件 `{file_token}`。请确认文件 token 和类型是否正确。"
        elif code == 1061004:
            return (
                f"❌ 权限不足（code {code}）\n"
                "需要满足以下条件之一：\n"
                "• 文件所有者 + 父文件夹编辑权限\n"
                "• 父文件夹的所有者或 full_access 权限\n"
                "同时需要在飞书开放平台开通：drive:drive 或 space:document:delete"
            )
        elif code == 1061007:
            return f"❌ 文件 `{file_token}` 已被删除。"
        elif code == 1061045:
            return f"⚠️ 接口频率限制，请稍后重试。（每秒最多 5 次）"
        else:
            return f"❌ 删除{type_label}失败：{msg} (code {code})"

    except Exception as e:
        return f"❌ 删除文件异常: {str(e)[:300]}"


# ─── Feishu Calendar Tools ────────────────────────────────────────────────────


async def _resolve_feishu_open_id(
    agent_id: uuid.UUID,
    canonical_user_id: object,
) -> tuple[str, str]:
    """Resolve a public canonical user_id to one internal Feishu open_id."""
    async with async_session() as db:
        route = await resolve_human_channel_recipient(db, agent_id, canonical_user_id, channel="feishu")
        open_id = str(route.member.open_id or "").strip()
        if not open_id:
            raise RecipientResolutionError(
                "recipient_unreachable",
                "user_id has no usable Feishu open_id",
            )
        return route.user.display_name, open_id


async def _feishu_calendar_list(agent_id: uuid.UUID, arguments: dict) -> str:
    import httpx
    import re as _re
    from datetime import timedelta as _td

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    now = datetime.now(timezone.utc)

    def _to_iso(t: str | None, default: datetime) -> str:
        """Return an ISO-8601 string with timezone for freebusy API."""
        if not t:
            return default.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        if _re.fullmatch(r"\d+", t.strip()):
            from datetime import datetime as _dt2

            return _dt2.fromtimestamp(int(t.strip()), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        return t.strip()

    def _to_unix(t: str | None, default: datetime) -> str:
        """Convert ISO-8601 / Unix string / None to Unix timestamp string."""
        if not t:
            return str(int(default.timestamp()))
        if _re.fullmatch(r"\d+", t.strip()):
            return t.strip()
        try:
            from datetime import datetime as _dt2

            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = _dt2.strptime(t.strip(), fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return str(int(dt.timestamp()))
                except ValueError:
                    continue
            from dateutil import parser as _dp

            return str(int(_dp.parse(t).timestamp()))
        except Exception:
            return str(int(default.timestamp()))

    start_arg = arguments.get("start_time")
    end_arg = arguments.get("end_time")
    start_ts = _to_unix(start_arg, now)
    end_ts = _to_unix(end_arg, now + _td(days=7))
    start_iso = _to_iso(start_arg, now)
    end_iso = _to_iso(end_arg, now + _td(days=7))

    # ── 1. Query sender's real freebusy from Feishu Calendar ─────────────────
    sender_open_id = channel_feishu_sender_open_id.get(None)
    canonical_user_id = str(arguments.get("user_id") or "").strip()
    if canonical_user_id:
        try:
            _, sender_open_id = await _resolve_feishu_open_id(agent_id, canonical_user_id)
        except RecipientResolutionError as exc:
            return exc.as_json()

    freebusy_section = ""
    if sender_open_id:
        try:
            async with httpx.AsyncClient(timeout=10) as fb_client:
                fb_resp = await fb_client.post(
                    "https://open.feishu.cn/open-apis/calendar/v4/freebusy/list",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"user_id_type": "open_id"},
                    json={
                        "time_min": start_iso,
                        "time_max": end_iso,
                        "user_id": sender_open_id,
                    },
                )
            fb_data = fb_resp.json()
            if fb_data.get("code") == 0:
                busy_slots = fb_data.get("data", {}).get("freebusy_list", [])
                if busy_slots:
                    from datetime import datetime as _dt2
                    from zoneinfo import ZoneInfo

                    tz_cn = ZoneInfo("Asia/Shanghai")
                    busy_lines = []
                    for slot in sorted(busy_slots, key=lambda x: x.get("start_time", "")):
                        try:
                            s = _dt2.fromisoformat(slot["start_time"]).astimezone(tz_cn).strftime("%H:%M")
                            e = _dt2.fromisoformat(slot["end_time"]).astimezone(tz_cn).strftime("%H:%M")
                            busy_lines.append(f"  🔴 {s}–{e}")
                        except Exception:
                            busy_lines.append(f"  🔴 {slot.get('start_time')}–{slot.get('end_time')}")
                    freebusy_section = f"\n📌 **用户真实日历（忙碌时段）**：\n" + "\n".join(busy_lines)
                else:
                    freebusy_section = "\n📌 **用户真实日历**：该时段全部空闲。"
        except Exception as _fe:
            freebusy_section = f"\n⚠️ Freebusy 查询异常: {_fe}"

    # ── 2. Also list bot's own calendar events ───────────────────────────────
    agent_cal_id, cal_err = await _get_agent_calendar_id(token)
    if not agent_cal_id:
        # Return freebusy results even if bot calendar fails
        if freebusy_section:
            return freebusy_section.strip()
        return cal_err or "❌ Failed to retrieve agent's primary calendar ID."

    # Note: page_size is NOT a valid param for this API — omit it entirely
    params: dict = {}
    if start_ts:
        params["start_time"] = start_ts
    if end_ts:
        params["end_time"] = end_ts

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{agent_cal_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )

    data = resp.json()
    if data.get("code") != 0:
        if freebusy_section:
            return freebusy_section.strip()
        return f"❌ Calendar API error: {data.get('msg')} (code {data.get('code')})"

    items = data.get("data", {}).get("items", [])
    if not items and not freebusy_section:
        return "📅 该时间段内没有日程。"

    lines = []
    if items:
        lines.append(f"📅 Bot 日历共 {len(items)} 个日程：\n")
    for ev in items:
        summary = ev.get("summary", "(no title)")
        start = ev.get("start_time", {}).get("timestamp", "")
        end_t = ev.get("end_time", {}).get("timestamp", "")
        location = ev.get("location", {}).get("name", "")
        event_id = ev.get("event_id", "")
        try:
            from datetime import datetime as _dt

            s = _dt.fromtimestamp(int(start), tz=timezone.utc).strftime("%m-%d %H:%M") if start else "?"
            e = _dt.fromtimestamp(int(end_t), tz=timezone.utc).strftime("%H:%M") if end_t else "?"
        except Exception:
            s, e = start, end_t
        loc_str = f" | 📍{location}" if location else ""
        lines.append(f"- **{summary}** | 🕐{s}–{e}{loc_str}  (ID: `{event_id}`)")

    if freebusy_section:
        lines.append(freebusy_section)

    return "\n".join(lines) if lines else "📅 该时间段内没有日程。"


async def _feishu_calendar_create(agent_id: uuid.UUID, arguments: dict) -> str:
    import httpx

    summary = arguments.get("summary", "").strip()
    start_time = arguments.get("start_time", "").strip()
    end_time = arguments.get("end_time", "").strip()

    for f, v in [("summary", summary), ("start_time", start_time), ("end_time", end_time)]:
        if not v:
            return f"❌ Missing required argument '{f}'"

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    # Resolve every explicit attendee before creating the event, so invalid or
    # ambiguous IDs cannot leave a partially-created calendar operation.
    attendee_open_ids: list[str] = []
    attendee_display: list[str] = []
    for canonical_user_id in list(arguments.get("attendee_user_ids") or [])[:20]:
        try:
            display_name, open_id = await _resolve_feishu_open_id(agent_id, canonical_user_id)
        except RecipientResolutionError as exc:
            return exc.as_json()
        if open_id not in attendee_open_ids:
            attendee_open_ids.append(open_id)
            attendee_display.append(display_name)

    # Current Feishu sender remains an implicit internal route.
    sender_oid = channel_feishu_sender_open_id.get(None)
    if sender_oid and sender_oid not in attendee_open_ids:
        attendee_open_ids.append(sender_oid)

    agent_cal_id, cal_err = await _get_agent_calendar_id(token)
    if not agent_cal_id:
        return cal_err or "❌ Failed to retrieve agent's primary calendar ID."

    tz = arguments.get("timezone", "Asia/Shanghai")
    body: dict = {
        "summary": summary,
        "start_time": {"timestamp": str(int(_iso_to_ts(start_time))), "timezone": tz},
        "end_time": {"timestamp": str(int(_iso_to_ts(end_time))), "timezone": tz},
    }
    if arguments.get("description"):
        body["description"] = arguments["description"]
    if arguments.get("location"):
        body["location"] = {"name": arguments["location"]}

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{agent_cal_id}/events",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )

    data = resp.json()
    if data.get("code") != 0:
        return f"❌ Failed to create event: {data.get('msg')} (code {data.get('code')})"

    event_id = data.get("data", {}).get("event", {}).get("event_id", "")

    if attendee_open_ids and event_id:
        async with httpx.AsyncClient(timeout=20) as client:
            for oid in attendee_open_ids:
                await client.post(
                    f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{agent_cal_id}/events/{event_id}/attendees",
                    json={"attendees": [{"type": "user", "user_id": oid}]},
                    headers={"Authorization": f"Bearer {token}"},
                    params={"user_id_type": "open_id"},
                )

    att_str = f"\n**参与人**: {', '.join(attendee_display)}" if attendee_display else ""
    invite_note = "\n（已向您发送日历邀请，请在飞书日历中确认）" if attendee_open_ids else ""
    return (
        f"✅ 日历事件已创建！\n"
        f"**标题**: {summary}\n"
        f"**时间**: {start_time} → {end_time}{att_str}\n"
        f"**Event ID**: `{event_id}`{invite_note}"
    )


async def _feishu_calendar_update(agent_id: uuid.UUID, arguments: dict) -> str:
    import httpx

    event_id = arguments.get("event_id", "").strip()
    if not event_id:
        return "❌ 'event_id' is required."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    agent_cal_id, cal_err = await _get_agent_calendar_id(token)
    if not agent_cal_id:
        return cal_err or "❌ Failed to retrieve agent's primary calendar ID."

    patch: dict = {}
    tz = arguments.get("timezone", "Asia/Shanghai")
    if arguments.get("summary"):
        patch["summary"] = arguments["summary"]
    if arguments.get("description"):
        patch["description"] = arguments["description"]
    if arguments.get("location"):
        patch["location"] = {"name": arguments["location"]}
    if arguments.get("start_time"):
        patch["start_time"] = {"timestamp": str(int(_iso_to_ts(arguments["start_time"]))), "timezone": tz}
    if arguments.get("end_time"):
        patch["end_time"] = {"timestamp": str(int(_iso_to_ts(arguments["end_time"]))), "timezone": tz}

    if not patch:
        return "ℹ️ No fields to update."

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.patch(
            f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{agent_cal_id}/events/{event_id}",
            json=patch,
            headers={"Authorization": f"Bearer {token}"},
        )

    data = resp.json()
    if data.get("code") != 0:
        return f"❌ Failed to update: {data.get('msg')} (code {data.get('code')})"

    return f"✅ Event `{event_id}` updated. Changed: {', '.join(patch.keys())}."


async def _feishu_calendar_delete(agent_id: uuid.UUID, arguments: dict) -> str:
    import httpx

    event_id = arguments.get("event_id", "").strip()
    if not event_id:
        return "❌ 'event_id' is required."

    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."
    from app.services.feishu_service import feishu_service

    token = await feishu_service.get_tenant_access_token(app_id, app_secret)

    agent_cal_id, cal_err = await _get_agent_calendar_id(token)
    if not agent_cal_id:
        return cal_err or "❌ Failed to retrieve agent's primary calendar ID."

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.delete(
            f"https://open.feishu.cn/open-apis/calendar/v4/calendars/{agent_cal_id}/events/{event_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

    data = resp.json()
    if data.get("code") != 0:
        return f"❌ Failed to delete: {data.get('msg')} (code {data.get('code')})"

    return f"✅ Event `{event_id}` deleted successfully."


# ─── Feishu Approval Tools ───────────────────────────────────────────────────


async def _feishu_approval_create(agent_id: uuid.UUID, arguments: dict) -> str:
    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    approval_code = arguments.get("approval_code", "").strip()
    canonical_user_id = arguments.get("user_id", "").strip()
    form_data = arguments.get("form_data", "").strip()

    if not approval_code or not canonical_user_id or not form_data:
        return "❌ form_data, user_id and approval_code are required."

    try:
        _, open_id = await _resolve_feishu_open_id(agent_id, canonical_user_id)
    except RecipientResolutionError as exc:
        return exc.as_json()

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.create_approval_instance(app_id, app_secret, approval_code, open_id, form_data)
        err = _check_feishu_err(resp)
        if err:
            return err

        instance_code = resp.get("data", {}).get("instance_code", "")
        return f"✅ 审批发起成功！\n审批实例 ID: `{instance_code}`"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _feishu_approval_query(agent_id: uuid.UUID, arguments: dict) -> str:
    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    approval_code = arguments.get("approval_code", "").strip()
    status = arguments.get("status")

    if not approval_code:
        return "❌ approval_code is required."

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.query_approval_instances(app_id, app_secret, approval_code, status)
        err = _check_feishu_err(resp)
        if err:
            return err

        data = resp.get("data", {})
        instance_codes = data.get("instance_code_list", [])

        return f"✅ 查询完成。共发现 {len(instance_codes)} 个符合条件的审批实例。\n实例列表: {instance_codes}"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


async def _feishu_approval_get(agent_id: uuid.UUID, arguments: dict) -> str:
    app_id, app_secret = await _get_feishu_credentials(agent_id)
    if not app_id or not app_secret:
        return "❌ Agent has no Feishu channel configured."

    instance_id = arguments.get("instance_id", "").strip()
    if not instance_id:
        return "❌ instance_id is required."

    from app.services.feishu_service import feishu_service

    try:
        resp = await feishu_service.get_approval_instance(app_id, app_secret, instance_id)
        err = _check_feishu_err(resp)
        if err:
            return err

        data = resp.get("data", {})
        import json

        return f"✅ 审批实例查询结果:\n```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```"
    except Exception as e:
        return f"Failed: {str(e)[:300]}"


# ─── Feishu User Search ───────────────────────────────────────────────────────


async def _feishu_user_search(agent_id: uuid.UUID, arguments: dict) -> str:
    """Search related people by display name and return canonical IDs only."""

    name = (arguments.get("name") or "").strip()
    if not name:
        return "❌ Missing required argument 'name'"

    async with async_session() as db:
        query = (
            select(AgentRelationship)
            .join(UserModel, AgentRelationship.user_id == UserModel.id)
            .where(
                AgentRelationship.agent_id == agent_id,
                UserModel.is_active.is_(True),
                UserModel.display_name.ilike(f"%{name}%"),
            )
            .options(
                selectinload(AgentRelationship.user),
                selectinload(AgentRelationship.member),
            )
            .order_by(UserModel.display_name, UserModel.id)
        )
        relationships = (await db.execute(query)).scalars().all()
        matches: list[tuple[UserModel, OrgMember]] = []
        seen: set[uuid.UUID] = set()
        for relationship in relationships:
            if relationship.user_id in seen:
                continue
            try:
                route = await resolve_human_channel_recipient(
                    db,
                    agent_id,
                    relationship.user_id,
                    channel="feishu",
                )
            except RecipientResolutionError:
                continue
            seen.add(route.user.id)
            matches.append((route.user, route.member))

    if not matches:
        return f"🔍 未找到与「{name}」匹配且可通过飞书联系的关系用户。"

    lines = [f"🔍 找到 {len(matches)} 位匹配「{name}」的关系用户：\n"]
    for user, member in matches:
        lines.append(f"• **{user.display_name}**")
        lines.append(f"  user_id: `{user.id}`")
        if member.department_path:
            lines.append(f"  部门: {member.department_path}")
    if len(matches) > 1:
        lines.append("\n存在重名，请根据 user_id 和部门选择准确对象。")
    return "\n".join(lines)


# ─── AgentBay Tool Handlers ─────────────────────────────────────


def _agentbay_normalize_image_bytes(data) -> bytes | None:
    """Normalize AgentBay image payloads to raw bytes."""
    import base64 as _base64

    if isinstance(data, str):
        if data.startswith("data:image"):
            data = data.split(",", 1)[1]
        return _base64.b64decode(data)
    if isinstance(data, bytes):
        return data
    return None


def _agentbay_save_image_to_workspace(
    *,
    agent_id: uuid.UUID,
    ws: Path,
    raw_bytes: bytes,
    prefix: str,
    label: str,
) -> str:
    """Save an explicitly requested screenshot under workspace/screenshots/."""
    import time as _time

    rel_path = f"workspace/screenshots/{prefix}-{int(_time.time())}.png"
    screenshot_path = ws / rel_path
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.write_bytes(raw_bytes)
    logger.info(f"[AgentBay] Explicit screenshot saved to workspace: {rel_path}")
    return f"Screenshot saved to `{rel_path}`.\n![{label}](/api/agents/{agent_id}/files/download?path={rel_path})"


async def _agentbay_browser_navigate(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """AgentBay browser navigation.

    After navigating, always captures an internal screenshot for LLM vision.
    The screenshot is held in memory and consumed by vision_inject.py in the
    same request cycle; it is not persisted to the user's workspace.
    """
    if not agent_id:
        return "❌ AgentBay 工具需要 agent 上下文"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    url = arguments.get("url", "")
    wait_for = arguments.get("wait_for", "")

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        # Always request a screenshot for navigation so the model can observe the result
        result = await client.browser_navigate(url, wait_for=wait_for, screenshot=True)

        # Build text parts from the navigation result
        parts = [f"✅ 已访问: {url}"]
        if result.get("title"):
            parts.append(f"标题: {result['title']}")
        if result.get("content"):
            content = result["content"][:3000]
            parts.append(f"内容:\n{content}")
        logger.info(f"[AgentBay] Browser navigate result: {result.get('title')}")

        screenshot_data = result.get("screenshot")
        if screenshot_data:
            raw_bytes = _agentbay_normalize_image_bytes(screenshot_data)

            if raw_bytes:
                # Store in memory only — vision_inject.py will consume it.
                from app.services.vision_inject import store_temp_screenshot

                img_id = store_temp_screenshot(raw_bytes)
                parts.append(
                    f"Internal screenshot captured for analysis. [ImageID: {img_id}]\n"
                    f"NOTE: This screenshot is for LLM vision only and is not saved to the user's workspace."
                )
                logger.info(f"[AgentBay] Browser navigate screenshot stored in memory (id={img_id})")

        return "\n\n".join(parts)

    except RuntimeError as e:
        return f"❌ {str(e)}。请先在 Agent 设置中配置 AgentBay 通道。"
    except Exception as e:
        logger.exception(f"[AgentBay] Browser navigate failed for agent {agent_id}")
        return f"❌ AgentBay 浏览器访问失败: {str(e)[:200]}"


async def _agentbay_browser_screenshot(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Take a screenshot of the CURRENT browser page without navigating.

    Correct way to observe the result of a click, type, or form submit — never
    call browser_navigate again just to screenshot, that refreshes the page.

    The image is held in the process-level memory cache and consumed once by
    the LLM vision pipeline — no disk write, nothing shown in the user's file
    manager or chat history.
    """
    if not agent_id:
        return "❌ AgentBay 工具需要 agent 上下文"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        result = await client.browser_screenshot()

        screenshot_data = result.get("screenshot")
        if not screenshot_data:
            return "❌ 截图失败：未返回图像数据"

        raw_bytes = _agentbay_normalize_image_bytes(screenshot_data)
        if raw_bytes is None:
            return "❌ 截图失败：未知数据格式"

        # Store in memory only — vision_inject.py will consume it for LLM vision
        from app.services.vision_inject import store_temp_screenshot

        img_id = store_temp_screenshot(raw_bytes)
        logger.info(f"[AgentBay] Browser screenshot stored in memory (id={img_id})")
        return (
            f"Internal screenshot captured for analysis. [ImageID: {img_id}]\n"
            f"NOTE: This screenshot is for LLM vision only and is not saved to the user's workspace."
        )

    except RuntimeError as e:
        return f"❌ {str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Browser screenshot failed for agent {agent_id}")
        return f"❌ 截图失败: {str(e)[:200]}"


async def _agentbay_browser_save_screenshot(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Save the current AgentBay browser screenshot to workspace/screenshots/."""
    if not agent_id:
        return "❌ AgentBay 工具需要 agent 上下文"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        result = await client.browser_screenshot()
        raw_bytes = _agentbay_normalize_image_bytes(result.get("screenshot"))
        if raw_bytes is None:
            return "❌ 截图保存失败：未返回可保存的图像数据"
        return _agentbay_save_image_to_workspace(
            agent_id=agent_id,
            ws=ws,
            raw_bytes=raw_bytes,
            prefix="browser-screenshot",
            label="Browser Screenshot",
        )
    except RuntimeError as e:
        return f"❌ {str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Browser save screenshot failed for agent {agent_id}")
        return f"❌ 截图保存失败: {str(e)[:200]}"


async def _agentbay_browser_click(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """AgentBay 浏览器点击。"""
    if not agent_id:
        return "❌ AgentBay 工具需要 agent 上下文"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    selector = arguments.get("selector", "")

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        await client.browser_click(selector)
        return f"✅ 已点击元素: {selector}"
    except RuntimeError as e:
        return f"❌ {str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Browser click failed")
        return f"❌ 点击失败: {str(e)[:200]}"


async def _agentbay_browser_type(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """AgentBay 浏览器输入。"""
    if not agent_id:
        return "❌ AgentBay 工具需要 agent 上下文"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    selector = arguments.get("selector", "")
    text = arguments.get("text", "")

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        await client.browser_type(selector, text)
        return f"✅ 已在 {selector} 输入文本"
    except RuntimeError as e:
        return f"❌ {str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Browser type failed")
        return f"❌ 输入失败: {str(e)[:200]}"


async def _agentbay_code_execute(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """在 AgentBay 代码空间执行代码。"""
    if not agent_id:
        return "❌ AgentBay 工具需要 agent 上下文"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    language = arguments.get("language", "python")
    code = arguments.get("code", "")
    timeout = arguments.get("timeout", 30)

    if not code.strip():
        return "❌ 请提供要执行的代码"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        result = await client.code_execute(language, code, timeout)

        # 格式化返回结果
        parts = [f"✅ 代码执行完成 ({language})"]
        if result.get("stdout"):
            parts.append(f"📤 输出:\n{result['stdout']}")
        if result.get("stderr"):
            parts.append(f"⚠️ 错误输出:\n{result['stderr']}")
        if result.get("exit_code") != 0:
            parts.append(f"退出码: {result['exit_code']}")

        return "\n\n".join(parts)

    except RuntimeError as e:
        return f"❌ {str(e)}。请先在 Agent 设置中配置 AgentBay 通道。"
    except Exception as e:
        logger.exception(f"[AgentBay] Code execution failed for agent {agent_id}")
        return f"❌ 代码执行失败: {str(e)[:200]}"


async def _agentbay_code_write_file(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Write a text file in the AgentBay Code Sandbox."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    remote_path = arguments.get("remote_path") or arguments.get("path") or ""
    content = arguments.get("content")
    mode = arguments.get("mode", "overwrite")

    if not remote_path.strip():
        return "Missing required argument 'remote_path'"
    if content is None:
        return "Missing required argument 'content'"
    if mode not in ("overwrite", "append"):
        return "Invalid mode. Use 'overwrite' or 'append'."

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        result = await asyncio.to_thread(
            client._session.file_system.write_file,
            remote_path,
            str(content),
            mode,
        )
        if result.success:
            byte_count = len(str(content).encode("utf-8"))
            return f"File written in AgentBay Code Sandbox: {remote_path} ({byte_count} bytes, mode={mode})"
        return f"Write failed: {result.error_message}"
    except RuntimeError as e:
        return f"{str(e)}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Code write file failed for agent {agent_id}")
        return f"Write file failed: {str(e)[:200]}"


async def _agentbay_code_read_file(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Read a text file from the AgentBay Code Sandbox."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    remote_path = arguments.get("remote_path") or arguments.get("path") or ""
    if not remote_path.strip():
        return "Missing required argument 'remote_path'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        result = await asyncio.to_thread(
            client._session.file_system.read_file,
            remote_path,
        )
        if result.success:
            content = getattr(result, "content", "") or ""
            return f"File read from AgentBay Code Sandbox: {remote_path}\n\n{content[:12000]}"
        return f"Read failed: {result.error_message}"
    except RuntimeError as e:
        return f"{str(e)}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Code read file failed for agent {agent_id}")
        return f"Read file failed: {str(e)[:200]}"


async def _agentbay_code_edit_file(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Edit a text file in the AgentBay Code Sandbox."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    remote_path = arguments.get("remote_path") or arguments.get("path") or ""
    edits = arguments.get("edits")
    dry_run = bool(arguments.get("dry_run", False))

    if not remote_path.strip():
        return "Missing required argument 'remote_path'"
    if not isinstance(edits, list) or not edits:
        return "Missing required argument 'edits'"

    normalized_edits = []
    for edit in edits:
        if not isinstance(edit, dict):
            return "Each edit must be an object with oldText and newText."
        old_text = edit.get("oldText")
        new_text = edit.get("newText")
        if old_text is None or new_text is None:
            return "Each edit must include oldText and newText."
        normalized_edits.append({"oldText": str(old_text), "newText": str(new_text)})

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        result = await asyncio.to_thread(
            client._session.file_system.edit_file,
            remote_path,
            normalized_edits,
            dry_run,
        )
        if result.success:
            action = "Previewed edits for" if dry_run else "Edited"
            return f"{action} AgentBay Code Sandbox file: {remote_path} ({len(normalized_edits)} replacement(s))"
        return f"Edit failed: {result.error_message}"
    except RuntimeError as e:
        return f"{str(e)}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Code edit file failed for agent {agent_id}")
        return f"Edit file failed: {str(e)[:200]}"

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


# ─── AgentBay: Browser Extract & Observe ────────────────────────────────


async def _agentbay_browser_extract(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Extract structured data from current browser page."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    instruction = arguments.get("instruction", "")
    selector = arguments.get("selector", "")

    if not instruction.strip():
        return "Missing required argument 'instruction'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        result = await client.browser_extract(instruction, selector=selector)

        if result.get("success"):
            import json

            data = result.get("data", {})
            data_str = json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, (dict, list)) else str(data)
            return f"Extraction successful:\n\n{data_str[:5000]}"
        else:
            return f"Extraction failed: {result}"

    except RuntimeError as e:
        return f"{str(e)}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Browser extract failed for agent {agent_id}")
        return f"Browser extract failed: {str(e)[:200]}"


async def _agentbay_browser_observe(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Observe the current browser page state."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    instruction = arguments.get("instruction", "")
    selector = arguments.get("selector", "")

    if not instruction.strip():
        return "Missing required argument 'instruction'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        result = await client.browser_observe(instruction, selector=selector)

        if result.get("success"):
            import json

            elements = result.get("elements", [])
            if not elements:
                return "No interactive elements found matching your instruction."
            elements_str = json.dumps(elements, ensure_ascii=False, indent=2)
            return f"Found {len(elements)} interactive element(s):\n\n{elements_str[:5000]}"
        else:
            return f"Observation failed: {result}"

    except RuntimeError as e:
        return f"{str(e)}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Browser observe failed for agent {agent_id}")
        return f"Browser observe failed: {str(e)[:200]}"


# ─── AgentBay: Command (Shell) ──────────────────────────────────────────


async def _agentbay_browser_login(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Perform an automated login using AgentBay's built-in login skill.

    Supports complex login flows including CAPTCHAs, OTP inputs,
    and multi-step authentication via AgentBay's AI-driven capability.
    """
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    url = arguments.get("url", "")
    login_config = arguments.get("login_config", "")

    if not url.strip():
        return "Missing required argument 'url'"
    if not login_config.strip():
        return "Missing required argument 'login_config' (JSON string with api_key + skill_id)"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "browser", session_id=_session_id)
        result = await client.browser_login(url, login_config)

        if result.get("success"):
            return f"Login completed successfully. {result.get('message', '')}"
        else:
            return f"Login failed: {result.get('message', 'Unknown error')}"

    except RuntimeError as e:
        return f"{str(e)}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Browser login failed for agent {agent_id}")
        return f"Login failed: {str(e)[:200]}"


async def _agentbay_command_exec(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Execute a shell command in the AgentBay environment."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    command = arguments.get("command", "")
    timeout_ms = arguments.get("timeout_ms", 50000)
    cwd = arguments.get("cwd", "")

    if not command.strip():
        return "Missing required argument 'command'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "code", session_id=_session_id)
        result = await client.command_exec(command, timeout_ms=timeout_ms, cwd=cwd)

        parts = []
        if result.get("success"):
            parts.append(f"Command executed successfully (exit code: {result.get('exit_code', 0)})")
        else:
            parts.append(f"Command failed (exit code: {result.get('exit_code', -1)})")

        if result.get("stdout"):
            parts.append(f"stdout:\n{result['stdout'][:3000]}")
        if result.get("stderr"):
            parts.append(f"stderr:\n{result['stderr'][:1000]}")
        if result.get("error_message"):
            parts.append(f"Error: {result['error_message']}")

        return "\n\n".join(parts)

    except RuntimeError as e:
        return f"{str(e)}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Command exec failed for agent {agent_id}")
        return f"Command execution failed: {str(e)[:200]}"


# ─── AgentBay: Computer Use Handlers ────────────────────────────────────


def _agentbay_extract_screen_dimensions(screen_data) -> tuple[int | None, int | None, str]:
    """Return width/height/dpi text from AgentBay get_screen_size payload."""
    if not isinstance(screen_data, dict):
        return None, None, ""
    width = screen_data.get("width")
    height = screen_data.get("height")
    dpi = screen_data.get("dpiScalingFactor")
    try:
        width = int(width) if width is not None else None
        height = int(height) if height is not None else None
    except (TypeError, ValueError):
        width, height = None, None
    parts = []
    if width and height:
        parts.append(f"width={width}, height={height}")
    if dpi is not None:
        parts.append(f"dpiScalingFactor={dpi}")
    return width, height, ", ".join(parts)


async def _agentbay_get_screen_metadata(client) -> tuple[int | None, int | None, str]:
    try:
        size_result = await client.computer_get_screen_size()
        if size_result.get("success"):
            return _agentbay_extract_screen_dimensions(size_result.get("data"))
    except Exception as e:
        logger.debug(f"[AgentBay] Could not fetch computer screen size: {e}")
    return None, None, ""


def _agentbay_image_dimensions(raw_bytes: bytes) -> tuple[int | None, int | None]:
    try:
        from io import BytesIO
        from PIL import Image

        with Image.open(BytesIO(raw_bytes)) as img:
            return img.width, img.height
    except Exception:
        return None, None


def _agentbay_crop_image_bytes(
    raw_bytes: bytes,
    *,
    x: int,
    y: int,
    width: int,
    height: int,
) -> tuple[bytes, tuple[int, int, int, int], int] | None:
    try:
        from io import BytesIO
        from PIL import Image

        with Image.open(BytesIO(raw_bytes)) as img:
            img_width, img_height = img.width, img.height
            left = max(0, min(int(x), img_width - 1))
            top = max(0, min(int(y), img_height - 1))
            right = max(left + 1, min(left + int(width), img_width))
            bottom = max(top + 1, min(top + int(height), img_height))
            cropped = img.crop((left, top, right, bottom))

            # Enlarge precision crops before vision injection so small controls
            # occupy more pixels without changing the absolute coordinate labels.
            max_side = max(cropped.width, cropped.height)
            scale = 1
            if max_side <= 260:
                scale = 3
            elif max_side <= 520:
                scale = 2
            if scale > 1:
                cropped = cropped.resize((cropped.width * scale, cropped.height * scale), Image.Resampling.LANCZOS)

            buf = BytesIO()
            cropped.save(buf, format="PNG")
            return buf.getvalue(), (left, top, right - left, bottom - top), scale
    except Exception as e:
        logger.debug(f"[AgentBay] Could not crop desktop screenshot: {e}")
        return None


def _agentbay_expand_precision_crop(
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    min_width: int = 360,
    min_height: int = 240,
) -> tuple[int, int, int, int]:
    """Expand small requested crops so near-miss targeting still shows context."""
    width = max(1, int(width))
    height = max(1, int(height))
    expanded_width = max(width, min_width)
    expanded_height = max(height, min_height)
    center_x = int(x) + width / 2
    center_y = int(y) + height / 2
    expanded_x = int(round(center_x - expanded_width / 2))
    expanded_y = int(round(center_y - expanded_height / 2))
    return expanded_x, expanded_y, expanded_width, expanded_height


def _agentbay_desktop_coordinate_note(
    screen_note: str,
    image_width: int | None = None,
    image_height: int | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> str:
    parts = []
    if screen_note:
        parts.append(f"Cloud Desktop coordinate system for mouse tools: {screen_note}.")
    if image_width and image_height:
        parts.append(f"Latest screenshot pixel size: width={image_width}, height={image_height}.")
    if crop:
        x, y, width, height = crop
        parts.append(
            f"Precision crop shown to vision: absolute origin=({x}, {y}), size={width}x{height}. "
            "Grid labels in the crop are absolute Cloud Desktop coordinates, not crop-local coordinates."
        )
    if parts:
        parts.append(
            "The injected analysis image includes a coordinate grid; use the grid labels to choose the center of the target. "
            "Before clicking dialog buttons, text buttons, tabs, menus, checkboxes, close buttons, small controls, "
            "or any target whose center is not unambiguous, take a precision screenshot around that target area. "
            "For popup dismissal, prefer agentbay_computer_dismiss_dialog before coordinate clicking. "
            "Use absolute desktop pixels from the top-left corner (0, 0); do not use the size of the right-side preview panel."
        )
    return "\n".join(parts)


def _agentbay_normalize_text(value) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _agentbay_app_field(app: dict, *keys: str) -> str:
    for key in keys:
        value = app.get(key)
        if value:
            return str(value)
    return ""


def _agentbay_format_apps(apps: list, limit: int = 40) -> str:
    import json

    if not apps:
        return "[]"
    compact_apps = []
    for app in apps[:limit]:
        if isinstance(app, dict):
            compact_apps.append(
                {
                    key: app.get(key)
                    for key in (
                        "name",
                        "start_cmd",
                        "startCmd",
                        "work_directory",
                        "workDirectory",
                        "stop_cmd",
                        "stopCmd",
                    )
                    if app.get(key)
                }
            )
        else:
            compact_apps.append(str(app))
    rendered = json.dumps(compact_apps, ensure_ascii=False, indent=2)
    if len(apps) > limit:
        rendered += f"\n... {len(apps) - limit} more app(s) omitted"
    return rendered[:5000]


def _agentbay_find_installed_app_match(query: str, apps: list) -> tuple[dict | None, float]:
    from difflib import SequenceMatcher

    query_norm = _agentbay_normalize_text(query.split()[0] if query else query)
    if not query_norm:
        return None, 0.0

    best_app = None
    best_score = 0.0
    for app in apps:
        if not isinstance(app, dict):
            continue
        fields = [
            _agentbay_app_field(app, "name"),
            _agentbay_app_field(app, "start_cmd", "startCmd"),
            _agentbay_app_field(app, "work_directory", "workDirectory"),
        ]
        for field in fields:
            field_norm = _agentbay_normalize_text(field)
            if not field_norm:
                continue
            if query_norm == field_norm:
                score = 1.0
            elif query_norm in field_norm or field_norm in query_norm:
                score = 0.9
            else:
                score = SequenceMatcher(None, query_norm, field_norm).ratio()
            if score > best_score:
                best_app, best_score = app, score

    return best_app, best_score


def _agentbay_uncertain_start_error(error_message: str) -> bool:
    text = (error_message or "").lower()
    return "may have launched" in text or "no processes found" in text


async def _agentbay_visible_apps_note(client) -> str:
    try:
        visible = await client.computer_list_visible_apps()
        if visible.get("success"):
            apps = visible.get("apps", [])
            return (
                f"Visible applications after the launch attempt ({len(apps)}):\n{_agentbay_format_apps(apps, limit=20)}"
            )
        return f"Could not verify visible applications: {visible.get('error_message', 'Unknown error')}"
    except Exception as e:
        logger.debug(f"[AgentBay] Could not list visible apps after start_app: {e}")
        return f"Could not verify visible applications: {str(e)[:200]}"


async def _agentbay_computer_screenshot(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Take a screenshot of the AgentBay cloud desktop.

    The image is held in the process-level memory cache for LLM vision analysis
    only — no disk write, nothing shown in the user's file manager or chat
    history.
    """
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    focus_x = arguments.get("focus_x")
    focus_y = arguments.get("focus_y")
    focus_width = arguments.get("focus_width")
    focus_height = arguments.get("focus_height")

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_screenshot()

        if not (result.get("success") and result.get("data")):
            return f"Screenshot failed: {result.get('error_message', 'Unknown error')}"

        raw_data = result["data"]

        raw_bytes = _agentbay_normalize_image_bytes(raw_data)
        if raw_bytes is None:
            return "Screenshot captured but data format is unrecognised."

        crop_bounds: tuple[int, int, int, int] | None = None
        crop_scale = 1
        analysis_bytes = raw_bytes
        if focus_x is not None and focus_y is not None and focus_width is not None and focus_height is not None:
            try:
                crop_result = _agentbay_crop_image_bytes(
                    raw_bytes,
                    x=int(round(float(focus_x))),
                    y=int(round(float(focus_y))),
                    width=int(round(float(focus_width))),
                    height=int(round(float(focus_height))),
                )
                if crop_result:
                    analysis_bytes, crop_bounds, crop_scale = crop_result
            except (TypeError, ValueError):
                crop_bounds = None

        # Store in memory only — vision_inject.py will consume it for LLM vision
        from app.services.vision_inject import store_temp_screenshot

        grid_options = {}
        if crop_bounds:
            crop_x, crop_y, crop_width, crop_height = crop_bounds
            grid_options = {
                "origin_x": crop_x,
                "origin_y": crop_y,
                "minor_step": 10,
                "major_step": 50,
                "pixel_scale": crop_scale,
            }
        img_id = store_temp_screenshot(analysis_bytes, grid_options=grid_options)
        logger.info(f"[AgentBay] Desktop screenshot stored in memory (id={img_id})")
        screen_width, screen_height, screen_note = await _agentbay_get_screen_metadata(client)
        image_width, image_height = _agentbay_image_dimensions(raw_bytes)
        coordinate_note = _agentbay_desktop_coordinate_note(
            screen_note,
            image_width or screen_width,
            image_height or screen_height,
            crop=crop_bounds,
        )
        return (
            f"Internal desktop screenshot captured for analysis. [ImageID: {img_id}]\n"
            f"{coordinate_note}\n"
            "TARGETING NOTE: Before clicking dialog buttons, text buttons, tabs, menus, checkboxes, "
            "close buttons, small controls, or any target whose center is not unambiguous, call "
            "agentbay_computer_precision_screenshot around the target and click from that enlarged crop.\n"
            f"NOTE: This screenshot is for LLM vision only and is not saved to the user's workspace."
        )

    except RuntimeError as e:
        return f"{str(e)}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Computer screenshot failed for agent {agent_id}")
        return f"Desktop screenshot failed: {str(e)[:200]}"


async def _agentbay_computer_save_screenshot(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Save the current AgentBay cloud desktop screenshot to workspace/screenshots/."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_screenshot()
        if not (result.get("success") and result.get("data")):
            return f"Screenshot save failed: {result.get('error_message', 'Unknown error')}"
        raw_bytes = _agentbay_normalize_image_bytes(result.get("data"))
        if raw_bytes is None:
            return "Screenshot save failed: captured data format is unrecognised."
        screen_width, screen_height, screen_note = await _agentbay_get_screen_metadata(client)
        image_width, image_height = _agentbay_image_dimensions(raw_bytes)
        coordinate_note = _agentbay_desktop_coordinate_note(
            screen_note,
            image_width or screen_width,
            image_height or screen_height,
        )
        saved = _agentbay_save_image_to_workspace(
            agent_id=agent_id,
            ws=ws,
            raw_bytes=raw_bytes,
            prefix="desktop-screenshot",
            label="Desktop Screenshot",
        )
        return f"{saved}\n{coordinate_note}"
    except RuntimeError as e:
        return f"{str(e)}. Please configure AgentBay in Agent settings."
    except Exception as e:
        logger.exception(f"[AgentBay] Computer save screenshot failed for agent {agent_id}")
        return f"Desktop screenshot save failed: {str(e)[:200]}"


async def _agentbay_computer_precision_screenshot(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Take an enlarged precision crop for desktop controls."""
    aliases = {
        "focus_x": "x",
        "focus_y": "y",
        "focus_width": "width",
        "focus_height": "height",
    }
    for alias, canonical in aliases.items():
        if arguments.get(canonical) is None and arguments.get(alias) is not None:
            arguments[canonical] = arguments.get(alias)

    required = ("x", "y", "width", "height")
    missing = [key for key in required if arguments.get(key) is None]
    if missing:
        return (
            f"Missing required precision crop argument(s): {', '.join(missing)}. "
            "Use x, y, width, height for the absolute desktop crop rectangle."
        )

    try:
        requested_x = int(round(float(arguments["x"])))
        requested_y = int(round(float(arguments["y"])))
        requested_width = int(round(float(arguments["width"])))
        requested_height = int(round(float(arguments["height"])))
    except (TypeError, ValueError):
        return (
            "Precision crop failed: x, y, width, and height must be numeric absolute desktop pixels. "
            f"Got x={arguments.get('x')!r}, y={arguments.get('y')!r}, "
            f"width={arguments.get('width')!r}, height={arguments.get('height')!r}."
        )

    expanded_x, expanded_y, expanded_width, expanded_height = _agentbay_expand_precision_crop(
        requested_x,
        requested_y,
        requested_width,
        requested_height,
    )

    precision_args = dict(arguments)
    precision_args["focus_x"] = expanded_x
    precision_args["focus_y"] = expanded_y
    precision_args["focus_width"] = expanded_width
    precision_args["focus_height"] = expanded_height
    result = await _agentbay_computer_screenshot(agent_id, ws, precision_args)
    expansion_note = ""
    if (
        expanded_x,
        expanded_y,
        expanded_width,
        expanded_height,
    ) != (requested_x, requested_y, requested_width, requested_height):
        expansion_note = (
            f"Requested crop ({requested_x}, {requested_y}, {requested_width}x{requested_height}) "
            f"was expanded for context to ({expanded_x}, {expanded_y}, {expanded_width}x{expanded_height}). "
        )
    return (
        "Precision desktop crop captured for accurate targeting. "
        f"{expansion_note}"
        "Use the absolute coordinate labels in this enlarged crop for the next click; click the visual center "
        "of the target and do not reuse a guessed coordinate from the full screenshot.\n"
        f"{result}"
    )


async def _agentbay_computer_click(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Click the mouse at specific coordinates on the desktop."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    x = arguments.get("x", 0)
    y = arguments.get("y", 0)
    button = arguments.get("button", "left")

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        try:
            x = int(round(float(x)))
            y = int(round(float(y)))
        except (TypeError, ValueError):
            return f"Click failed: x and y must be numeric desktop pixel coordinates, got x={x!r}, y={y!r}."

        screen_width, screen_height, screen_note = await _agentbay_get_screen_metadata(client)
        if screen_width and screen_height and not (0 <= x < screen_width and 0 <= y < screen_height):
            return (
                f"Click refused: ({x}, {y}) is outside the Cloud Desktop coordinate system "
                f"({screen_note}). Use coordinates from the latest full desktop screenshot."
            )
        result = await client.computer_click(x, y, button=button)
        if result.get("success"):
            note = f" within {screen_note}" if screen_note else ""
            return (
                f"Clicked at ({x}, {y}) with {button} button{note}. "
                f"This only confirms the mouse event was sent; call agentbay_computer_screenshot to verify the UI changed."
            )
        note = f" Coordinate system: {screen_note}." if screen_note else ""
        return f"Click failed at ({x}, {y}).{note}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer click failed")
        return f"Click failed: {str(e)[:200]}"


async def _agentbay_computer_input_text(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Type text at the current cursor position."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    text = arguments.get("text", "")
    if not text:
        return "Missing required argument 'text'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_input_text(text)
        if result.get("success"):
            return f"Typed text: {text[:100]}"
        return f"Text input failed"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer input_text failed")
        return f"Text input failed: {str(e)[:200]}"


async def _agentbay_computer_press_keys(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Press keyboard keys or shortcuts."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    keys = arguments.get("keys", [])
    hold = arguments.get("hold", False)

    if not keys:
        return "Missing required argument 'keys'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_press_keys(keys, hold=hold)
        key_str = "+".join(keys)
        if result.get("success"):
            return f"Pressed keys: {key_str}" + (" (held)" if hold else "")
        return f"Key press failed: {key_str}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer press_keys failed")
        return f"Key press failed: {str(e)[:200]}"


async def _agentbay_computer_scroll(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Scroll the screen at a specific position."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    x = arguments.get("x", 0)
    y = arguments.get("y", 0)
    direction = arguments.get("direction", "down")
    amount = arguments.get("amount", 1)

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_scroll(x, y, direction=direction, amount=amount)
        if result.get("success"):
            return f"Scrolled {direction} by {amount} step(s) at ({x}, {y})"
        return f"Scroll failed"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer scroll failed")
        return f"Scroll failed: {str(e)[:200]}"


async def _agentbay_computer_move_mouse(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Move mouse to coordinates without clicking."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    x = arguments.get("x", 0)
    y = arguments.get("y", 0)

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_move_mouse(x, y)
        if result.get("success"):
            return f"Mouse moved to ({x}, {y})"
        return f"Mouse move failed"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer move_mouse failed")
        return f"Mouse move failed: {str(e)[:200]}"


async def _agentbay_computer_drag_mouse(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Drag mouse from one position to another."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    from_x = arguments.get("from_x", 0)
    from_y = arguments.get("from_y", 0)
    to_x = arguments.get("to_x", 0)
    to_y = arguments.get("to_y", 0)
    button = arguments.get("button", "left")

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_drag_mouse(from_x, from_y, to_x, to_y, button=button)
        if result.get("success"):
            return f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})"
        return f"Drag failed"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer drag_mouse failed")
        return f"Drag failed: {str(e)[:200]}"


async def _agentbay_computer_get_screen_size(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Get the screen resolution."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_get_screen_size()
        if result.get("success"):
            import json

            data = result.get("data")
            data_str = json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)
            return f"Screen size: {data_str}"
        return f"Failed to get screen size: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer get_screen_size failed")
        return f"Get screen size failed: {str(e)[:200]}"


async def _agentbay_computer_start_app(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Start an application on the desktop."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    cmd = arguments.get("cmd", "")
    work_dir = arguments.get("work_dir", "")

    if not cmd.strip():
        return "Missing required argument 'cmd'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_start_app(cmd, work_dir=work_dir)
        if result.get("success"):
            # result.data may contain non-serializable objects (e.g. Process),
            # so convert to string safely instead of json.dumps()
            data = result.get("data")
            if data is not None:
                try:
                    import json

                    data_str = (
                        json.dumps(data, ensure_ascii=False, indent=2)
                        if isinstance(data, (dict, list, str, int, float, bool))
                        else str(data)
                    )
                except (TypeError, ValueError):
                    data_str = str(data)
            else:
                data_str = ""
            return f"Application started: {cmd}" + (f"\n\n{data_str[:1000]}" if data_str else "")

        direct_error = result.get("error_message", "Unknown error")
        installed_note = ""
        try:
            installed_result = await client.computer_get_installed_apps()
            if installed_result.get("success"):
                apps = installed_result.get("apps", [])
                matched_app, score = _agentbay_find_installed_app_match(cmd, apps)
                if matched_app and score >= 0.58:
                    matched_name = _agentbay_app_field(matched_app, "name") or "(unnamed app)"
                    matched_cmd = _agentbay_app_field(matched_app, "start_cmd", "startCmd")
                    matched_work_dir = _agentbay_app_field(matched_app, "work_directory", "workDirectory") or work_dir
                    if matched_cmd and matched_cmd.strip() != cmd.strip():
                        retry = await client.computer_start_app(matched_cmd, work_dir=matched_work_dir)
                        if retry.get("success"):
                            retry_data = retry.get("data")
                            retry_data_str = str(retry_data)[:1000] if retry_data is not None else ""
                            return (
                                f"Direct start command failed: {cmd}\n"
                                f"Matched installed app: {matched_name} (score={score:.2f})\n"
                                f"Retried with start_cmd: {matched_cmd}\n"
                                f"Application started." + (f"\n\n{retry_data_str}" if retry_data_str else "")
                            )

                        retry_error = retry.get("error_message", "Unknown error")
                        if _agentbay_uncertain_start_error(retry_error):
                            visible_note = await _agentbay_visible_apps_note(client)
                            return (
                                f"Direct start command failed: {cmd}\n"
                                f"Matched installed app: {matched_name} (score={score:.2f})\n"
                                f"Retried with start_cmd: {matched_cmd}\n"
                                f"Retry reported an uncertain launch result: {retry_error}\n\n"
                                f"{visible_note}"
                            )
                        return (
                            f"Direct start command failed: {cmd}\n"
                            f"Matched installed app: {matched_name} (score={score:.2f})\n"
                            f"Retried with start_cmd: {matched_cmd}\n"
                            f"Retry failed: {retry_error}"
                        )

                installed_note = (
                    f"\n\nInstalled apps were checked, but no confident match was found for `{cmd}`. "
                    f"Use agentbay_computer_get_installed_apps and then pass the returned start_cmd to this tool."
                )
            else:
                installed_note = (
                    f"\n\nCould not check installed apps: {installed_result.get('error_message', 'Unknown error')}"
                )
        except Exception as e:
            logger.debug(f"[AgentBay] Installed app fallback failed: {e}")
            installed_note = f"\n\nCould not check installed apps: {str(e)[:200]}"

        if _agentbay_uncertain_start_error(direct_error):
            visible_note = await _agentbay_visible_apps_note(client)
            return (
                f"Start command reported an uncertain launch result: {direct_error}\n\n{visible_note}{installed_note}"
            )

        return f"Failed to start application: {direct_error}{installed_note}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer start_app failed")
        return f"Start application failed: {str(e)[:200]}"


async def _agentbay_computer_get_installed_apps(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """List installed desktop applications and launch commands."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    start_menu = arguments.get("start_menu", True)
    desktop = arguments.get("desktop", True)
    ignore_system_apps = arguments.get("ignore_system_apps", True)

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_get_installed_apps(
            start_menu=bool(start_menu),
            desktop=bool(desktop),
            ignore_system_apps=bool(ignore_system_apps),
        )
        if result.get("success"):
            apps = result.get("apps", [])
            if not apps:
                return "No installed applications found."
            return (
                f"Installed applications ({len(apps)}). Use the returned start_cmd exactly with "
                f"agentbay_computer_start_app; do not guess app launch commands.\n\n"
                f"{_agentbay_format_apps(apps, limit=80)}"
            )
        return f"Failed to get installed applications: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer get_installed_apps failed")
        return f"Get installed applications failed: {str(e)[:200]}"


async def _agentbay_computer_get_cursor_position(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Get current cursor position."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_get_cursor_position()
        if result.get("success"):
            import json

            data = result.get("data")
            data_str = json.dumps(data, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)
            return f"Cursor position: {data_str}"
        return f"Failed to get cursor position: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer get_cursor_position failed")
        return f"Get cursor position failed: {str(e)[:200]}"


async def _agentbay_computer_get_active_window(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Get info about the currently active window."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_get_active_window()
        if result.get("success"):
            import json

            window = result.get("window")
            window_str = json.dumps(window, ensure_ascii=False, indent=2) if isinstance(window, dict) else str(window)
            return f"Active window:\n\n{window_str}"
        return f"Failed to get active window: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer get_active_window failed")
        return f"Get active window failed: {str(e)[:200]}"


async def _agentbay_computer_activate_window(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Activate (bring to front) a window by its ID."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    window_id = arguments.get("window_id")
    if window_id is None:
        return "Missing required argument 'window_id'"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_activate_window(int(window_id))
        if result.get("success"):
            return f"Window {window_id} activated (brought to front)"
        return f"Failed to activate window {window_id}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer activate_window failed")
        return f"Activate window failed: {str(e)[:200]}"


async def _agentbay_computer_list_windows(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """List OS-level root windows with IDs and geometry."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    timeout_ms = arguments.get("timeout_ms", 3000)

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_list_windows(timeout_ms=int(timeout_ms))
        if result.get("success"):
            import json

            windows = result.get("windows", [])
            if not windows:
                return "No root windows found."
            windows_str = json.dumps(windows, ensure_ascii=False, indent=2)
            return (
                f"OS-level root desktop windows ({len(windows)}). These window_id values refer to whole "
                f"application windows. Use them for activation, or for closing only when the user explicitly "
                f"asked to close/quit an entire desktop window or app. Do NOT use these IDs for in-app popups, "
                f"modals, embedded marketplace/store panels, browser/app tabs, document tabs, or software-internal "
                f"dialogs; close those with the app UI, Escape, Ctrl+W, or agentbay_computer_dismiss_dialog.\n\n"
                f"{windows_str[:5000]}"
            )
        return f"Failed to list windows: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer list_windows failed")
        return f"List windows failed: {str(e)[:200]}"


async def _agentbay_computer_close_window(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Close an entire OS-level root desktop window/application by explicit ID."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    window_id = arguments.get("window_id")
    title = str(arguments.get("title") or "").strip()

    if window_id is None:
        if not title:
            return (
                "Missing required argument `window_id`. Only use agentbay_computer_close_window when the user "
                "explicitly wants to close or quit an entire OS-level desktop window/application. If the target "
                "is an in-app popup, modal, embedded marketplace/store panel, browser/app tab, document tab, "
                "or software-internal dialog, use app UI controls, Escape, Ctrl+W, or "
                "agentbay_computer_dismiss_dialog instead."
            )

        try:
            _session_id = arguments.pop("_session_id", "")
            client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
            windows_result = await client.computer_list_windows()
            if not windows_result.get("success"):
                return f"Failed to list windows before closing: {windows_result.get('error_message', 'Unknown error')}"

            from difflib import SequenceMatcher
            import json

            title_norm = _agentbay_normalize_text(title)
            candidates: list[dict] = []
            for window in windows_result.get("windows", []):
                if not isinstance(window, dict):
                    continue
                candidate = str(window.get("title") or window.get("window_title") or "")
                candidate_norm = _agentbay_normalize_text(candidate)
                if not candidate_norm:
                    continue
                if title_norm in candidate_norm or candidate_norm in title_norm:
                    score = 0.95
                else:
                    score = SequenceMatcher(None, title_norm, candidate_norm).ratio()
                if score >= 0.35:
                    item = dict(window)
                    item["match_score"] = round(score, 3)
                    candidates.append(item)
            candidates.sort(key=lambda item: item.get("match_score", 0), reverse=True)
            return (
                f"Refusing to close by title-only match for `{title}` because it can close the wrong application. "
                f"The candidates below are whole OS-level root windows. Choose a root window_id only if the user "
                f"explicitly wants to close/quit that entire application window. For in-app popups, modals, "
                f"embedded marketplace/store panels, browser/app tabs, document tabs, or software-internal dialogs, "
                f"do not close a root window; use app UI controls, Escape, Ctrl+W, or "
                f"agentbay_computer_dismiss_dialog instead.\n\n"
                f"{json.dumps(candidates[:8], ensure_ascii=False, indent=2)[:3000]}"
            )
        except RuntimeError as e:
            return f"{str(e)}"
        except Exception as e:
            logger.exception(f"[AgentBay] Computer close_window candidate lookup failed")
            return f"Close window requires window_id. Candidate lookup failed: {str(e)[:200]}"

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_close_window(int(window_id))
        if result.get("success"):
            return (
                f"Closed OS-level root desktop window {window_id}; the whole application window may now be gone. "
                f"Call agentbay_computer_screenshot to verify."
            )
        return f"Failed to close window {window_id}: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer close_window failed")
        return f"Close window failed: {str(e)[:200]}"


async def _agentbay_computer_dismiss_dialog(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Safely dismiss the current in-app popup/dialog without closing root windows."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    title = str(arguments.get("title") or "").strip()
    window_id = arguments.get("window_id")

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)

        if window_id is not None:
            return (
                "agentbay_computer_dismiss_dialog does not close root desktop windows. "
                "It only sends Escape to the active in-app popup/dialog. "
                "For in-app tabs, embedded panels, marketplace/store windows, or document tabs, use the app UI "
                "or shortcuts such as Ctrl+W. If the user explicitly wants to close/quit a whole desktop window "
                "or app, call agentbay_computer_close_window with a window_id returned by "
                "agentbay_computer_list_windows."
            )

        esc_result = await client.computer_press_keys(["esc"])
        if esc_result.get("success"):
            title_note = f" Target hint: `{title}`." if title else ""
            return (
                f"Sent Escape to safely dismiss the active in-app popup/dialog.{title_note} "
                f"Call agentbay_computer_screenshot to verify. This tool never closes the root application window; "
                f"if Escape does not affect an in-app tab or embedded panel, use that app's own close control "
                f"or a shortcut such as Ctrl+W instead of root-window close."
            )

        return (
            f"Could not send Escape to dismiss the active popup/dialog: "
            f"{esc_result.get('error_message', 'Unknown error')}. "
            f"Do not use this tool to close root application windows."
        )
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer dismiss_dialog failed")
        return f"Dismiss dialog failed: {str(e)[:200]}"


async def _agentbay_computer_list_visible_apps(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """List currently visible/running applications."""
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    try:
        _session_id = arguments.pop("_session_id", "")
        client = await get_agentbay_client_for_agent(agent_id, "computer", session_id=_session_id)
        result = await client.computer_list_visible_apps()
        if result.get("success"):
            import json

            apps = result.get("apps", [])
            if not apps:
                return "No visible applications running."
            apps_str = json.dumps(apps, ensure_ascii=False, indent=2)
            return f"Visible applications ({len(apps)}):\n\n{apps_str[:3000]}"
        return f"Failed to list applications: {result.get('error_message', 'Unknown error')}"
    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] Computer list_visible_apps failed")
        return f"List applications failed: {str(e)[:200]}"


async def _agentbay_file_transfer(agent_id: Optional[uuid.UUID], ws: Path, arguments: dict) -> str:
    """Transfer a file between workspace and an AgentBay environment, or between two environments.

    Supported transfer directions:
      - workspace  → env:      upload_file(local_workspace_path, remote_path)   [single SDK call]
      - env        → workspace: download_file(remote_path, local_workspace_path) [single SDK call]
      - env A      → env B:    download to /tmp/<uuid>, upload to env B, cleanup /tmp [transparent]

    The 'local' side of the SDK calls is always the platform backend server,
    which has access to the agent workspace directory.
    """
    if not agent_id:
        return "AgentBay tools require agent context"

    from app.services.agentbay_client import get_agentbay_client_for_agent

    from_type = arguments.get("from_type", "")
    from_path = arguments.get("from_path", "")
    to_type = arguments.get("to_type", "")
    to_path = arguments.get("to_path", "")
    session_id = arguments.pop("_session_id", "")

    if not all([from_type, from_path, to_type, to_path]):
        return "Missing required parameters: from_type, from_path, to_type, to_path"

    # Reject no-op transfers
    if from_type == "workspace" and to_type == "workspace":
        return "Cannot transfer workspace → workspace. Use write_file or workspace tools instead."
    if from_type == to_type and from_type != "workspace":
        return f"Same environment ({from_type}) transfer: use agentbay_command_exec with 'cp' to copy files within the same environment."

    env_types = {"browser", "computer", "code"}

    # ── Helper: resolve and validate a workspace-relative path ──────────────
    def resolve_workspace(rel_path: str) -> tuple[str | None, str]:
        """Return (absolute_local_path_str, error_message). error_message is '' on success."""
        local = (ws / rel_path).resolve()
        if not str(local).startswith(str(ws.resolve())):
            return None, "Permission denied: path must be inside the agent workspace"
        return str(local), ""

    try:
        # ── Case 1: workspace → env ──────────────────────────────────────────
        if from_type == "workspace" and to_type in env_types:
            local_path, err = resolve_workspace(from_path)
            if err:
                return err
            import os

            if not os.path.exists(local_path):
                return f"File not found in workspace: {from_path}"
            client = await get_agentbay_client_for_agent(agent_id, to_type, session_id=session_id)
            result = await asyncio.to_thread(client._session.file_system.upload_file, local_path, to_path)
            if result.success:
                msg = f"Transferred workspace/{from_path} → [{to_type}]{to_path} ({result.bytes_sent} bytes)"
                # After uploading to the computer desktop directory, notify the GNOME
                # file manager so the file icon appears immediately without manual refresh.
                desktop_dir = "/home/wuying/桌面"
                if to_type == "computer" and to_path.startswith(desktop_dir):
                    try:
                        await asyncio.to_thread(
                            client._session.command.exec, f"DISPLAY=:0 gio info '{to_path}' 2>/dev/null || true"
                        )
                    except Exception:
                        pass  # Non-critical: desktop refresh failure doesn't affect transfer result
                return msg
            return f"Upload failed: {result.error_message}"

        # ── Case 2: env → workspace ──────────────────────────────────────────
        elif from_type in env_types and to_type == "workspace":
            local_path, err = resolve_workspace(to_path)
            if err:
                return err
            import os

            os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
            client = await get_agentbay_client_for_agent(agent_id, from_type, session_id=session_id)
            result = await asyncio.to_thread(client._session.file_system.download_file, from_path, local_path)
            if result.success:
                return (
                    f"Transferred [{from_type}]{from_path} → workspace/{to_path} "
                    f"({result.bytes_received} bytes). "
                    f"File available in workspace at: {to_path}"
                )
            return f"Download failed: {result.error_message}"

        # ── Case 3: env A → env B (transparent /tmp/ intermediary) ──────────
        elif from_type in env_types and to_type in env_types:
            import uuid as _uuid
            import os

            tmp_path = f"/tmp/agentbay_transfer_{_uuid.uuid4().hex}"
            try:
                # Step 1: download from source env to backend /tmp/
                src_client = await get_agentbay_client_for_agent(agent_id, from_type, session_id=session_id)
                dl_result = await asyncio.to_thread(src_client._session.file_system.download_file, from_path, tmp_path)
                if not dl_result.success:
                    return f"Transfer failed (download from {from_type}): {dl_result.error_message}"

                # Step 2: upload from backend /tmp/ to destination env
                dst_client = await get_agentbay_client_for_agent(agent_id, to_type, session_id=session_id)
                ul_result = await asyncio.to_thread(dst_client._session.file_system.upload_file, tmp_path, to_path)
                if not ul_result.success:
                    return f"Transfer failed (upload to {to_type}): {ul_result.error_message}"

                return f"Transferred [{from_type}]{from_path} → [{to_type}]{to_path} ({dl_result.bytes_received} bytes)"
            finally:
                # Always clean up the temporary file regardless of success or failure
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except Exception:
                    pass  # Non-critical: ignore cleanup errors

        else:
            return f"Unsupported transfer: {from_type} → {to_type}"

    except RuntimeError as e:
        return f"{str(e)}"
    except Exception as e:
        logger.exception(f"[AgentBay] File transfer failed for agent {agent_id}")
        return f"File transfer failed: {str(e)[:200]}"
