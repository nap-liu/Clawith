"""REST API for closed-loop AI-native project management."""

import hashlib
import json
import posixpath
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from jose import JWTError, jwt
from loguru import logger
from sqlalchemy import and_, delete, func, or_, select, text, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.events import get_redis
from app.core.permissions import build_visible_agents_query, is_platform_admin_user
from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.project import (
    Project,
    ProjectAccessGrant,
    ProjectCapabilityBinding,
    ProjectEvent,
    ProjectMemberSnapshot,
    ProjectRepositoryOperation,
    ProjectRun,
    ProjectRunMemberSnapshot,
    ProjectTemplate,
    ProjectWorkItem,
)
from app.models.subagent_run import SubagentRun
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.schemas.project import (
    A2AWakeRequest,
    CapabilityOut,
    CapabilityUpdate,
    GitBranchRequest,
    GitCloneRequest,
    GitCommitRequest,
    GitRemoteRequest,
    GitRestoreRequest,
    LeaderUpdate,
    ProjectAccessGrantCreate,
    ProjectAccessGrantOut,
    ProjectAgentCreate,
    ProjectAgentLifecycleRequest,
    ProjectAgentOut,
    ProjectAgentPromoteRequest,
    ProjectAgentPromotionOut,
    ProjectAgentUpdate,
    ProjectCapabilityCreate,
    ProjectCreate,
    ProjectEventCreate,
    ProjectEventOut,
    ProjectFileWriteRequest,
    ProjectFromTemplateCreate,
    ProjectGroupMessageCreate,
    ProjectKickoffConfirm,
    ProjectMemberCreate,
    ProjectMemberLifecycleRequest,
    ProjectMemberOut,
    ProjectMemberToolUpdate,
    ProjectMemberUpdate,
    ProjectMilestoneOut,
    ProjectRunCreate,
    ProjectRunMemberSnapshotOut,
    ProjectRunOut,
    ProjectRunUpdate,
    ProjectSettingsUpdate,
    ProjectSkillBackfillRequest,
    ProjectTemplateCreate,
    ProjectTemplateFromProjectCreate,
    ProjectTemplateFromProjectUpdate,
    ProjectUpdate,
    WorkItemCreate,
    WorkItemDetailOut,
    WorkItemOut,
    WorkItemUpdate,
)
from app.services.org_directory import (
    permission_directory_departments,
    permission_directory_members,
)
from app.services.project_agent_service import (
    create_project_agent,
    deactivate_project_agent,
    promote_project_agent,
    restore_project_agent,
    serialize_project_agent,
    update_project_agent,
)
from app.services.project_agent_service import (
    get_project_agent as get_project_agent_record,
)
from app.services.project_agent_service import (
    list_project_agents as list_project_agent_records,
)
from app.services.project_agent_template_assets import (
    ProjectAgentTemplateAssetError,
    sanitize_project_agent_template_assets,
)
from app.services.project_agent_template_service import (
    export_project_agents_for_template,
    export_project_capabilities_for_template,
    instantiate_project_agents_from_template,
    instantiate_project_capabilities_from_template,
)
from app.services.project_capability_contract import serialize_project_capability
from app.services.project_capability_options import load_project_capability_options
from app.services.project_collaboration_prompt import build_project_kickoff_task
from app.services.project_git_service import (
    apply_project_repository_clone,
    begin_project_repository_clone,
    commit_project_changes,
    create_branch,
    delete_git_remote,
    finalize_project_repository_clone,
    inspect_project_directory,
    inspect_project_file,
    inspect_project_file_at,
    iter_project_directory_archive,
    iter_project_file_blob,
    list_git_remotes,
    list_project_files,
    project_commit_diff,
    project_user_git_email,
    put_git_remote,
    read_project_file_content,
    reconcile_project_repository_operations,
    release_project_repository_clone_lock,
    remove_project_repository,
    repository_state,
    restore_as_new_commit,
    rollback_project_repository_clone,
    write_project_file,
)
from app.services.project_group_timeline import (
    build_project_group_timeline,
    serialize_project_group_message,
)
from app.services.project_member_runtime import PROJECT_AGENT_DEFAULT_TOOL_NAMES
from app.services.project_service import (
    accessible_projects_clause,
    add_capability,
    add_event,
    add_member,
    apply_run_status,
    can_manage_project_execution_user,
    create_project,
    deactivate_project_member,
    deliver_project_a2a,
    ensure_project_accepts_group_message,
    ensure_enabled_project_leader,
    ensure_project_group_session,
    ensure_project_leader_session,
    ensure_project_running,
    freeze_run_members,
    project_execution_user_id,
    project_summary,
    reconcile_project_runs,
    replace_access_grants,
    require_owner,
    require_project,
    resolve_project_execution_user,
    restore_project_member,
    serialize_project_events,
    serialize_project_run_member_snapshots,
    serialize_project_runs,
)
from app.services.project_skill_assets import (
    apply_project_skill_backfill,
    bind_library_skill_to_project_agent,
    delete_project_skill_asset,
    export_project_skills_for_template,
    instantiate_project_skills_from_template,
    plan_project_skill_backfill,
    project_skill_deletion_impact,
    project_skill_manifest,
    refresh_project_skill_asset,
    rollback_project_skill_backfill,
    set_project_skill_enabled,
)
from app.services.project_template_snapshot import (
    ProjectHeadSnapshot,
    ProjectTemplateSnapshotError,
    capture_project_head_snapshot,
    materialize_snapshot_agent_files,
    public_template_definition,
    restore_project_template_files,
    sanitize_template_settings,
)
from app.services.tool_enablement import tool_is_required

