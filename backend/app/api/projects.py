"""REST API for closed-loop AI-native project management."""

import sys
from types import ModuleType

from app.api import projects_shared as _projects_shared
from app.api.projects_shared import *  # noqa: F401,F403
from app.api import (
    projects_bootstrap as _projects_bootstrap,
    projects_templates_manage as _projects_templates_manage,
    projects_project_api as _projects_project_api,
    projects_access_members as _projects_access_members,
    projects_capabilities as _projects_capabilities,
    projects_work_items as _projects_work_items,
    projects_runs_events as _projects_runs_events,
    projects_group_sessions as _projects_group_sessions,
    projects_group_messages as _projects_group_messages,
    projects_git_files as _projects_git_files,
)

_MODULE_EXPORTS: tuple[tuple[ModuleType, tuple[str, ...]], ...] = (
    (
        _projects_bootstrap,
        (
            "list_project_templates",
            "create_project_template",
            "get_project_template",
            "delete_project_template",
            "get_project_bootstrap_options",
            "create_project_from_template",
            "_create_project_from_template",
        ),
    ),
    (
        _projects_templates_manage,
        (
            "create_project_template_editor",
            "list_projects",
            "post_project",
            "create_template_from_project",
            "update_template_from_project",
            "get_project_template_manifest",
        ),
    ),
    (
        _projects_project_api,
        (
            "get_project",
            "get_project_directory_departments",
            "get_project_directory_members",
            "get_project_dashboard",
            "patch_project",
            "get_project_settings",
            "patch_project_settings",
            "delete_project",
        ),
    ),
    (
        _projects_access_members,
        (
            "list_access_grants",
            "create_access_grant",
            "delete_access_grant",
            "list_project_members",
            "list_project_agents",
            "create_project_owned_agent",
            "get_project_owned_agent",
            "patch_project_owned_agent",
            "deactivate_project_owned_agent",
            "restore_project_owned_agent",
            "promote_project_owned_agent",
            "create_project_member",
            "_load_project_member",
            "remove_project_member",
            "restore_removed_project_member",
            "patch_project_member",
            "_project_member_tools_payload",
            "get_project_member_tools",
            "put_project_member_tools",
            "put_project_leader",
        ),
    ),
    (
        _projects_capabilities,
        (
            "list_project_capabilities",
            "project_skill_backfill",
            "create_project_capability",
            "get_project_skill_delete_impact",
            "delete_project_skill_capability",
            "refresh_project_skill_capability",
            "patch_project_capability",
            "_project_capability_binding",
        ),
    ),
    (
        _projects_work_items,
        (
            "_require_member_agent",
            "_validate_work_item_links",
            "list_work_items",
            "get_work_item_detail",
            "create_work_item",
            "patch_work_item",
        ),
    ),
    (
        _projects_runs_events,
        (
            "list_project_runs",
            "create_project_run",
            "patch_project_run",
            "list_run_snapshots",
            "list_project_events",
            "create_project_event",
        ),
    ),
    (
        _projects_group_sessions,
        (
            "get_project_leader_session",
            "confirm_project_kickoff",
            "get_project_group_session",
            "list_project_group_messages",
        ),
    ),
    (
        _projects_group_messages,
        (
            "create_project_group_message",
            "wake_project_agent",
        ),
    ),
    (
        _projects_git_files,
        (
            "get_git_remotes",
            "put_project_git_remote",
            "delete_project_git_remote",
            "clone_project_git_repository",
            "create_git_restore",
            "get_project_files",
            "get_project_file_content",
            "get_project_file_raw",
            "get_project_directory_archive_ticket",
            "get_project_directory_archive",
            "get_project_html_preview_resource",
            "put_project_file",
            "create_git_commit",
            "create_git_branch",
            "list_project_milestones",
            "get_git_state",
            "get_git_diff",
        ),
    ),
)

_SHARED_SYMBOLS = (
    "_is_project_agent_identity_path",
    "_guard_project_agent_identity_paths",
    "_guard_project_agent_member_mutation",
    "_create_project_file_ticket",
    "_create_project_snapshot_ticket",
    "_authorize_project_snapshot_ticket",
    "_authorize_project_file_ticket",
    "_parse_project_file_range",
    "_tenant_id",
    "_merge_settings",
    "_record_git_head",
    "_record_git_repository_settings",
    "_git_remote_audit_metadata",
    "_can_manage_template",
    "_visible_template_clause",
    "_manageable_template_clause",
    "_require_template",
    "_template_payload",
    "_build_project_template_definition",
    "_group_session_payload",
    "_group_message_payload",
    "_leader_session_payload",
    "_uses_project_group_planning",
    "_has_unfinished_direct_planning_turn",
    "_has_active_project_planning_run",
    "_kickoff_transcript",
)

for module, names in _MODULE_EXPORTS:
    for name in names:
        globals()[name] = module.__dict__[name]


def _prepare_projects_module(module: ModuleType, symbol_names: tuple[str, ...]) -> None:
    for symbol_name in symbol_names:
        module.__dict__[symbol_name].__module__ = __name__


_prepare_projects_module(_projects_shared, _SHARED_SYMBOLS)
for module, names in _MODULE_EXPORTS:
    _prepare_projects_module(module, names)

_SYNC_MODULES = (_projects_shared,) + tuple(module for module, _names in _MODULE_EXPORTS)


def _sync_root_exports() -> None:
    module = sys.modules[__name__]
    for name, value in tuple(module.__dict__.items()):
        if name.startswith("__"):
            continue
        for target in _SYNC_MODULES:
            if name in target.__dict__:
                target.__dict__[name] = value


class _ProjectsModule(ModuleType):
    def __setattr__(self, name: str, value) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for target in _SYNC_MODULES:
            if name in target.__dict__:
                target.__dict__[name] = value


sys.modules[__name__].__class__ = _ProjectsModule
_sync_root_exports()
