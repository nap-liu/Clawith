"""Project-scoped tools exposed only inside a frozen project child runtime."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import func, select

from app.database import async_session
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.project import (
    Project,
    ProjectCapabilityBinding,
    ProjectEvent,
    ProjectMemberSnapshot,
    ProjectRun,
    ProjectWorkItem,
)
from app.models.subagent_run import SubagentRun
from app.services.project_git_service import (
    ProjectSandboxWorkspace,
    commit_project_changes,
    commit_project_workspace_sandbox_changes,
    delete_project_workspace_file,
    edit_project_workspace_file,
    find_project_workspace_files,
    list_project_workspace,
    materialize_project_read_workspace,
    move_project_workspace_path,
    project_agent_git_email,
    project_repository_commit_is_ancestor,
    read_project_workspace_file,
    repository_state,
    reset_project_repository_head,
    restore_as_new_commit,
    search_project_workspace,
    write_project_workspace_file,
)
from app.services.project_service import (
    add_event,
    deactivate_project_member,
    restore_project_member,
)

from functools import wraps

from app.services import project_runtime_execution as _runtime_execution
from app.services import project_runtime_tool_support as _runtime_support
from app.services.project_runtime_tool_catalog import (
    LEADER_ONLY_PROJECT_TOOLS,
    PARTICIPANT_PROJECT_TOOLS,
    PROJECT_FILE_WORKSPACES,
    PROJECT_RUNTIME_TOOL_NAMES,
    PROJECT_SANDBOX_TOOL_NAMES,
    PROJECT_STANDARD_FILE_TOOL_NAMES,
    PROJECT_STRUCTURED_READ_TOOL_NAMES,
    PROJECT_TOOL_REGISTRY,
    PROJECT_WORKSPACE_ROUTED_TOOL_NAMES,
    WORK_ITEM_EVIDENCE_MAX_CHARS,
    WORK_ITEM_EVIDENCE_MAX_ITEMS,
    WORK_ITEM_EVENT_SCAN_MAX,
    WORK_ITEM_PRIORITIES,
    WORK_ITEM_PROGRESS_MAX_CHARS,
    WORK_ITEM_STATUSES,
    _bounded_context_text,
    _milestone_operation_event,
    _milestone_operation_key,
    add_project_workspace_parameter,
    effective_project_tool_names,
    project_runtime_tool_schemas,
)


def _sync_runtime_module_dependencies() -> None:
    _runtime_support.async_session = async_session
    _runtime_execution.async_session = async_session


def _async_runtime_proxy(function):
    @wraps(function)
    async def proxy(*args, **kwargs):
        _sync_runtime_module_dependencies()
        return await function(*args, **kwargs)

    proxy.__module__ = __name__
    return proxy


_resolve_uncertain_project_file_audit = _async_runtime_proxy(
    _runtime_support._resolve_uncertain_project_file_audit
)




_uuid = _runtime_support._uuid
load_project_runtime_scope = _runtime_support.load_project_runtime_scope
_runtime_scope = _async_runtime_proxy(_runtime_support._runtime_scope)
execute_project_workspace_tool = _async_runtime_proxy(
    _runtime_support.execute_project_workspace_tool
)
resolve_project_sandbox_scope = _async_runtime_proxy(
    _runtime_support.resolve_project_sandbox_scope
)
finalize_project_sandbox_changes = _async_runtime_proxy(
    _runtime_support.finalize_project_sandbox_changes
)
_enabled_member = _runtime_support._enabled_member
_dependency_ids = _runtime_support._dependency_ids
_project_context = _async_runtime_proxy(_runtime_support._project_context)
_list_work_items = _async_runtime_proxy(_runtime_support._list_work_items)


execute_project_runtime_tool = _async_runtime_proxy(
    _runtime_execution.execute_project_runtime_tool
)