router = APIRouter(prefix="/projects", tags=["projects"])
_PROJECT_FILE_TICKET_TTL_SECONDS = 15 * 60
_PROJECT_AGENT_IDENTITY_PATH = re.compile(r"^\.agents/[^/]+/(?:soul|memory)\.md$")
_PROJECT_PLANNING_RUN_TRIGGERS = {
    "group_leader_message",
    "group_mention",
    "leader_reply_batch",
}


def _is_project_agent_identity_path(path: str) -> bool:
    """Recognize owner-managed Agent identity files after POSIX normalization."""

    normalized = posixpath.normpath(path.replace("\\", "/")).lstrip("/")
    return bool(_PROJECT_AGENT_IDENTITY_PATH.fullmatch(normalized))


async def _guard_project_agent_identity_paths(
    db: AsyncSession,
    user: User,
    project: Project,
    *paths: str,
) -> None:
    """Require the project owner before a generic file API changes Agent identity."""

    if any(_is_project_agent_identity_path(path) for path in paths):
        await require_owner(db, user, project.id)


async def _guard_project_agent_member_mutation(
    db: AsyncSession,
    user: User,
    project: Project,
    member: ProjectMemberSnapshot,
) -> None:
    """Prevent edit-role users from changing project-owned Agent membership."""

    agent = await db.get(Agent, member.agent_id)
    if agent is not None and agent.scope == "project" and agent.project_id == project.id:
        await require_owner(db, user, project.id)


def _create_project_file_ticket(user: User, project: Project, metadata: dict) -> str:
    settings = get_settings()
    expires_at = int(datetime.now(UTC).timestamp()) + _PROJECT_FILE_TICKET_TTL_SECONDS
    return jwt.encode(
        {
            "sub": str(user.id),
            "tenant_id": str(project.tenant_id),
            "project_id": str(project.id),
            "path": metadata["path"],
            "object_id": metadata["object_id"],
            "purpose": "project_file",
            "exp": expires_at,
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def _create_project_snapshot_ticket(
    user: User,
    project: Project,
    *,
    purpose: str,
    path: str,
    head: str,
) -> str:
    settings = get_settings()
    expires_at = int(datetime.now(UTC).timestamp()) + _PROJECT_FILE_TICKET_TTL_SECONDS
    return jwt.encode(
        {
            "sub": str(user.id),
            "tenant_id": str(project.tenant_id),
            "project_id": str(project.id),
            "path": path,
            "head": head,
            "purpose": purpose,
            "exp": expires_at,
        },
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


async def _authorize_project_snapshot_ticket(
    db: AsyncSession,
    project_id: uuid.UUID,
    ticket: str,
    *,
    purpose: str,
) -> tuple[User, Project, dict]:
    settings = get_settings()
    try:
        payload = jwt.decode(ticket, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_id = uuid.UUID(str(payload.get("sub")))
    except (JWTError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired project snapshot ticket") from exc
    if (
        payload.get("purpose") != purpose
        or payload.get("project_id") != str(project_id)
        or not isinstance(payload.get("head"), str)
        or not isinstance(payload.get("path"), str)
    ):
        raise HTTPException(status_code=401, detail="Project snapshot ticket does not match this resource")
    user = (await db.execute(select(User).where(User.id == user_id, User.is_active.is_(True)))).scalar_one_or_none()
    if user is None or payload.get("tenant_id") != str(user.tenant_id):
        raise HTTPException(status_code=401, detail="Project snapshot ticket user is unavailable")
    project = await require_project(db, user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    return user, project, payload


async def _authorize_project_file_ticket(
    db: AsyncSession,
    project_id: uuid.UUID,
    path: str,
    ticket: str,
) -> tuple[User, Project, dict]:
    settings = get_settings()
    try:
        payload = jwt.decode(ticket, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_id = uuid.UUID(str(payload.get("sub")))
    except (JWTError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired project file ticket") from exc
    if (
        payload.get("purpose") != "project_file"
        or payload.get("project_id") != str(project_id)
        or payload.get("path") != path
    ):
        raise HTTPException(status_code=401, detail="Project file ticket does not match this resource")
    user = (await db.execute(select(User).where(User.id == user_id, User.is_active.is_(True)))).scalar_one_or_none()
    if user is None or payload.get("tenant_id") != str(user.tenant_id):
        raise HTTPException(status_code=401, detail="Project file ticket user is unavailable")
    project = await require_project(db, user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    metadata = await inspect_project_file(project, path)
    if payload.get("object_id") != metadata["object_id"]:
        raise HTTPException(status_code=409, detail="Project file changed; refresh its preview URL")
    return user, project, metadata


def _parse_project_file_range(value: str | None, size: int) -> tuple[int, int, bool]:
    if size <= 0:
        if value:
            raise ValueError("range outside empty object")
        return 0, -1, False
    if not value:
        return 0, size - 1, False
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("invalid range")
    start_raw, separator, end_raw = value[6:].strip().partition("-")
    if not separator:
        raise ValueError("invalid range")
    if not start_raw:
        suffix = int(end_raw)
        if suffix <= 0:
            raise ValueError("invalid suffix")
        return max(0, size - suffix), size - 1, True
    start = int(start_raw)
    if start < 0 or start >= size:
        raise ValueError("range start outside object")
    end = int(end_raw) if end_raw else size - 1
    if end < start:
        raise ValueError("range end before start")
    return start, min(end, size - 1), True


def _tenant_id(user: User) -> uuid.UUID:
    if user.tenant_id is None:
        raise HTTPException(status_code=403, detail="A tenant membership is required")
    return user.tenant_id


def _merge_settings(current: dict, patch: dict) -> dict:
    """Recursively merge project-local settings without mutating ORM JSON in place."""

    merged = dict(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_settings(merged[key], value)
        else:
            merged[key] = value
    return merged


def _record_git_head(project: Project, commit: str) -> None:
    settings = dict(project.settings or {})
    settings["git"] = {**dict(settings.get("git") or {}), "head": commit}
    project.settings = settings


def _record_git_repository_settings(
    project: Project,
    *,
    remotes: list[dict],
    source: str | None = None,
    head: str | None = None,
    default_branch: str | None = None,
) -> None:
    settings = dict(project.settings or {})
    safe_remotes = [
        {
            "name": str(remote.get("name") or ""),
            "url_sha256": hashlib.sha256(str(remote.get("url") or "").encode("utf-8")).hexdigest(),
        }
        for remote in remotes
    ]
    git_settings = {
        **dict(settings.get("git") or {}),
        "mode": "managed",
        "repository_mode": "managed",
        # Full remote URLs are owner-only and live in Git config. Project
        # settings are included in viewer dashboards, so keep only safe refs.
        "remotes": safe_remotes,
        "remote_count": len(safe_remotes),
    }
    if source is not None:
        git_settings["source"] = source
    if head is not None:
        git_settings["head"] = head
    if default_branch is not None:
        git_settings["default_branch"] = default_branch
    settings["git"] = git_settings
    project.settings = settings


def _git_remote_audit_metadata(remote: dict, *, history_changed: bool) -> dict:
    """Return viewer-safe remote evidence without persisting repository URLs."""

    url = str(remote.get("url") or "")
    return {
        "remote_name": str(remote.get("name") or ""),
        "remote_url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
        "history_changed": history_changed,
    }


def _can_manage_template(user: User, template: ProjectTemplate) -> bool:
    if is_platform_admin_user(user):
        return True
    if user.role == "org_admin":
        return bool(user.tenant_id is not None and template.tenant_id == user.tenant_id)
    if user.tenant_id is None or template.tenant_id != user.tenant_id:
        return False
    return template.created_by_user_id == user.id


def _visible_template_clause(user: User):
    if is_platform_admin_user(user):
        return true()
    tenant_id = _tenant_id(user)
    if user.role == "org_admin":
        return or_(
            ProjectTemplate.is_published.is_(True),
            ProjectTemplate.tenant_id == tenant_id,
        )
    return and_(
        or_(
            ProjectTemplate.tenant_id.is_(None),
            ProjectTemplate.tenant_id == tenant_id,
        ),
        or_(
            ProjectTemplate.is_published.is_(True),
            ProjectTemplate.created_by_user_id == user.id,
        ),
    )


def _manageable_template_clause(user: User):
    if is_platform_admin_user(user):
        return true()
    tenant_id = _tenant_id(user)
    if user.role == "org_admin":
        return ProjectTemplate.tenant_id == tenant_id
    return and_(
        ProjectTemplate.tenant_id == tenant_id,
        ProjectTemplate.created_by_user_id == user.id,
    )


async def _require_template(
    db: AsyncSession,
    user: User,
    template_id: uuid.UUID,
    *,
    manage: bool = False,
    lock: bool = False,
) -> ProjectTemplate:
    clause = _manageable_template_clause(user) if manage else _visible_template_clause(user)
    stmt = select(ProjectTemplate).where(ProjectTemplate.id == template_id, clause)
    if lock:
        stmt = stmt.with_for_update()
    template = (await db.execute(stmt)).scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="Project template not found")
    return template


async def _template_payload(db: AsyncSession, template: ProjectTemplate, user: User) -> dict:
    definition = template.definition or {}
    public_definition = public_template_definition(definition) if "project_snapshot" in definition else definition
    usage_count = (
        await db.execute(select(func.count(Project.id)).where(Project.template_id == template.id))
    ).scalar_one()
    author_name = None
    if template.created_by_user_id:
        author_name = (
            await db.execute(select(User.display_name).where(User.id == template.created_by_user_id))
        ).scalar_one_or_none()
    can_manage = _can_manage_template(user, template)
    return {
        "id": str(template.id),
        "tenant_id": str(template.tenant_id) if template.tenant_id else None,
        "created_by_user_id": str(template.created_by_user_id) if template.created_by_user_id else None,
        "name": template.name,
        "description": template.description,
        "category": template.category,
        "version": template.version,
        "is_published": template.is_published,
        "can_edit": can_manage,
        "can_delete": can_manage,
        "definition": public_definition,
        "author_name": author_name or "平台模板",
        "usage_count": usage_count,
        "featured": bool(public_definition.get("featured", False)),
        "objective_hint": public_definition.get("goal") or public_definition.get("objective", ""),
        "success_criteria": public_definition.get("success_criteria", []),
        "roles": public_definition.get("roles", []),
        "skills": public_definition.get("skills", []),
        "mcp_servers": public_definition.get("mcp_servers", []),
        "created_at": template.created_at.isoformat() if template.created_at else None,
        "updated_at": template.updated_at.isoformat() if template.updated_at else None,
    }


async def _build_project_template_definition(
    db: AsyncSession,
    project: Project,
    *,
    included_skill_binding_ids: list[uuid.UUID] | None = None,
) -> tuple[dict, ProjectHeadSnapshot]:
    """Build one sanitized definition from an immutable project HEAD."""

    snapshot = await capture_project_head_snapshot(project)
    with materialize_snapshot_agent_files(snapshot) as snapshot_root:
        template_agents = await export_project_agents_for_template(
            db,
            project,
            project_root=snapshot_root,
        )
        template_skills = await export_project_skills_for_template(
            db,
            project,
            included_binding_ids=included_skill_binding_ids or [],
            project_root=snapshot_root,
        )
    return (
        {
            "schema_version": 1,
            "goal": project.goal,
            "success_criteria": list(project.success_criteria or []),
            "settings": sanitize_template_settings(project.settings or {}),
            "agents": template_agents,
            "skill_assets": template_skills,
            "capabilities": await export_project_capabilities_for_template(db, project),
            "project_snapshot": snapshot.project_files,
        },
        snapshot,
    )

def _group_session_payload(session: ChatSession, project: Project) -> dict:
    policies = dict((project.settings or {}).get("policies") or {})
    mention_limit = min(8, max(1, int(policies.get("max_group_mentions_per_message", 4))))
    configured_wake_budget = max(0, int(policies.get("max_a2a_wakes", mention_limit)))
    # A Human message always gets one project-owner turn, even when an old project
    # policy configured a zero A2A wake budget. The remainder is available to
    # explicit mentions of other members.
    max_mentions = min(mention_limit, max(0, max(1, configured_wake_budget) - 1))
    return {
        "id": str(session.id),
        "project_id": str(session.project_id) if session.project_id else None,
        "title": session.title,
        "group_name": session.group_name,
        "source_channel": session.source_channel,
        "access_agent_id": str(session.agent_id),
        "max_mentions": max_mentions,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "last_message_at": session.last_message_at.isoformat() if session.last_message_at else None,
    }


def _group_message_payload(message: ChatMessage) -> dict:
    return serialize_project_group_message(message)


def _leader_session_payload(session: ChatSession, discussion_count: int) -> dict:
    config = dict(session.im_config or {})
    return {
        "id": str(session.id),
        "project_id": str(session.project_id) if session.project_id else None,
        "agent_id": str(session.agent_id),
        "user_id": str(session.user_id) if session.user_id else None,
        "title": session.title,
        "source_channel": session.source_channel,
        "is_group": False,
        "read_only": bool(config.get("read_only")),
        "planning_transport": config.get("planning_transport", "leader_session"),
        "discussion_count": discussion_count,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "last_message_at": session.last_message_at.isoformat() if session.last_message_at else None,
    }


def _uses_project_group_planning(project: Project) -> bool:
    planning = dict((project.settings or {}).get("planning") or {})
    return planning.get("conversation_mode") == "project_group"


def _has_unfinished_direct_planning_turn(messages: list[ChatMessage]) -> bool:
    """Detect the latest Human anchor without its terminal owner reply.

    Modern Web replies identify their exact Human anchor. Older planning rows did
    not, so one later plain assistant row remains the compatibility completion
    signal. Tool-call rows never satisfy the barrier.
    """

    latest_human_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].role == "user" and messages[index].sender_user_id is not None
        ),
        None,
    )
    if latest_human_index is None:
        return False
    human = messages[latest_human_index]
    for message in messages[latest_human_index + 1 :]:
        if message.role != "assistant":
            continue
        anchor_id = dict(message.message_meta or {}).get("turn_anchor_id")
        if anchor_id is None or str(anchor_id) == str(human.id):
            return False
    return True


async def _has_active_project_planning_run(db: AsyncSession, project: Project) -> bool:
    active_id = (
        await db.execute(
            select(ProjectRun.id)
            .where(
                ProjectRun.project_id == project.id,
                ProjectRun.tenant_id == project.tenant_id,
                ProjectRun.trigger_type.in_(_PROJECT_PLANNING_RUN_TRIGGERS),
                ProjectRun.status.in_(["queued", "running"]),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return active_id is not None


def _kickoff_transcript(
    project: Project,
    messages: list[ChatMessage],
    confirmation: str,
    confirmed_at: datetime,
) -> str:
    lines = [
        f"# {project.name} · 项目启动记录",
        "",
        f"项目目标：{project.goal}",
        "",
        "## 方案讨论",
        "",
    ]
    role_labels = {"user": "用户", "assistant": "负责人"}
    for message in messages:
        if message.role not in role_labels:
            continue
        actor = role_labels.get(message.role, "参与者")
        timestamp = message.created_at.isoformat() if message.created_at else "时间未记录"
        lines.extend([f"### {actor} · {timestamp}", ""])
        content = message.content.strip() or "（无内容）"
        lines.extend([f"> {line}" if line else ">" for line in content.splitlines()])
        lines.append("")
    lines.extend(["## 方案确认", "", f"确认时间：{confirmed_at.isoformat()}", "", confirmation, ""])
    return "\n".join(lines)

__all__ = [name for name in globals() if not name.startswith("__")]
