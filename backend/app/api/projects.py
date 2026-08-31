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


async def _ensure_builtin_templates(db: AsyncSession) -> None:
    found = (
        await db.execute(select(ProjectTemplate.id).where(ProjectTemplate.tenant_id.is_(None)).limit(1))
    ).scalar_one_or_none()
    if found:
        return
    definitions = [
        {
            "name": "产品研发冲刺",
            "category": "研发",
            "description": "由负责人驱动需求、研发、测试与交付闭环。",
            "definition": {
                "featured": True,
                "goal": "按验收标准交付一个可发布的产品增量",
                "success_criteria": ["关键路径通过", "评审与测试留痕", "产出进入 Git 历史"],
                "roles": ["负责人", "产品设计", "前端开发", "后端开发", "质量工程"],
                "skills": ["需求拆解", "代码评审"],
                "mcp_servers": ["GitHub"],
                "settings": {"runtime": {"max_parallel_runs": 4}},
            },
        },
        {
            "name": "市场洞察研究",
            "category": "研究",
            "description": "并行采集、交叉验证并形成有证据链的研究报告。",
            "definition": {
                "goal": "输出可追溯的市场研究报告",
                "success_criteria": ["来源可回溯", "结论经交叉评审"],
                "roles": ["研究负责人", "情报分析", "事实核查", "报告编辑"],
                "skills": ["深度研究", "事实核查"],
                "mcp_servers": ["Web Search"],
            },
        },
        {
            "name": "内容发布流水线",
            "category": "内容",
            "description": "从选题、创作、审校到多渠道发布的协作模板。",
            "definition": {
                "goal": "稳定产出符合品牌规范的内容",
                "success_criteria": ["审校通过", "发布物与素材均进入项目 Git"],
                "roles": ["内容负责人", "作者", "审校", "发布运营"],
                "skills": ["内容创作", "品牌审校"],
                "mcp_servers": [],
            },
        },
    ]
    for item in definitions:
        db.add(
            ProjectTemplate(
                tenant_id=None,
                created_by_user_id=None,
                name=item["name"],
                description=item["description"],
                category=item["category"],
                version="1.0.0",
                is_published=True,
                definition=item["definition"],
            )
        )
    await db.flush()


@router.get("/templates")
async def list_project_templates(
    category: str | None = None,
    q: str | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_builtin_templates(db)
    stmt = select(ProjectTemplate).where(_visible_template_clause(current_user))
    if category:
        stmt = stmt.where(ProjectTemplate.category == category)
    if q:
        stmt = stmt.where(ProjectTemplate.name.ilike(f"%{q}%"))
    templates = (await db.execute(stmt.order_by(ProjectTemplate.created_at.desc()))).scalars().all()
    return [await _template_payload(db, template, current_user) for template in templates]


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_project_template(
    data: ProjectTemplateCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    definition = dict(data.definition)
    if "project_snapshot" in definition:
        raise HTTPException(
            status_code=422,
            detail="Final project assets can only be published from an owned project",
        )
    if "roles" in definition and not isinstance(definition["roles"], list):
        raise HTTPException(status_code=422, detail="项目模板角色配置必须为列表。")
    template_agents = definition.get("agents", [])
    try:
        sanitized_agents = sanitize_project_agent_template_assets(template_agents)
        if "agents" in definition:
            definition["agents"] = sanitized_agents
    except ProjectAgentTemplateAssetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    template = ProjectTemplate(
        tenant_id=_tenant_id(current_user),
        created_by_user_id=current_user.id,
        **data.model_dump(exclude={"definition"}),
        definition=definition,
    )
    db.add(template)
    await db.flush()
    return await _template_payload(db, template, current_user)


@router.get("/templates/{template_id}")
async def get_project_template(
    template_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    template = await _require_template(db, current_user, template_id)
    return await _template_payload(db, template, current_user)


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_template(
    template_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    template = await _require_template(db, current_user, template_id, manage=True, lock=True)
    editor_projects = (
        (
            await db.execute(
                select(Project).where(
                    Project.settings["template_editor"]["template_id"].as_string() == str(template.id)
                )
            )
        )
        .scalars()
        .all()
    )
    for project in editor_projects:
        settings = dict(project.settings or {})
        settings.pop("template_editor", None)
        project.settings = settings
    await db.delete(template)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/bootstrap-options")
async def get_project_bootstrap_options(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    cache_key = f"projects:bootstrap:v1:{tenant_id}:{current_user.id}"
    try:
        cached = await (await get_redis()).get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception:
        logger.debug("Project bootstrap cache read failed; loading from the database")
    agents = (
        (await db.execute(build_visible_agents_query(current_user, tenant_id=tenant_id).order_by(Agent.name)))
        .scalars()
        .all()
    )
    platform_tools = (
        (
            await db.execute(
                select(Tool)
                .where(
                    Tool.enabled.is_(True),
                    Tool.source.in_(("builtin", "admin")),
                    or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
                )
                .order_by(Tool.category, Tool.display_name, Tool.name)
            )
        )
        .scalars()
        .all()
    )
    agent_installed_mcp_tools = (
        (
            await db.execute(
                select(AgentTool, Tool)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(
                    AgentTool.agent_id.in_([agent.id for agent in agents]),
                    AgentTool.enabled.is_(True),
                    Tool.enabled.is_(True),
                    Tool.type == "mcp",
                    Tool.source == "agent",
                    or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
                )
                .order_by(AgentTool.agent_id, Tool.mcp_server_name, Tool.display_name, Tool.name)
            )
        )
        .all()
    ) if agents else []
    users = (
        (
            await db.execute(
                select(User).where(User.tenant_id == tenant_id, User.is_active.is_(True)).order_by(User.display_name)
            )
        )
        .scalars()
        .all()
    )
    capability_options = await load_project_capability_options(db, tenant_id, agents)
    selectable_mcp_names = {
        uuid.UUID(str(capability["capability_id"])): str(capability["name"])
        for capability in capability_options.capabilities
        if capability.get("type") == "mcp"
        and capability.get("capability_id")
        and capability.get("name")
    }
    payload = {
        "agents": [
            {
                "id": str(agent.id),
                "name": agent.name,
                "role_description": agent.role_description,
                "avatar_url": agent.avatar_url,
                "status": agent.status,
                "agent_type": agent.agent_type,
                "primary_model_id": str(agent.primary_model_id) if agent.primary_model_id else None,
                "fallback_model_id": str(agent.fallback_model_id) if agent.fallback_model_id else None,
                "max_tool_rounds": agent.max_tool_rounds,
            }
            for agent in agents
        ],
        "tools": [
            {
                "id": str(tool.id),
                "name": tool.name,
                "display_name": tool.display_name,
                "description": tool.description,
                "category": tool.category,
                "type": tool.type,
                "icon": tool.icon,
                "source": tool.source,
                "agent_tool_source": None,
                "installed_by_agent_id": None,
                "config_schema": tool.config_schema or {},
                "agent_config": {},
                "mcp_server_id": str(tool.mcp_server_id) if tool.mcp_server_id else None,
                "mcp_server_name": selectable_mcp_names.get(
                    tool.mcp_server_id,
                    tool.mcp_server_name,
                ),
                "enabled": tool.type != "mcp" and (
                    tool_is_required(tool.name) or tool.name in PROJECT_AGENT_DEFAULT_TOOL_NAMES
                ),
                "can_disable": not tool_is_required(tool.name),
            }
            for tool in platform_tools
            if tool.type != "mcp"
            or (
                tool.mcp_server_id is not None
                and tool.mcp_server_id in capability_options.shared_mcp_ids
            )
        ] + [
            {
                "id": str(tool.id),
                "name": tool.name,
                "display_name": tool.display_name,
                "description": tool.description,
                "category": tool.category,
                "type": tool.type,
                "icon": tool.icon,
                "source": tool.source,
                "agent_tool_source": "user_installed",
                "installed_by_agent_id": str(assignment.agent_id),
                "config_schema": tool.config_schema or {},
                "agent_config": {},
                "mcp_server_id": str(tool.mcp_server_id) if tool.mcp_server_id else None,
                "mcp_server_name": selectable_mcp_names.get(
                    tool.mcp_server_id,
                    tool.mcp_server_name,
                ),
                "enabled": False,
                "can_disable": True,
            }
            for assignment, tool in agent_installed_mcp_tools
            if tool.mcp_server_id is not None
            and tool.mcp_server_id
            in capability_options.agent_mcp_ids.get(assignment.agent_id, frozenset())
        ],
        "users": [
            {
                "id": str(user.id),
                "name": user.display_name,
                "email": getattr(user, "email", None),
                "avatar_url": user.avatar_url,
            }
            for user in users
            if user.id != current_user.id
        ],
        "capabilities": [
            capability
            for capability in capability_options.capabilities
            if capability.get("type") == "skill"
        ],
    }
    try:
        await (await get_redis()).set(cache_key, json.dumps(payload, ensure_ascii=False), ex=30)
    except Exception:
        logger.debug("Project bootstrap cache write failed; returning database result")
    return payload


@router.post("/from-template", status_code=status.HTTP_201_CREATED)
async def create_project_from_template(
    data: ProjectFromTemplateCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _create_project_from_template(data, current_user, db)


async def _create_project_from_template(
    data: ProjectFromTemplateCreate,
    current_user: User,
    db: AsyncSession,
    *,
    editor_template_id: uuid.UUID | None = None,
):
    template = await _require_template(db, current_user, data.template_id)
    allowed_override_keys = {"members", "capabilities", "shared_with_user_ids"}
    unknown_override_keys = set(data.overrides) - allowed_override_keys
    if unknown_override_keys:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported project template overrides: {', '.join(sorted(unknown_override_keys))}",
        )
    stored_definition = dict(template.definition or {})
    definition = {**stored_definition, **data.overrides}
    template_agents = list(definition.get("agents") or [])
    if not template_agents:
        if "roles" in definition and not isinstance(definition["roles"], list):
            raise HTTPException(status_code=422, detail="项目模板角色配置必须为列表。")
        normalized_roles: list[tuple[str, str]] = []
        for role in list(definition.get("roles") or []):
            if isinstance(role, dict):
                role_name = str(role.get("name") or role.get("key") or "").strip()
                role_description = str(
                    role.get("description") or role.get("role_description") or ""
                ).strip()
            else:
                role_name = str(role).strip()
                role_description = ""
            if not role_name:
                continue
            normalized_roles.append(
                (
                    role_name[:200],
                    (role_description or f"负责{role_name}相关工作。")[:500],
                )
            )
        if not normalized_roles:
            normalized_roles = [("项目负责人", "负责项目整体协调与推进。")]
        template_agents = [
            {
                "name": role_name,
                "role_description": role_description,
                "is_leader": index == 0,
                "is_enabled": True,
            }
            for index, (role_name, role_description) in enumerate(normalized_roles)
        ]
    packaged_snapshot = stored_definition.get("project_snapshot")
    packaged_skill_assets = stored_definition.get("skill_assets", []) if packaged_snapshot is not None else []
    packaged_capabilities = stored_definition.get("capabilities", []) if packaged_snapshot is not None else []
    capabilities_overridden = "capabilities" in data.overrides
    template_capabilities_to_restore = (
        [
            item
            for item in packaged_capabilities
            if not capabilities_overridden or (isinstance(item, dict) and item.get("source") == "inherited")
        ]
        if packaged_snapshot is not None
        else list(definition.get("capabilities", []))
    )
    project_settings = dict(definition.get("settings", {}))
    if editor_template_id is not None:
        project_settings["template_editor"] = {"template_id": str(editor_template_id)}
    payload = ProjectCreate(
        name=data.name or template.name,
        description=data.description if data.description is not None else template.description,
        goal=definition.get("goal", definition.get("objective", "")),
        success_criteria=definition.get("success_criteria", []),
        visibility=data.visibility,
        template_id=template.id,
        settings=project_settings,
        members=definition.get("members", []),
        capabilities=definition.get("capabilities", []) if capabilities_overridden or packaged_snapshot is None else [],
        shared_with_user_ids=definition.get("shared_with_user_ids", []),
    )
    project = await create_project(
        db,
        current_user,
        payload,
        allow_template_agents=True,
    )
    try:
        await restore_project_template_files(
            project,
            packaged_snapshot,
            author_name=current_user.display_name,
            author_email=project_user_git_email(current_user.id),
        )
        created_agents = await instantiate_project_agents_from_template(
            db,
            project,
            current_user,
            template_agents,
        )
        if packaged_snapshot is not None:
            await instantiate_project_skills_from_template(
                db,
                project,
                current_user.display_name,
                current_user.id,
                packaged_skill_assets,
                [agent.id for agent, _member in created_agents],
            )
            await instantiate_project_capabilities_from_template(
                db,
                project,
                current_user,
                template_capabilities_to_restore,
                created_agents,
            )
    except Exception as exc:
        try:
            await remove_project_repository(project)
        except Exception:
            logger.exception("Failed to compensate project template storage for project {}", project.id)
        if isinstance(exc, (ProjectAgentTemplateAssetError, ProjectTemplateSnapshotError)):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise

    try:
        await ensure_project_group_session(db, project)
        await ensure_project_leader_session(db, project)
        explicit_leader_id = next(
            (member.agent_id for member in payload.members if member.is_leader),
            None,
        )
        if explicit_leader_id is not None:
            project_leader_id = (
                await db.execute(
                    select(Agent.id)
                    .where(
                        Agent.project_id == project.id,
                        Agent.tenant_id == project.tenant_id,
                        Agent.is_deleted.is_(False),
                        or_(
                            Agent.id == explicit_leader_id,
                            Agent.source_agent_id == explicit_leader_id,
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if project_leader_id is None:
                raise HTTPException(
                    status_code=422,
                    detail="Selected project owner is unavailable",
                )
            await db.execute(
                ProjectMemberSnapshot.__table__.update()
                .where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                )
                .values(is_leader=False)
            )
            await db.execute(
                ProjectMemberSnapshot.__table__.update()
                .where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id == project_leader_id,
                )
                .values(is_leader=True)
            )
            add_event(
                db,
                project,
                "leader.changed",
                "Applied the responsible person selected during template creation",
                actor_user_id=current_user.id,
                actor_agent_id=project_leader_id,
                metadata={"source": "template_override"},
            )
            await db.flush()
        git_state = await repository_state(project, limit=1)
        project.settings = {
            **dict(project.settings or {}),
            "git": {
                **dict(dict(project.settings or {}).get("git") or {}),
                "head": git_state.get("head"),
            },
        }
        if packaged_snapshot is not None:
            add_event(
                db,
                project,
                "project.template.restored",
                "Restored final project assets and digital employee configuration from a template",
                actor_user_id=current_user.id,
                metadata={
                    "template_id": str(template.id),
                    "file_count": len(packaged_snapshot.get("files", []))
                    if isinstance(packaged_snapshot, dict) and isinstance(packaged_snapshot.get("files"), list)
                    else 0,
                    "digital_employee_count": len(created_agents),
                    "skill_count": len(packaged_skill_assets)
                    if isinstance(packaged_skill_assets, list)
                    else 0,
                },
            )
        await db.flush()
        await db.refresh(project)
        summary = await project_summary(db, project, actor_user_id=current_user.id)
        restored_files = (
            packaged_snapshot.get("files", [])
            if isinstance(packaged_snapshot, dict) and isinstance(packaged_snapshot.get("files"), list)
            else []
        )
        portable_capabilities = [item for item in template_capabilities_to_restore if isinstance(item, dict)]
        restored_tool_ids = {
            str(item.get("capability_id"))
            for item in portable_capabilities
            if item.get("capability_type") == "tool" and item.get("capability_id")
        }
        restored_connection_ids = {
            str(item.get("capability_id"))
            for item in portable_capabilities
            if item.get("capability_type") == "mcp" and item.get("capability_id")
        }
        summary["template_setup_summary"] = {
            "restored_file_count": len(restored_files),
            "restored_digital_employee_count": len(created_agents),
            "restored_skill_count": len(packaged_skill_assets) if isinstance(packaged_skill_assets, list) else 0,
            "restored_connection_count": len(restored_connection_ids),
            "restored_tool_count": len(restored_tool_ids),
        }
        # Project rows and their managed repository form one product-level
        # creation boundary. Commit here so a database commit failure can still
        # compensate the repository before the request dependency exits.
        await db.commit()
        return summary
    except Exception:
        await db.rollback()
        try:
            await remove_project_repository(project)
        except Exception:
            logger.exception("Failed to compensate project template storage for project {}", project.id)
        raise


@router.post("/templates/{template_id}/editor", status_code=status.HTTP_201_CREATED)
async def create_project_template_editor(
    template_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an isolated, hidden project for editing one manageable template."""

    template = await _require_template(db, current_user, template_id, manage=True)
    return await _create_project_from_template(
        ProjectFromTemplateCreate(template_id=template.id),
        current_user,
        db,
        editor_template_id=template.id,
    )


@router.get("")
async def list_projects(
    scope: str = Query("mine", pattern="^(mine|shared|running|archived|all)$"),
    q: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Project).where(
        accessible_projects_clause(current_user),
        func.coalesce(Project.settings["template_editor"]["template_id"].as_string(), "") == "",
    )
    if scope == "mine":
        stmt = stmt.where(Project.owner_user_id == current_user.id)
    elif scope == "shared":
        stmt = stmt.where(Project.owner_user_id != current_user.id)
    elif scope == "running":
        stmt = stmt.where(Project.status == "running")
    elif scope == "archived":
        stmt = stmt.where(Project.status == "archived")
    if status_filter:
        stmt = stmt.where(Project.status == status_filter)
    elif scope != "archived":
        stmt = stmt.where(Project.status != "archived")
    if q:
        stmt = stmt.where(or_(Project.name.ilike(f"%{q}%"), Project.goal.ilike(f"%{q}%")))
    projects = (await db.execute(stmt.order_by(Project.updated_at.desc()))).scalars().all()
    return [await project_summary(db, project, actor_user_id=current_user.id) for project in projects]


@router.post("", status_code=status.HTTP_201_CREATED)
async def post_project(
    data: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await create_project(db, current_user, data)
    try:
        summary = await project_summary(db, project, actor_user_id=current_user.id)
        await db.commit()
        return summary
    except Exception:
        await db.rollback()
        try:
            await remove_project_repository(project)
        except Exception:
            logger.exception("Failed to compensate project storage for project {}", project.id)
        raise


@router.post("/{project_id}/templates", status_code=status.HTTP_201_CREATED)
async def create_template_from_project(
    project_id: uuid.UUID,
    data: ProjectTemplateFromProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Publish a sanitized project template, including project-owned Agents."""

    project = await require_owner(db, current_user, project_id)
    # Serialize publication for one source project. The immutable package and
    # its definition live in the same database row, so request rollback keeps
    # both invisible and a retry can safely return the completed publication.
    await db.refresh(project, with_for_update=True)
    try:
        definition, snapshot = await _build_project_template_definition(
            db,
            project,
            included_skill_binding_ids=data.included_skill_binding_ids,
        )
    except (ProjectAgentTemplateAssetError, ProjectTemplateSnapshotError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    template_agents = definition["agents"]
    publication_key = hashlib.sha256(
        "\x1f".join(
            (
                str(project.tenant_id),
                str(project.id),
                snapshot.head,
                str(data.name or project.name),
                str(data.description if data.description is not None else project.description or ""),
                data.category,
                data.version,
                "published" if data.is_published else "draft",
                ",".join(sorted(str(value) for value in data.included_skill_binding_ids)),
            )
        ).encode("utf-8")
    ).hexdigest()
    existing_templates = (
        (
            await db.execute(
                select(ProjectTemplate).where(
                    ProjectTemplate.tenant_id == project.tenant_id,
                    ProjectTemplate.created_by_user_id == current_user.id,
                    ProjectTemplate.name == (data.name or project.name),
                    ProjectTemplate.category == data.category,
                    ProjectTemplate.version == data.version,
                    ProjectTemplate.is_published == data.is_published,
                )
            )
        )
        .scalars()
        .all()
    )
    existing = next(
        (
            item
            for item in existing_templates
            if isinstance(item.definition, dict) and item.definition.get("_publication_key") == publication_key
        ),
        None,
    )
    if existing is not None:
        return await _template_payload(db, existing, current_user)
    definition["_publication_key"] = publication_key
    template = ProjectTemplate(
        tenant_id=project.tenant_id,
        created_by_user_id=current_user.id,
        name=data.name or project.name,
        description=data.description if data.description is not None else project.description,
        category=data.category,
        version=data.version,
        is_published=data.is_published,
        definition=definition,
    )
    db.add(template)
    await db.flush()
    add_event(
        db,
        project,
        "project.template.published",
        "Published final project assets and digital employee configuration as a template",
        actor_user_id=current_user.id,
        metadata={
            "template_id": str(template.id),
            "source_head": snapshot.head,
            "file_count": len(snapshot.project_files["files"]),
            "digital_employee_count": len(template_agents),
            "skill_count": len(definition["skill_assets"]),
            "excluded_file_count": snapshot.project_files["excluded_file_count"],
        },
    )
    await db.flush()
    await db.refresh(template)
    return await _template_payload(db, template, current_user)


@router.put("/templates/{template_id}/from-project/{project_id}")
async def update_template_from_project(
    template_id: uuid.UUID,
    project_id: uuid.UUID,
    data: ProjectTemplateFromProjectUpdate | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replace a manageable template definition with one project snapshot."""

    template = await _require_template(db, current_user, template_id, manage=True, lock=True)
    project = await require_owner(db, current_user, project_id)
    await db.refresh(project, with_for_update=True)
    if (
        not is_platform_admin_user(current_user)
        and template.tenant_id is not None
        and template.tenant_id != project.tenant_id
    ):
        raise HTTPException(status_code=404, detail="Project template not found")
    update = data or ProjectTemplateFromProjectUpdate()
    try:
        definition, snapshot = await _build_project_template_definition(
            db,
            project,
            included_skill_binding_ids=update.included_skill_binding_ids,
        )
    except (ProjectAgentTemplateAssetError, ProjectTemplateSnapshotError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    next_name = update.name if update.name is not None else template.name
    next_description = update.description if update.description is not None else template.description
    next_category = update.category if update.category is not None else template.category
    next_version = update.version if update.version is not None else template.version
    next_published = update.is_published if update.is_published is not None else template.is_published
    definition["_publication_key"] = hashlib.sha256(
        "\x1f".join(
            (
                str(template.id),
                str(project.id),
                snapshot.head,
                next_name,
                next_description or "",
                next_category,
                next_version,
                "published" if next_published else "draft",
                ",".join(sorted(str(value) for value in update.included_skill_binding_ids)),
            )
        ).encode("utf-8")
    ).hexdigest()
    template.name = next_name
    template.description = next_description
    template.category = next_category
    template.version = next_version
    template.is_published = next_published
    template.definition = definition
    add_event(
        db,
        project,
        "project.template.updated",
        "Updated a project template from the current project snapshot",
        actor_user_id=current_user.id,
        metadata={
            "template_id": str(template.id),
            "source_head": snapshot.head,
            "file_count": len(snapshot.project_files["files"]),
            "digital_employee_count": len(definition["agents"]),
            "skill_count": len(definition["skill_assets"]),
            "excluded_file_count": snapshot.project_files["excluded_file_count"],
        },
    )
    await db.flush()
    await db.refresh(template)
    return await _template_payload(db, template, current_user)


@router.get("/{project_id}/template-manifest")
async def get_project_template_manifest(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Preview the exact safe assets that project-template publication will include."""

    project = await require_owner(db, current_user, project_id)
    try:
        definition, _snapshot = await _build_project_template_definition(db, project)
    except (ProjectAgentTemplateAssetError, ProjectTemplateSnapshotError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = public_template_definition(definition)
    skill_rows = await project_skill_manifest(db, project)
    skill_bindings = {
        binding.id: binding
        for binding in (
            await db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                    ProjectCapabilityBinding.capability_type == "skill",
                )
            )
        ).scalars()
    }
    result["skills"] = []
    for item in skill_rows:
        binding = skill_bindings[uuid.UUID(item["binding_id"])]
        impact = await project_skill_deletion_impact(db, project, binding)
        result["skills"].append(
            {
                "binding_id": item["binding_id"],
                "member_id": item["member_id"],
                "member_agent_id": item["member_agent_id"],
                "member_name": item["member_name"],
                "member_role": item["member_role"],
                "name": item["name"],
                "version": item["version"],
                "selected": False,
                "selection_state": "unselected",
                "is_enabled": item["is_enabled"],
                "file_count": item["file_count"],
                "size_bytes": item["size_bytes"],
                "affected_members": impact["affected_members"],
                "affected_member_count": impact["affected_member_count"],
            }
        )
    member_rows = list(
        (
            await db.execute(
                select(ProjectMemberSnapshot, Agent)
                .join(Agent, Agent.id == ProjectMemberSnapshot.agent_id)
                .where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    Agent.tenant_id == project.tenant_id,
                    Agent.is_deleted.is_(False),
                )
                .order_by(
                    ProjectMemberSnapshot.is_leader.desc(),
                    ProjectMemberSnapshot.created_at,
                    ProjectMemberSnapshot.id,
                )
            )
        ).all()
    )
    members = [member for member, _agent in member_rows]
    capability_bindings = list(
        (
            await db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                    ProjectCapabilityBinding.capability_type.in_(["tool", "mcp"]),
                )
            )
        ).scalars()
    )
    binding_ids: dict[tuple[str, uuid.UUID], list[str]] = {}
    for binding in capability_bindings:
        if binding.capability_id is not None:
            binding_ids.setdefault((binding.capability_type, binding.capability_id), []).append(str(binding.id))

    grouped_capabilities: dict[tuple[str, uuid.UUID], dict] = {}
    for item in definition.get("capabilities", []):
        if not isinstance(item, dict) or item.get("capability_type") not in {"tool", "mcp"}:
            continue
        try:
            capability_id = uuid.UUID(str(item.get("capability_id")))
        except ValueError:
            continue
        key = (str(item["capability_type"]), capability_id)
        grouped = grouped_capabilities.setdefault(
            key,
            {
                "name": str(item.get("capability_name") or ""),
                "is_enabled": False,
                "member_indexes": set(),
            },
        )
        item_enabled = bool(item.get("is_enabled", True))
        grouped["is_enabled"] = grouped["is_enabled"] or item_enabled
        if not item_enabled:
            continue
        if item.get("source") == "shared":
            grouped["member_indexes"].update(range(len(members)))
        else:
            index = item.get("digital_employee_index")
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(members):
                grouped["member_indexes"].add(index)

    result["capabilities"] = []
    for (capability_type, capability_id), grouped in sorted(
        grouped_capabilities.items(), key=lambda item: (item[0][0], item[1]["name"], str(item[0][1]))
    ):
        synthetic = ProjectCapabilityBinding(
            tenant_id=project.tenant_id,
            project_id=project.id,
            capability_type=capability_type,
            capability_id=capability_id,
            capability_name=grouped["name"],
            source="shared",
            is_enabled=grouped["is_enabled"],
            scope={},
            config={},
        )
        observed = await serialize_project_capability(db, project, synthetic)
        affected = [members[index] for index in sorted(grouped["member_indexes"])]
        persisted_binding_ids = binding_ids.get((capability_type, capability_id), [])
        result["capabilities"].append(
            {
                "binding_id": persisted_binding_ids[0] if persisted_binding_ids else None,
                "binding_ids": persisted_binding_ids,
                "capability_id": str(capability_id),
                "type": capability_type,
                "key": observed.get("key"),
                "name": grouped["name"],
                "description": observed["description"],
                "selected": grouped["is_enabled"],
                "selection_state": "selected" if grouped["is_enabled"] else "unselected",
                "is_enabled": grouped["is_enabled"],
                "availability": observed["availability"],
                "affected_members": [
                    {
                        "member_id": str(member.id),
                        "agent_id": str(member.agent_id),
                        "name": member.name_snapshot,
                        "role": member.role_snapshot,
                        "is_active": member.is_enabled,
                    }
                    for member in affected
                ],
                "affected_member_count": len(affected),
            }
        )
    result["asset_summary"] = {
        **dict(result.get("asset_summary") or {}),
        "skill_count": len(result["skills"]),
        "skill_file_count": sum(item["file_count"] for item in result["skills"]),
        "skill_size_bytes": sum(item["size_bytes"] for item in result["skills"]),
        "capability_count": len(result["capabilities"]),
    }
    return result


@router.get("/{project_id}")
async def get_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    return await project_summary(db, project, actor_user_id=current_user.id)


@router.get("/{project_id}/directory/departments")
async def get_project_directory_departments(
    project_id: uuid.UUID,
    parent_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return organization departments available to the project owner."""
    project = await require_owner(db, current_user, project_id)
    return await permission_directory_departments(
        db,
        tenant_id=project.tenant_id,
        current_user_id=current_user.id,
        parent_id=parent_id,
        search=search,
        limit=limit,
    )


@router.get("/{project_id}/directory/members")
async def get_project_directory_members(
    project_id: uuid.UUID,
    department_id: uuid.UUID | None = None,
    include_descendants: bool = False,
    search: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return canonical share candidates available to the project owner."""
    project = await require_owner(db, current_user, project_id)
    return await permission_directory_members(
        db,
        tenant_id=project.tenant_id,
        department_id=department_id,
        include_descendants=include_descendants,
        search=search,
        page=page,
        page_size=page_size,
        excluded_user_ids={project.owner_user_id},
    )


@router.get("/{project_id}/dashboard")
async def get_project_dashboard(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    # Repair ProjectRuns written by older dispatchers before exposing the
    # dashboard. This is an idempotent data invariant, not presentation logic.
    if await reconcile_project_runs(db, project.id, tenant_id=project.tenant_id):
        await db.commit()
    summary = await project_summary(db, project, actor_user_id=current_user.id)
    work_items = (
        (
            await db.execute(
                select(ProjectWorkItem)
                .where(
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.tenant_id == project.tenant_id,
                )
                .order_by(ProjectWorkItem.updated_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    runs = (
        (
            await db.execute(
                select(ProjectRun)
                .where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                )
                .order_by(ProjectRun.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    events = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(
                    ProjectEvent.project_id == project.id,
                    ProjectEvent.tenant_id == project.tenant_id,
                )
                .order_by(ProjectEvent.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    capabilities = (
        (
            await db.execute(
                select(ProjectCapabilityBinding).where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    await reconcile_project_repository_operations(project.id, db=db)
    git_state = await repository_state(project, 20)
    files = await list_project_files(project)
    return {
        "project": summary,
        "members": members,
        "capabilities": capabilities,
        "work_items": work_items,
        "runs": await serialize_project_runs(db, project, list(runs)),
        "events": await serialize_project_events(db, project, list(events)),
        "git": git_state,
        "commits": git_state["commits"],
        "files": files,
        "branches": git_state["branches"],
        "settings": project.settings or {},
        "policies": (project.settings or {}).get("policies", {}),
        "runtime": (project.settings or {}).get("runtime", {}),
        "work_item_counts": summary["work_item_counts"],
        "progress": summary["progress"],
    }


@router.patch("/{project_id}")
async def patch_project(
    project_id: uuid.UUID,
    data: ProjectUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    updates = data.model_dump(exclude_unset=True)
    acl_change = bool({"visibility", "shared_with_user_ids", "execution_user_id"} & data.model_fields_set)
    runtime_change = "status" in data.model_fields_set
    project = (
        await require_owner(db, current_user, project_id, lock=True)
        if acl_change or runtime_change
        else await require_project(db, current_user, project_id, edit=True)
    )
    runtime_event_type = None
    target_status = updates.get("status")
    runtime_previous_status = project.status if runtime_change else None
    if runtime_change:
        if project.status not in {"running", "paused", "waiting"} or target_status not in {"running", "paused"}:
            raise HTTPException(
                status_code=409,
                detail="The project runtime switch is unavailable in the current project state",
            )
        if target_status != project.status:
            runtime_event_type = "project.paused" if target_status == "paused" else "project.resumed"
    shared_ids = updates.pop("shared_with_user_ids", None)
    requested_visibility = updates.pop("visibility", None)
    requested_execution_user_id = updates.pop("execution_user_id", None)
    normalized_shared_ids = (
        {user_id for user_id in shared_ids if user_id != project.owner_user_id} if shared_ids is not None else None
    )
    if requested_visibility == "shared" and normalized_shared_ids is not None and not normalized_shared_ids:
        raise HTTPException(status_code=422, detail="A shared project requires at least one shared user")
    if requested_visibility == "private" and normalized_shared_ids:
        raise HTTPException(status_code=422, detail="A private project cannot include shared users")
    current_shared_ids = set(
        (
            await db.execute(
                select(ProjectAccessGrant.user_id).where(
                    ProjectAccessGrant.project_id == project.id,
                    ProjectAccessGrant.tenant_id == project.tenant_id,
                )
            )
        ).scalars()
    )
    effective_shared_ids = normalized_shared_ids if normalized_shared_ids is not None else current_shared_ids
    if normalized_shared_ids is not None:
        effective_visibility = "shared" if effective_shared_ids else "private"
    elif requested_visibility == "private":
        effective_visibility = "private"
        effective_shared_ids = set()
    else:
        effective_visibility = requested_visibility or project.visibility
    if effective_visibility == "shared" and not effective_shared_ids:
        raise HTTPException(status_code=422, detail="A shared project requires at least one shared user")

    execution_change = "execution_user_id" in data.model_fields_set
    if execution_change and not can_manage_project_execution_user(current_user, project):
        raise HTTPException(
            status_code=403,
            detail="Only platform or company administrators can change the project execution user",
        )
    previous_execution_user_id = project_execution_user_id(project)
    automatic_execution_fallback = False
    if effective_visibility == "private":
        automatic_execution_fallback = previous_execution_user_id != project.owner_user_id
        project.execution_user_id = None
    else:
        if execution_change:
            selected_execution_user_id = requested_execution_user_id
            # NULL and the owner UUID are the same canonical owner fallback.
            project.execution_user_id = (
                None if selected_execution_user_id in {None, project.owner_user_id} else selected_execution_user_id
            )
        elif project.visibility != "shared":
            # A newly shared project always starts under its owner unless an
            # administrator atomically chooses another active shared user.
            project.execution_user_id = None
        selected_execution_user_id = project_execution_user_id(project)
        valid_execution_user_ids = {project.owner_user_id, *effective_shared_ids}
        if selected_execution_user_id not in valid_execution_user_ids:
            if execution_change:
                raise HTTPException(
                    status_code=422,
                    detail="Project execution user must be the owner or an active shared project user",
                )
            if project.execution_user_id is not None:
                raise HTTPException(
                    status_code=409,
                    detail="The current project execution user must be changed before removing their access",
                )
            selected_execution_user_id = project.owner_user_id
    for key, value in updates.items():
        setattr(project, key, value)
    if shared_ids is not None:
        await replace_access_grants(db, project, shared_ids, actor_user_id=current_user.id)
    elif requested_visibility == "private":
        await replace_access_grants(db, project, [], actor_user_id=current_user.id)
    elif requested_visibility == "shared" and project.visibility != "shared":
        raise HTTPException(status_code=422, detail="Set shared_with_user_ids when sharing a project")
    if project.execution_user_id is not None:
        await resolve_project_execution_user(db, project, project.execution_user_id)
    current_execution_user_id = project_execution_user_id(project)
    if current_execution_user_id != previous_execution_user_id:
        add_event(
            db,
            project,
            "project.execution_user.changed",
            "Changed the project execution user",
            actor_user_id=current_user.id,
            metadata={
                "previous_execution_user_id": str(previous_execution_user_id),
                "execution_user_id": str(current_execution_user_id),
                "automatic_fallback": automatic_execution_fallback,
            },
        )
    if runtime_event_type is not None:
        add_event(
            db,
            project,
            runtime_event_type,
            "Paused all new project work" if target_status == "paused" else "Resumed project work",
            actor_user_id=current_user.id,
            metadata={"previous_status": runtime_previous_status},
        )
    if set(updates) - {"status"} or acl_change:
        add_event(db, project, "project.updated", "Project settings updated", actor_user_id=current_user.id)
    await db.flush()
    await db.refresh(project)
    return await project_summary(db, project, actor_user_id=current_user.id)


@router.get("/{project_id}/settings")
async def get_project_settings(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    return project.settings or {}


@router.patch("/{project_id}/settings")
async def patch_project_settings(
    project_id: uuid.UUID,
    data: ProjectSettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    patch = data.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=422, detail="At least one project setting is required")
    if "git" in patch:
        raise HTTPException(status_code=422, detail="Git repository configuration cannot be changed through settings")
    project.settings = _merge_settings(project.settings or {}, patch)
    add_event(
        db,
        project,
        "project.settings.updated",
        "Updated project runtime or governance settings",
        actor_user_id=current_user.id,
        metadata={"changed_keys": sorted(patch)},
    )
    await db.flush()
    return project.settings


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id, lock=True)
    if project.status in {"initializing", "running"}:
        raise HTTPException(
            status_code=409,
            detail="Pause the project before deleting it",
        )
    cleanup_target = SimpleNamespace(id=project.id, tenant_id=project.tenant_id)

    # Project-scoped Agents and conversations are owned by the project and are
    # removed by the project's database cascades.  Chat messages predate that
    # ownership model and intentionally have no session/project foreign key, so
    # delete the exact project conversation rows before the cascades reach the
    # project Agents they reference.
    project_agent_ids = list(
        (
            await db.execute(
                select(Agent.id).where(
                    Agent.project_id == project.id,
                    Agent.tenant_id == project.tenant_id,
                    Agent.scope == "project",
                )
            )
        ).scalars()
    )
    session_scope = [ChatSession.project_id == project.id]
    if project_agent_ids:
        session_scope.extend(
            (
                ChatSession.agent_id.in_(project_agent_ids),
                ChatSession.peer_agent_id.in_(project_agent_ids),
            )
        )
    related_session_ids = list(
        (
            await db.execute(select(ChatSession.id).where(or_(*session_scope)))
        ).scalars()
    )
    active_project_run_id = await db.scalar(
        select(ProjectRun.id)
        .where(
            ProjectRun.project_id == project.id,
            ProjectRun.status.not_in({"succeeded", "failed", "cancelled"}),
        )
        .limit(1)
    )
    subagent_scope = [SubagentRun.project_id == project.id]
    if related_session_ids:
        subagent_scope.extend(
            (
                SubagentRun.id.in_(related_session_ids),
                SubagentRun.parent_session_id.in_(related_session_ids),
            )
        )
    active_subagent_run_id = await db.scalar(
        select(SubagentRun.id)
        .where(
            or_(*subagent_scope),
            SubagentRun.status.not_in({"completed", "failed", "cancelled"}),
        )
        .limit(1)
    )
    if active_project_run_id is not None or active_subagent_run_id is not None:
        raise HTTPException(
            status_code=409,
            detail="The project still has active work; wait for it to finish or cancel it before deleting",
        )

    # Child runs must be removed before their parent ChatSessions because the
    # durable parent edge intentionally uses RESTRICT.
    await db.execute(delete(SubagentRun).where(or_(*subagent_scope)))

    project_session_ids = [str(session_id) for session_id in related_session_ids]
    message_scope = []
    if project_session_ids:
        message_scope.append(ChatMessage.conversation_id.in_(project_session_ids))
    if project_agent_ids:
        message_scope.extend(
            (
                ChatMessage.agent_id.in_(project_agent_ids),
                ChatMessage.sender_agent_id.in_(project_agent_ids),
            )
        )
    if message_scope:
        await db.execute(delete(ChatMessage).where(or_(*message_scope)))
    if related_session_ids:
        await db.execute(delete(ChatSession).where(ChatSession.id.in_(related_session_ids)))

    # Project digital employees are deleted by the project FK cascade, but a
    # small set of older Agent-owned tables intentionally has no cascade. Clear
    # those exact project-owned rows first so a used project remains deletable.
    # The table names are fixed application schema identifiers; values remain
    # bound parameters.
    if project_agent_ids:
        cleanup_tables = (
            "agent_activity_logs",
            "audit_logs",
            "approval_requests",
            "channel_configs",
            "dingtalk_channel_provisioning_sessions",
            "published_pages",
            "notifications",
            "agent_permissions",
        )
        for agent_id in project_agent_ids:
            await db.execute(
                text(
                    "DELETE FROM gateway_messages "
                    "WHERE agent_id = :agent_id OR sender_agent_id = :agent_id"
                ),
                {"agent_id": agent_id},
            )
            await db.execute(
                text(
                    "DELETE FROM task_logs WHERE task_id IN "
                    "(SELECT id FROM tasks WHERE agent_id = :agent_id)"
                ),
                {"agent_id": agent_id},
            )
            await db.execute(
                text("DELETE FROM tasks WHERE agent_id = :agent_id"),
                {"agent_id": agent_id},
            )
            for table_name in cleanup_tables:
                await db.execute(
                    text(f"DELETE FROM {table_name} WHERE agent_id = :agent_id"),
                    {"agent_id": agent_id},
                )

    await db.delete(project)
    await db.commit()
    try:
        await remove_project_repository(cleanup_target)
    except Exception:
        logger.exception("Project {} was deleted but managed storage cleanup failed", project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{project_id}/access-grants", response_model=list[ProjectAccessGrantOut])
async def list_access_grants(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    return (
        (
            await db.execute(
                select(ProjectAccessGrant).where(
                    ProjectAccessGrant.project_id == project.id,
                    ProjectAccessGrant.tenant_id == project.tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )


@router.post("/{project_id}/access-grants", response_model=ProjectAccessGrantOut, status_code=201)
async def create_access_grant(
    project_id: uuid.UUID,
    data: ProjectAccessGrantCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    user = (
        await db.execute(
            select(User).where(User.id == data.user_id, User.tenant_id == project.tenant_id, User.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if user is None or user.id == project.owner_user_id:
        raise HTTPException(status_code=422, detail="Shared user must be another active user in the project tenant")
    grant = (
        await db.execute(
            select(ProjectAccessGrant).where(
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.user_id == user.id,
                ProjectAccessGrant.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if grant:
        grant.role = data.role
    else:
        grant = ProjectAccessGrant(
            tenant_id=project.tenant_id,
            project_id=project.id,
            user_id=user.id,
            role=data.role,
            created_by_user_id=current_user.id,
        )
        db.add(grant)
    project.visibility = "shared"
    add_event(db, project, "project.shared", f"Shared project with {user.display_name}", actor_user_id=current_user.id)
    await db.flush()
    return grant


@router.delete("/{project_id}/access-grants/{grant_id}")
async def delete_access_grant(
    project_id: uuid.UUID,
    grant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id, lock=True)
    grant = (
        await db.execute(
            select(ProjectAccessGrant).where(
                ProjectAccessGrant.id == grant_id,
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if grant is None:
        raise HTTPException(status_code=404, detail="Access grant not found")
    previous_execution_user_id = project_execution_user_id(project)
    remaining = (
        await db.execute(
            select(func.count(ProjectAccessGrant.id)).where(
                ProjectAccessGrant.project_id == project.id,
                ProjectAccessGrant.id != grant.id,
            )
        )
    ).scalar_one()
    if grant.user_id == previous_execution_user_id and remaining:
        raise HTTPException(
            status_code=409,
            detail="The current project execution user must be changed before removing their access",
        )
    await db.execute(
        delete(ProjectAccessGrant).where(
            ProjectAccessGrant.id == grant_id,
            ProjectAccessGrant.project_id == project.id,
            ProjectAccessGrant.tenant_id == project.tenant_id,
        )
    )
    if remaining == 0:
        project.visibility = "private"
        project.execution_user_id = None
        if previous_execution_user_id != project.owner_user_id:
            add_event(
                db,
                project,
                "project.execution_user.changed",
                "Changed the project execution user",
                actor_user_id=current_user.id,
                metadata={
                    "previous_execution_user_id": str(previous_execution_user_id),
                    "execution_user_id": str(project.owner_user_id),
                    "automatic_fallback": True,
                },
            )
    add_event(db, project, "project.unshared", "Removed a project access grant", actor_user_id=current_user.id)
    return {"ok": True}


@router.get("/{project_id}/members", response_model=list[ProjectMemberOut])
async def list_project_members(
    project_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    project = await require_project(db, current_user, project_id)
    return (
        (
            await db.execute(
                select(ProjectMemberSnapshot)
                .where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                )
                .order_by(ProjectMemberSnapshot.is_leader.desc(), ProjectMemberSnapshot.created_at)
            )
        )
        .scalars()
        .all()
    )


@router.get("/{project_id}/agents", response_model=list[ProjectAgentOut])
async def list_project_agents(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the project-owned Agents visible to every project participant."""

    project = await require_project(db, current_user, project_id)
    records = await list_project_agent_records(db, project)
    return [await serialize_project_agent(project, agent, member) for agent, member in records]


@router.post("/{project_id}/agents", response_model=ProjectAgentOut, status_code=201)
async def create_project_owned_agent(
    project_id: uuid.UUID,
    data: ProjectAgentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create or copy an Agent as a project-owned asset."""

    project = await require_owner(db, current_user, project_id)
    agent, member = await create_project_agent(db, project, current_user, data)
    await db.commit()
    await db.refresh(agent)
    await db.refresh(member)
    return await serialize_project_agent(project, agent, member)


@router.get("/{project_id}/agents/{agent_id}", response_model=ProjectAgentOut)
async def get_project_owned_agent(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    agent, member = await get_project_agent_record(db, project, agent_id)
    return await serialize_project_agent(project, agent, member)


@router.patch("/{project_id}/agents/{agent_id}", response_model=ProjectAgentOut)
async def patch_project_owned_agent(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    data: ProjectAgentUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    agent, member = await get_project_agent_record(db, project, agent_id)
    await update_project_agent(db, project, current_user, agent, member, data)
    await db.commit()
    await db.refresh(agent)
    await db.refresh(member)
    return await serialize_project_agent(project, agent, member)


@router.post("/{project_id}/agents/{agent_id}/deactivate", response_model=ProjectAgentOut)
async def deactivate_project_owned_agent(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    data: ProjectAgentLifecycleRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.services.subagent_runtime import finalize_cancelled_project_member_turns

    project = await require_owner(db, current_user, project_id)
    agent, member = await get_project_agent_record(db, project, agent_id)
    child_ids = await deactivate_project_agent(
        db,
        project,
        current_user,
        agent,
        member,
        reason=data.reason if data else None,
    )
    await db.commit()
    await finalize_cancelled_project_member_turns(project.id, child_ids)
    await db.refresh(agent)
    await db.refresh(member)
    return await serialize_project_agent(project, agent, member)


@router.post("/{project_id}/agents/{agent_id}/restore", response_model=ProjectAgentOut)
async def restore_project_owned_agent(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    data: ProjectAgentLifecycleRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    agent, member = await get_project_agent_record(db, project, agent_id)
    await restore_project_agent(
        db,
        project,
        current_user,
        agent,
        member,
        reason=data.reason if data else None,
    )
    await db.commit()
    await db.refresh(agent)
    await db.refresh(member)
    return await serialize_project_agent(project, agent, member)


@router.post("/{project_id}/agents/{agent_id}/promote", response_model=ProjectAgentPromotionOut, status_code=201)
async def promote_project_owned_agent(
    project_id: uuid.UUID,
    agent_id: uuid.UUID,
    data: ProjectAgentPromoteRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    agent, _member = await get_project_agent_record(db, project, agent_id)
    promoted = await promote_project_agent(
        db,
        project,
        current_user,
        agent,
        name=data.name if data else None,
    )
    await db.commit()
    await db.refresh(promoted)
    return {
        "id": promoted.id,
        "name": promoted.name,
        "role_description": promoted.role_description or "",
        "source_project_id": project.id,
        "source_project_agent_id": agent.id,
    }


@router.post("/{project_id}/members", response_model=ProjectMemberOut, status_code=201)
async def create_project_member(
    project_id: uuid.UUID,
    data: ProjectMemberCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    member = await add_member(db, project, data, actor_user_id=current_user.id)
    await reconcile_project_repository_operations(project.id, db=db)
    return member


async def _load_project_member(
    db: AsyncSession,
    project: Project,
    member_id: uuid.UUID,
) -> ProjectMemberSnapshot:
    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.id == member_id,
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=404, detail="Project member not found")
    return member


@router.post("/{project_id}/members/{member_id}/remove", response_model=ProjectMemberOut)
async def remove_project_member(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    data: ProjectMemberLifecycleRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-remove a member while retaining every historical project record."""

    from app.services.subagent_runtime import finalize_cancelled_project_member_turns

    project = await require_project(db, current_user, project_id, edit=True)
    member = await _load_project_member(db, project, member_id)
    await _guard_project_agent_member_mutation(db, current_user, project, member)
    child_ids = await deactivate_project_member(
        db,
        project,
        member,
        actor_user_id=current_user.id,
        reason=data.reason if data else None,
    )
    await db.commit()
    await finalize_cancelled_project_member_turns(project.id, child_ids)
    await db.refresh(member)
    return member


@router.post("/{project_id}/members/{member_id}/restore", response_model=ProjectMemberOut)
async def restore_removed_project_member(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    data: ProjectMemberLifecycleRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Explicitly restore the same project-local member snapshot."""

    project = await require_project(db, current_user, project_id, edit=True)
    member = await _load_project_member(db, project, member_id)
    await _guard_project_agent_member_mutation(db, current_user, project, member)
    await restore_project_member(
        db,
        project,
        member,
        actor_user_id=current_user.id,
        reason=data.reason if data else None,
    )
    await db.commit()
    await db.refresh(member)
    return member


@router.patch("/{project_id}/members/{member_id}", response_model=ProjectMemberOut)
async def patch_project_member(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    data: ProjectMemberUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    member = await _load_project_member(db, project, member_id)
    updates = data.model_dump(exclude_unset=True)
    requested_enabled = updates.pop("is_enabled", None)
    requested_leader = updates.pop("is_leader", None)
    if requested_enabled is False and requested_leader is True:
        raise HTTPException(status_code=422, detail="A departed member cannot become project owner")
    if "config_snapshot" in updates:
        from app.services.project_member_runtime import merge_project_member_runtime_config

        member.config_snapshot = await merge_project_member_runtime_config(
            db,
            project,
            member,
            updates.pop("config_snapshot"),
        )
    if requested_enabled is False:
        from app.services.subagent_runtime import finalize_cancelled_project_member_turns

        child_ids = await deactivate_project_member(
            db,
            project,
            member,
            actor_user_id=current_user.id,
            reason="member_patch_disable",
        )
        await db.commit()
        await finalize_cancelled_project_member_turns(project.id, child_ids)
    elif requested_enabled is True:
        await restore_project_member(
            db,
            project,
            member,
            actor_user_id=current_user.id,
            reason="member_patch_restore",
        )
    if requested_leader is False and member.is_leader:
        raise HTTPException(
            status_code=422,
            detail="Assign another enabled project owner instead of clearing responsibility",
        )
    if requested_leader is True:
        if not member.is_enabled:
            raise HTTPException(status_code=422, detail="Project owner must be an active project member")
        await db.execute(
            ProjectMemberSnapshot.__table__.update()
            .where(ProjectMemberSnapshot.project_id == project.id)
            .values(is_leader=False)
        )
        member.is_leader = True
    add_event(
        db,
        project,
        "member.snapshot.updated",
        f"Updated snapshot for {member.name_snapshot}",
        actor_user_id=current_user.id,
        actor_agent_id=member.agent_id,
    )
    await db.flush()
    await db.refresh(member)
    return member


async def _project_member_tools_payload(
    db: AsyncSession,
    project: Project,
    member: ProjectMemberSnapshot,
) -> list[dict]:
    from app.api.tools import (
        _agent_visible_tool_clause,
        _globally_visible_tool_clause,
        _load_agent_tool_assignments,
        _tool_availability,
        _tool_record_visible_to_agent,
    )
    from app.services.agent_tools import _agent_has_feishu
    from app.services.tool_enablement import resolved_agent_tool_enabled, tool_is_required

    agent = await db.get(Agent, member.agent_id)
    if agent is None or agent.tenant_id != project.tenant_id:
        raise HTTPException(status_code=404, detail="Project member Agent was not found")
    assignments = await _load_agent_tool_assignments(db, agent.id)
    tools = (
        await db.execute(
            select(Tool)
            .where(
                _globally_visible_tool_clause(),
                _agent_visible_tool_clause(agent.tenant_id, assignments),
            )
            .order_by(Tool.category, Tool.name)
        )
    ).scalars().all()
    has_feishu = await _agent_has_feishu(agent.id)
    config = dict(member.config_snapshot or {})
    enabled_overrides = {str(name) for name in config.get("enabled_platform_tools", [])}
    disabled_overrides = {str(name) for name in config.get("disabled_platform_tools", [])}
    is_project_agent = agent.scope == "project" and agent.project_id == project.id
    result: list[dict] = []
    for tool in tools:
        if tool.category == "feishu" and not has_feishu:
            continue
        if (tool.config or {}).get("okr_agent_only") and not agent.is_system:
            continue
        assignment = assignments.get(str(tool.id))
        if not _tool_record_visible_to_agent(tool, agent.tenant_id, assignments):
            continue
        base_enabled = resolved_agent_tool_enabled(tool.name, assignment)
        enabled = (
            base_enabled
            if is_project_agent
            else tool_is_required(tool.name)
            or tool.name in enabled_overrides
            or (base_enabled and tool.name not in disabled_overrides)
        )
        result.append(
            {
                "id": str(tool.id),
                "name": tool.name,
                "display_name": tool.display_name,
                "description": tool.description,
                "type": tool.type,
                "category": tool.category,
                "icon": tool.icon,
                "enabled": enabled,
                "is_default": tool.is_default,
                "mcp_server_name": tool.mcp_server_name,
                "mcp_server_url": tool.mcp_server_url,
                "mcp_server_id": str(tool.mcp_server_id) if tool.mcp_server_id else None,
                "source": tool.source,
                **_tool_availability(tool.name),
            }
        )
    return result


@router.get("/{project_id}/members/{member_id}/tools")
async def get_project_member_tools(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    member = await _load_project_member(db, project, member_id)
    return await _project_member_tools_payload(db, project, member)


@router.put("/{project_id}/members/{member_id}/tools")
async def put_project_member_tools(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    updates: list[ProjectMemberToolUpdate],
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.api.tools import (
        _agent_visible_tool_clause,
        _load_agent_tool_assignments,
        _tool_record_visible_to_agent,
    )
    from app.services.project_member_runtime import (
        merge_project_member_runtime_config,
        sync_project_agent_mcp_bindings,
        sync_project_agent_tool_bindings,
    )
    from app.services.tool_enablement import tool_is_required

    project = await require_owner(db, current_user, project_id)
    member = await _load_project_member(db, project, member_id)
    if not member.is_enabled:
        raise HTTPException(status_code=409, detail="Departed project members cannot change tools")
    agent = await db.get(Agent, member.agent_id)
    if agent is None or agent.tenant_id != project.tenant_id:
        raise HTTPException(status_code=404, detail="Project member Agent was not found")
    assignments = await _load_agent_tool_assignments(db, agent.id)
    requested_ids = {update.tool_id for update in updates}
    tools = (
        await db.execute(
            select(Tool).where(
                Tool.id.in_(requested_ids),
                _agent_visible_tool_clause(agent.tenant_id, assignments),
            )
        )
    ).scalars().all()
    tool_by_id = {tool.id: tool for tool in tools}
    if set(tool_by_id) != requested_ids or any(
        not _tool_record_visible_to_agent(tool, agent.tenant_id, assignments)
        for tool in tools
    ):
        raise HTTPException(status_code=404, detail="Tool was not found for this project member")
    for update in updates:
        tool = tool_by_id[update.tool_id]
        if not update.enabled and tool_is_required(tool.name):
            raise HTTPException(status_code=409, detail="Required tools cannot be disabled")

    if agent.scope == "project" and agent.project_id == project.id:
        affected_mcp_server_ids: set[uuid.UUID] = set()
        affected_tool_ids: set[uuid.UUID] = set()
        for update in updates:
            tool = tool_by_id[update.tool_id]
            assignment = assignments.get(str(update.tool_id))
            if assignment is None:
                assignment = AgentTool(
                    agent_id=agent.id,
                    tool_id=update.tool_id,
                    enabled=update.enabled,
                    source="user_installed",
                )
                db.add(assignment)
            else:
                assignment.enabled = update.enabled
            if tool.type == "mcp" and tool.mcp_server_id is not None:
                affected_mcp_server_ids.add(tool.mcp_server_id)
            elif tool.type != "mcp":
                affected_tool_ids.add(tool.id)
        await db.flush()
        await sync_project_agent_tool_bindings(
            db,
            project,
            project_agent_id=agent.id,
            tool_ids=affected_tool_ids,
        )
        await sync_project_agent_mcp_bindings(
            db,
            project,
            project_agent_id=agent.id,
            server_ids=affected_mcp_server_ids,
        )
    else:
        config = dict(member.config_snapshot or {})
        enabled = {str(name) for name in config.get("enabled_platform_tools", [])}
        disabled = {str(name) for name in config.get("disabled_platform_tools", [])}
        for update in updates:
            name = tool_by_id[update.tool_id].name
            if update.enabled:
                enabled.add(name)
                disabled.discard(name)
            else:
                disabled.add(name)
                enabled.discard(name)
        member.config_snapshot = await merge_project_member_runtime_config(
            db,
            project,
            member,
            {
                **config,
                "enabled_platform_tools": sorted(enabled),
                "disabled_platform_tools": sorted(disabled),
            },
        )
    add_event(
        db,
        project,
        "member.tools.updated",
        f"Updated tools for {member.name_snapshot}",
        actor_user_id=current_user.id,
        actor_agent_id=member.agent_id,
    )
    await db.commit()
    return await _project_member_tools_payload(db, project, member)


@router.put("/{project_id}/leader", response_model=ProjectMemberOut)
async def put_project_leader(
    project_id: uuid.UUID,
    data: LeaderUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == data.agent_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status_code=422, detail="Project owner must be an enabled project member")
    await _guard_project_agent_member_mutation(db, current_user, project, member)
    await db.execute(
        ProjectMemberSnapshot.__table__.update()
        .where(ProjectMemberSnapshot.project_id == project.id)
        .values(is_leader=False)
    )
    member.is_leader = True
    add_event(
        db,
        project,
        "leader.changed",
        f"Assigned {member.name_snapshot} as project leader",
        actor_user_id=current_user.id,
        actor_agent_id=member.agent_id,
    )
    await db.flush()
    await db.refresh(member)
    return member


@router.get("/{project_id}/capabilities", response_model=list[CapabilityOut])
async def list_project_capabilities(
    project_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    project = await require_project(db, current_user, project_id)
    bindings = list(
        (
            await db.execute(
                select(ProjectCapabilityBinding)
                .where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                )
                .order_by(ProjectCapabilityBinding.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [await serialize_project_capability(db, project, binding) for binding in bindings]


@router.post("/{project_id}/capabilities/skill-backfill")
async def project_skill_backfill(
    project_id: uuid.UUID,
    data: ProjectSkillBackfillRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Preview, apply, or roll back the safe normalization of legacy Skill bindings."""

    project = await require_owner(db, current_user, project_id)
    if data.action == "dry_run":
        summary, _plans = await plan_project_skill_backfill(db, project)
        return {"action": "dry_run", "operation_id": None, **summary}
    if data.action == "apply":
        result, rollback_entries = await apply_project_skill_backfill(
            db,
            project,
            actor_user_id=current_user.id,
            actor_display_name=current_user.display_name,
        )
        if not rollback_entries:
            return {"action": "apply", "operation_id": None, **result}
        event = add_event(
            db,
            project,
            "capability.skill_backfill.applied",
            f"Normalized {len(rollback_entries)} legacy project Skill bindings",
            actor_user_id=current_user.id,
            metadata={
                "applied_count": len(rollback_entries),
                "rollback_entries": rollback_entries,
            },
        )
        await db.flush()
        return {"action": "apply", "operation_id": str(event.id), **result}

    operation = (
        await db.execute(
            select(ProjectEvent).where(
                ProjectEvent.id == data.operation_id,
                ProjectEvent.project_id == project.id,
                ProjectEvent.tenant_id == project.tenant_id,
                ProjectEvent.event_type == "capability.skill_backfill.applied",
            )
        )
    ).scalar_one_or_none()
    if operation is None:
        raise HTTPException(status_code=404, detail="Project Skill backfill operation was not found")
    rollback_events = list(
        (
            await db.execute(
                select(ProjectEvent).where(
                    ProjectEvent.project_id == project.id,
                    ProjectEvent.tenant_id == project.tenant_id,
                    ProjectEvent.event_type == "capability.skill_backfill.rolled_back",
                )
            )
        ).scalars()
    )
    if any(
        str(event.event_metadata.get("operation_id")) == str(operation.id)
        for event in rollback_events
        if isinstance(event.event_metadata, dict)
    ):
        raise HTTPException(status_code=409, detail="Project Skill backfill was already rolled back")
    result = await rollback_project_skill_backfill(
        db,
        project,
        dict(operation.event_metadata or {}).get("rollback_entries"),
        actor_user_id=current_user.id,
        actor_display_name=current_user.display_name,
    )
    rollback_event = add_event(
        db,
        project,
        "capability.skill_backfill.rolled_back",
        f"Rolled back {result['rolled_back_count']} project Skill bindings",
        actor_user_id=current_user.id,
        metadata={
            "operation_id": str(operation.id),
            "rolled_back_count": result["rolled_back_count"],
            "binding_ids": result["binding_ids"],
        },
    )
    await db.flush()
    return {
        "action": "rollback",
        "operation_id": str(operation.id),
        "rollback_event_id": str(rollback_event.id),
        **result,
    }


@router.post("/{project_id}/capabilities", response_model=CapabilityOut, status_code=201)
async def create_project_capability(
    project_id: uuid.UUID,
    data: ProjectCapabilityCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    if data.capability_type in {"mcp", "skill"}:
        if data.capability_id is None:
            raise HTTPException(status_code=422, detail="A registered capability is required")
        source_agent = None
        if data.source == "inherited" and data.inherited_from_agent_id is not None:
            project_agent = await db.get(Agent, data.inherited_from_agent_id)
            if (
                project_agent is not None
                and project_agent.tenant_id == project.tenant_id
                and project_agent.project_id == project.id
                and project_agent.scope == "project"
                and not project_agent.is_deleted
                and project_agent.source_agent_id is not None
            ):
                source_agent = await db.get(Agent, project_agent.source_agent_id)
                if (
                    source_agent is None
                    or source_agent.tenant_id != project.tenant_id
                    or source_agent.scope != "standard"
                    or source_agent.is_deleted
                ):
                    source_agent = None
        options = await load_project_capability_options(
            db,
            project.tenant_id,
            [source_agent] if source_agent is not None else [],
        )
        allowed = (
            options.allows(source_agent.id, data.capability_type, data.capability_id)
            if data.source == "inherited" and source_agent is not None
            else options.allows_shared(data.capability_type, data.capability_id)
        )
        if not allowed:
            raise HTTPException(status_code=422, detail="Selected project capability is unavailable")
    if data.capability_type == "skill":
        if data.source != "inherited":
            raise HTTPException(status_code=422, detail="Project Skills must belong to a project digital employee")
        binding = await bind_library_skill_to_project_agent(
            db,
            project,
            skill_id=data.capability_id,
            project_agent_id=data.inherited_from_agent_id,
            is_enabled=data.is_enabled,
            scope=data.scope,
            actor_user_id=current_user.id,
            actor_display_name=current_user.display_name,
        )
        add_event(
            db,
            project,
            "capability.bound",
            f"Bound skill capability {binding.capability_name}",
            actor_user_id=current_user.id,
            actor_agent_id=binding.inherited_from_agent_id,
            metadata={"binding_id": str(binding.id), "source": binding.source},
        )
    else:
        binding = await add_capability(db, project, data, actor_user_id=current_user.id)
        from app.services.project_member_runtime import sync_project_capability_assignment

        await sync_project_capability_assignment(db, project, binding)
    await db.refresh(binding)
    return await serialize_project_capability(db, project, binding)


@router.get("/{project_id}/capabilities/{binding_id}/delete-impact")
async def get_project_skill_delete_impact(
    project_id: uuid.UUID,
    binding_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    binding = await _project_capability_binding(db, project, binding_id)
    return await project_skill_deletion_impact(db, project, binding)


@router.delete("/{project_id}/capabilities/{binding_id}")
async def delete_project_skill_capability(
    project_id: uuid.UUID,
    binding_id: uuid.UUID,
    confirm: bool = Query(default=False),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    binding = await _project_capability_binding(db, project, binding_id)
    impact = await project_skill_deletion_impact(db, project, binding)
    if not confirm:
        raise HTTPException(
            status_code=409,
            detail={"code": "project_skill_delete_confirmation_required", **impact},
        )
    deleted = await delete_project_skill_asset(
        db,
        project,
        binding,
        actor_user_id=current_user.id,
        actor_display_name=current_user.display_name,
    )
    add_event(
        db,
        project,
        "capability.deleted",
        f"Deleted project Skill {deleted['skill_name']}",
        actor_user_id=current_user.id,
        metadata={
            "asset_id": deleted["asset_id"],
            "affected_member_count": deleted["affected_member_count"],
        },
    )
    return {"ok": True, **deleted}


@router.post("/{project_id}/capabilities/{binding_id}/refresh")
async def refresh_project_skill_capability(
    project_id: uuid.UUID,
    binding_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    binding = await _project_capability_binding(db, project, binding_id)
    result = await refresh_project_skill_asset(
        db,
        project,
        binding,
        actor_user_id=current_user.id,
        actor_display_name=current_user.display_name,
    )
    add_event(
        db,
        project,
        "capability.refreshed",
        f"Refreshed project Skill {binding.capability_name}",
        actor_user_id=current_user.id,
        metadata={
            "asset_id": result["asset_id"],
            "affected_member_count": result["affected_member_count"],
            "changed": result["changed"],
        },
    )
    await db.flush()
    await db.refresh(binding)
    return {**result, "capability": await serialize_project_capability(db, project, binding)}


@router.patch("/{project_id}/capabilities/{binding_id}", response_model=CapabilityOut)
async def patch_project_capability(
    project_id: uuid.UUID,
    binding_id: uuid.UUID,
    data: CapabilityUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    binding = await _project_capability_binding(db, project, binding_id)
    updates = data.model_dump(exclude_unset=True)
    if binding.capability_type == "skill":
        if "config" in updates:
            raise HTTPException(status_code=422, detail="Project Skill asset metadata cannot be edited directly")
        if "is_enabled" in updates and updates["is_enabled"] is not None:
            await set_project_skill_enabled(
                db,
                project,
                binding,
                enabled=updates.pop("is_enabled"),
                actor_user_id=current_user.id,
                actor_display_name=current_user.display_name,
            )
        for key, value in updates.items():
            setattr(binding, key, value)
    else:
        for key, value in updates.items():
            setattr(binding, key, value)
        from app.services.project_member_runtime import sync_project_capability_assignment

        await sync_project_capability_assignment(db, project, binding)
    add_event(
        db,
        project,
        "capability.updated",
        f"Updated capability {binding.capability_name}",
        actor_user_id=current_user.id,
        metadata={"binding_id": str(binding.id), "enabled": binding.is_enabled},
    )
    await db.flush()
    await db.refresh(binding)
    return await serialize_project_capability(db, project, binding)


async def _project_capability_binding(
    db: AsyncSession,
    project: Project,
    binding_id: uuid.UUID,
) -> ProjectCapabilityBinding:
    binding = (
        await db.execute(
            select(ProjectCapabilityBinding).where(
                ProjectCapabilityBinding.id == binding_id,
                ProjectCapabilityBinding.project_id == project.id,
                ProjectCapabilityBinding.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        raise HTTPException(status_code=404, detail="Capability binding not found")
    return binding


async def _require_member_agent(db: AsyncSession, project: Project, agent_id: uuid.UUID | None) -> None:
    if agent_id is None:
        return
    found = (
        await db.execute(
            select(ProjectMemberSnapshot.id).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == agent_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(
            status_code=422,
            detail="数字员工必须是已启用的项目成员",
        )


async def _validate_work_item_links(
    db: AsyncSession,
    project: Project,
    parent_id: uuid.UUID | None,
    dependency_ids: list[uuid.UUID],
    *,
    item_id: uuid.UUID | None = None,
) -> None:
    linked_ids = set(dependency_ids)
    if parent_id:
        linked_ids.add(parent_id)
    if item_id and item_id in linked_ids:
        raise HTTPException(status_code=422, detail="A work item cannot depend on or parent itself")
    if not linked_ids:
        return
    found_ids = set(
        (
            await db.execute(
                select(ProjectWorkItem.id).where(
                    ProjectWorkItem.id.in_(linked_ids),
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.tenant_id == project.tenant_id,
                )
            )
        ).scalars()
    )
    if found_ids != linked_ids:
        raise HTTPException(status_code=422, detail="Every parent and dependency must belong to this project")


@router.get("/{project_id}/work-items", response_model=list[WorkItemOut])
async def list_work_items(
    project_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    project = await require_project(db, current_user, project_id)
    return (
        (
            await db.execute(
                select(ProjectWorkItem)
                .where(ProjectWorkItem.project_id == project.id, ProjectWorkItem.tenant_id == project.tenant_id)
                .order_by(ProjectWorkItem.created_at)
            )
        )
        .scalars()
        .all()
    )


@router.get("/{project_id}/work-items/{work_item_id}", response_model=WorkItemDetailOut)
async def get_work_item_detail(
    project_id: uuid.UUID,
    work_item_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return one trace object; clients do not reconstruct links heuristically."""
    project = await require_project(db, current_user, project_id)
    item = (
        await db.execute(
            select(ProjectWorkItem).where(
                ProjectWorkItem.id == work_item_id,
                ProjectWorkItem.project_id == project.id,
                ProjectWorkItem.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    project_runs = (
        (
            await db.execute(
                select(ProjectRun)
                .where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                )
                .order_by(ProjectRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    all_run_payloads = await serialize_project_runs(db, project, list(project_runs))
    project_events = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(
                    ProjectEvent.project_id == project.id,
                    ProjectEvent.tenant_id == project.tenant_id,
                )
                .order_by(ProjectEvent.created_at.desc(), ProjectEvent.id.desc())
            )
        )
        .scalars()
        .all()
    )
    item_events = [event for event in project_events if event.work_item_id == item.id]
    explicit_run_ids = {
        str(value)
        for event in item_events
        for value in (
            event.run_id,
            dict(event.event_metadata or {}).get("project_run_id"),
            *(dict(event.event_metadata or {}).get("related_run_ids") or []),
        )
        if value
    }
    run_payloads = [
        run for run in all_run_payloads if run.get("work_item_id") == item.id or str(run["id"]) in explicit_run_ids
    ]
    run_ids = {str(run["id"]) for run in run_payloads}
    events = []
    for event in project_events:
        metadata = dict(event.event_metadata or {})
        event_run_ids = {
            str(value)
            for value in (
                event.run_id,
                metadata.get("project_run_id"),
                *(metadata.get("related_run_ids") or []),
            )
            if value
        }
        if event.work_item_id == item.id or bool(run_ids & event_run_ids):
            events.append(event)
    run_by_id = {str(run["id"]): run for run in run_payloads}
    trace_session_ids = {
        str(run.get("subagent_session_id") or run.get("session_id"))
        for run in run_payloads
        if run.get("subagent_session_id") or run.get("session_id")
    }
    trace_run_ids = {str(run["id"]) for run in run_payloads}
    trace_anchor_rows = (
        (
            await db.execute(
                select(ChatMessage).where(
                    ChatMessage.conversation_id.in_(trace_session_ids),
                    ChatMessage.role == "user",
                    ChatMessage.message_meta["project_run_id"].as_string().in_(trace_run_ids),
                )
            )
        )
        .scalars()
        .all()
        if trace_session_ids and trace_run_ids
        else []
    )
    trace_anchor_by_run_id = {
        str(dict(row.message_meta or {}).get("project_run_id")): str(row.id)
        for row in trace_anchor_rows
        if dict(row.message_meta or {}).get("project_run_id")
    }
    sessions = []
    for run in run_payloads:
        if not (run.get("session_id") or run.get("subagent_session_id")):
            continue
        run_input = dict(run.get("input") or {})
        run_dispatch = dict(run_input.get("dispatch") or {})
        anchor_message_id = trace_anchor_by_run_id.get(str(run["id"])) or (
            str(run_dispatch.get("turn_anchor_id") or run_input.get("turn_anchor_id") or "").strip() or None
        )
        sessions.append(
            {
                "run_id": str(run["id"]),
                "project_run_id": str(run["id"]),
                "work_item_id": str(item.id),
                "agent_id": str(run["agent_id"]) if run.get("agent_id") else None,
                "agent_name": run.get("agent_name"),
                "status": run["status"],
                "source_channel": "agent" if run["trigger_type"] == "a2a" else "subagent",
                "session_intent": "a2a" if run["trigger_type"] == "a2a" else "execution",
                "session_id": str(run["session_id"]) if run.get("session_id") else None,
                "subagent_session_id": (str(run["subagent_session_id"]) if run.get("subagent_session_id") else None),
                "anchor_message_id": anchor_message_id,
            }
        )
    known_session_ids = {row["session_id"] for row in sessions if row.get("session_id")}
    session_actor_ids = {event.actor_agent_id for event in item_events if event.actor_agent_id is not None}
    session_members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id.in_(session_actor_ids),
                )
            )
        )
        .scalars()
        .all()
        if session_actor_ids
        else []
    )
    session_member_names = {str(member.agent_id): member.name_snapshot for member in session_members}
    for event in item_events:
        metadata = dict(event.event_metadata or {})
        event_session_id = str(metadata.get("session_id") or "").strip()
        if not event_session_id or event_session_id in known_session_ids:
            continue
        sessions.append(
            {
                "run_id": str(event.run_id) if event.run_id else metadata.get("project_run_id"),
                "project_run_id": str(event.run_id) if event.run_id else metadata.get("project_run_id"),
                "work_item_id": str(item.id),
                "agent_id": str(event.actor_agent_id) if event.actor_agent_id else None,
                "agent_name": session_member_names.get(str(event.actor_agent_id)),
                "status": None,
                "source_channel": "subagent",
                "session_intent": "evidence",
                "session_id": event_session_id,
                "subagent_session_id": metadata.get("subagent_session_id") or event_session_id,
                "anchor_message_id": metadata.get("anchor_message_id")
                or metadata.get("turn_anchor_id")
                or metadata.get("subagent_turn_anchor_id"),
            }
        )
        known_session_ids.add(event_session_id)
    commits_by_hash: dict[str, dict] = {}
    files_by_path: dict[str, dict] = {}
    evidence: list[dict] = []
    for event in events:
        metadata = dict(event.event_metadata or {})
        linked_run = run_by_id.get(str(event.run_id)) if event.run_id else None
        trace = {
            "event_id": str(event.id),
            "run_id": str(event.run_id) if event.run_id else None,
            "work_item_id": str(event.work_item_id or item.id),
            "session_id": metadata.get("session_id")
            or (str(linked_run["session_id"]) if linked_run and linked_run.get("session_id") else None),
            "subagent_session_id": metadata.get("subagent_session_id")
            or (
                str(linked_run["subagent_session_id"]) if linked_run and linked_run.get("subagent_session_id") else None
            ),
        }
        commit = str(metadata.get("commit") or metadata.get("commit_hash") or "").strip()
        if commit:
            commits_by_hash.setdefault(
                commit,
                {
                    **trace,
                    "commit": commit,
                    "short_commit": commit[:12],
                    "message": metadata.get("message") or event.summary,
                    "event_type": event.event_type,
                    "created_at": event.created_at,
                    # Populated from Git below. Event metadata describes the
                    # requested operation and is not authoritative evidence of
                    # what the commit actually changed (an empty milestone can
                    # legitimately carry a non-empty requested path list).
                    "paths": [],
                },
            )
        for value in metadata.get("evidence") or []:
            evidence.append({**trace, "kind": "evidence", "value": str(value), "created_at": event.created_at})
        if metadata.get("progress_note"):
            evidence.append(
                {
                    **trace,
                    "kind": "progress_note",
                    "value": str(metadata["progress_note"]),
                    "created_at": event.created_at,
                }
            )
    # Only commits reached through the explicit work-item/run relation above
    # are inspected. The file list is derived from the repository diff, never
    # from event ``path(s)`` hints, so unrelated repository files and requested
    # paths on an empty milestone cannot leak into this detail contract.
    for commit_hash, commit_record in commits_by_hash.items():
        try:
            diff = await project_commit_diff(project, commit_hash, max_patch_bytes=1)
        except (HTTPException, RuntimeError) as exc:
            logger.warning(
                "Unable to resolve work-item Git diff project={} work_item={} commit={}: {}",
                project.id,
                item.id,
                commit_hash,
                exc,
            )
            commit_record["diff_available"] = False
            continue
        changed_files = list(diff.get("files") or [])
        commit_record["paths"] = [str(file["path"]) for file in changed_files if file.get("path")]
        commit_record["diff_available"] = True
        commit_record["files_truncated"] = bool(diff.get("files_truncated"))
        for changed_file in changed_files:
            path = str(changed_file.get("path") or "").strip()
            if not path:
                continue
            # Events and commits are newest-first. Keep the newest explicit
            # commit for a path while the separate commit list preserves the
            # full history for the work item.
            files_by_path.setdefault(
                path,
                {
                    "event_id": commit_record.get("event_id"),
                    "run_id": commit_record.get("run_id"),
                    "work_item_id": commit_record.get("work_item_id"),
                    "session_id": commit_record.get("session_id"),
                    "subagent_session_id": commit_record.get("subagent_session_id"),
                    "path": path,
                    "commit": commit_hash,
                    "status": changed_file.get("status"),
                    "additions": changed_file.get("additions"),
                    "deletions": changed_file.get("deletions"),
                    "binary": bool(changed_file.get("binary")),
                    "created_at": commit_record.get("created_at"),
                },
            )
    for run in run_payloads:
        output = dict(run.get("output") or {})
        value = output.get("result") or run.get("error")
        if value:
            evidence.append(
                {
                    "kind": "run_result" if output.get("result") else "run_error",
                    "value": str(value),
                    "run_id": str(run["id"]),
                    "work_item_id": str(item.id),
                    "session_id": str(run["session_id"]) if run.get("session_id") else None,
                    "subagent_session_id": (
                        str(run["subagent_session_id"]) if run.get("subagent_session_id") else None
                    ),
                    "created_at": run.get("finished_at") or run["updated_at"],
                }
            )
    return {
        "work_item": item,
        "runs": run_payloads,
        "sessions": sessions,
        "events": await serialize_project_events(db, project, list(events)),
        "commits": list(commits_by_hash.values()),
        "files": list(files_by_path.values()),
        "evidence": evidence,
    }


@router.post("/{project_id}/work-items", response_model=WorkItemOut, status_code=201)
async def create_work_item(
    project_id: uuid.UUID,
    data: WorkItemCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    await _require_member_agent(db, project, data.assignee_agent_id)
    await _validate_work_item_links(db, project, data.parent_id, data.dependency_ids)
    values = data.model_dump()
    values["dependency_ids"] = [str(value) for value in data.dependency_ids]
    item = ProjectWorkItem(
        tenant_id=project.tenant_id,
        project_id=project.id,
        created_by_user_id=current_user.id,
        **values,
    )
    db.add(item)
    await db.flush()
    add_event(
        db,
        project,
        "work_item.created",
        f"Created work item {item.title}",
        actor_user_id=current_user.id,
        work_item_id=item.id,
        metadata={
            "status": item.status,
            "priority": item.priority,
            "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
        },
    )
    await db.flush()
    await db.refresh(item)
    return item


@router.patch("/{project_id}/work-items/{work_item_id}", response_model=WorkItemOut)
async def patch_work_item(
    project_id: uuid.UUID,
    work_item_id: uuid.UUID,
    data: WorkItemUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    item = (
        await db.execute(
            select(ProjectWorkItem).where(
                ProjectWorkItem.id == work_item_id,
                ProjectWorkItem.project_id == project.id,
                ProjectWorkItem.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Work item not found")
    before = {
        "title": item.title,
        "status": item.status,
        "priority": item.priority,
        "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
    }
    updates = data.model_dump(exclude_unset=True)
    if data.dependency_ids is not None:
        updates["dependency_ids"] = [str(value) for value in data.dependency_ids]
    await _require_member_agent(db, project, updates.get("assignee_agent_id"))
    if data.dependency_ids is not None:
        await _validate_work_item_links(db, project, item.parent_id, data.dependency_ids, item_id=item.id)
    for key, value in updates.items():
        setattr(item, key, value)
    after = {
        "title": item.title,
        "status": item.status,
        "priority": item.priority,
        "assignee_agent_id": str(item.assignee_agent_id) if item.assignee_agent_id else None,
    }
    changed_fields = sorted(key for key in after if before[key] != after[key])
    if data.dependency_ids is not None:
        changed_fields.append("dependency_ids")
    add_event(
        db,
        project,
        "work_item.updated",
        f"Updated work item {item.title}",
        actor_user_id=current_user.id,
        work_item_id=item.id,
        metadata={"before": before, "after": after, "changed_fields": changed_fields},
    )
    await db.flush()
    await db.refresh(item)
    return item


@router.get("/{project_id}/runs", response_model=list[ProjectRunOut])
async def list_project_runs(
    project_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    project = await require_project(db, current_user, project_id)
    if await reconcile_project_runs(db, project.id, tenant_id=project.tenant_id):
        await db.commit()
    runs = (
        (
            await db.execute(
                select(ProjectRun)
                .where(ProjectRun.project_id == project.id, ProjectRun.tenant_id == project.tenant_id)
                .order_by(ProjectRun.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return await serialize_project_runs(db, project, list(runs))


@router.post("/{project_id}/runs", response_model=ProjectRunOut, status_code=201)
async def create_project_run(
    project_id: uuid.UUID,
    data: ProjectRunCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create one durable, single-Agent execution and dispatch it immediately.

    ``POST /runs`` is an execution command, not a passive history insert. The
    durable ProjectRun + group anchor are committed as one outbox transaction;
    the daemon can retry dispatch after a process exit.
    """
    from app.services.subagent_runtime import dispatch_project_run

    project = await require_project(db, current_user, project_id, edit=True)
    ensure_project_running(project)
    if data.trigger_type == "a2a":
        raise HTTPException(
            status_code=422,
            detail="请通过项目协作功能向数字员工发送消息。",
        )
    work_item = None
    if data.work_item_id:
        work_item = (
            await db.execute(
                select(ProjectWorkItem).where(
                    ProjectWorkItem.id == data.work_item_id,
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.tenant_id == project.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if work_item is None:
            raise HTTPException(status_code=422, detail="所选任务不属于当前项目。")

    requested_agent_id = data.agent_id or (work_item.assignee_agent_id if work_item else None)
    member_conditions = [
        ProjectMemberSnapshot.project_id == project.id,
        ProjectMemberSnapshot.tenant_id == project.tenant_id,
        ProjectMemberSnapshot.is_enabled.is_(True),
    ]
    if requested_agent_id is not None:
        member_conditions.append(ProjectMemberSnapshot.agent_id == requested_agent_id)
    else:
        member_conditions.append(ProjectMemberSnapshot.is_leader.is_(True))
    member = (
        await db.execute(
            select(ProjectMemberSnapshot)
            .where(*member_conditions)
            .order_by(ProjectMemberSnapshot.is_leader.desc(), ProjectMemberSnapshot.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if member is None:
        if requested_agent_id is not None:
            raise HTTPException(
                status_code=422,
                detail="数字员工必须是已启用的项目成员",
            )
        raise HTTPException(status_code=422, detail="项目需要一名可用的负责人。")

    supplied_input = dict(data.input or {})
    task = str(
        supplied_input.get("task") or supplied_input.get("objective") or supplied_input.get("message") or ""
    ).strip()
    if work_item is not None:
        criteria = "\n".join(f"- {item}" for item in (work_item.acceptance_criteria or [])) or "- None recorded"
        work_context = (
            f"Execute project work item {work_item.id}: {work_item.title}\n\n"
            f"Description:\n{work_item.description or '(none)'}\n\n"
            f"Acceptance criteria:\n{criteria}"
        )
        task = f"{work_context}\n\nAdditional instruction:\n{task}" if task else work_context
    if not task:
        raise HTTPException(
            status_code=422,
            detail="请选择任务或填写执行内容。",
        )
    run_title = str(supplied_input.get("title") or supplied_input.get("objective") or "").strip()
    if work_item is not None:
        run_title = work_item.title
    if not run_title:
        run_title = task.splitlines()[0].strip()
    run_title = run_title[:120]

    group_session = await ensure_project_group_session(db, project)
    anchor = ChatMessage(
        id=uuid.uuid4(),
        agent_id=group_session.agent_id,
        user_id=current_user.id,
        sender_user_id=current_user.id,
        role="user",
        content=task,
        conversation_id=str(group_session.id),
        message_meta={
            "kind": "project_run_request",
            "project_id": str(project.id),
            "visible_to_group": True,
            "mentions": [str(member.agent_id)],
            "awakened_agent_ids": [],
            "wake_policy": "single_explicit_or_default_leader",
            "initiator_user_id": str(current_user.id),
            "target_agent_id": str(member.agent_id),
            "attachments": [],
        },
    )
    run = ProjectRun(
        tenant_id=project.tenant_id,
        project_id=project.id,
        work_item_id=data.work_item_id,
        agent_id=member.agent_id,
        initiated_by_user_id=current_user.id,
        execution_user_id=project_execution_user_id(project),
        status="queued",
        trigger_type=data.trigger_type,
        input={
            **supplied_input,
            "title": run_title,
            "group_session_id": str(group_session.id),
            "dispatch": {
                "group_session_id": str(group_session.id),
                "project_member_id": str(member.id),
                "turn_anchor_id": str(anchor.id),
                "task": task,
            },
        },
        output={"group_session_id": str(group_session.id)},
    )
    db.add_all([anchor, run])
    group_session.last_message_at = func.now()
    await db.flush()
    from app.services.project_group_turn_lifecycle import reconcile_project_group_turn

    await reconcile_project_group_turn(
        db,
        project_id=project.id,
        session=group_session,
    )
    await freeze_run_members(db, project, run)
    add_event(
        db,
        project,
        "run.queued",
        "Queued project run and froze run snapshots",
        actor_user_id=current_user.id,
        actor_agent_id=run.agent_id,
        work_item_id=run.work_item_id,
        run_id=run.id,
        metadata={
            "group_session_id": str(group_session.id),
            "target_agent_id": str(member.agent_id),
            "dispatch_policy": "single_agent",
        },
    )
    # This explicit commit creates the durable outbox boundary before child
    # creation. A daemon retry owns recovery if the process exits afterward.
    await db.commit()
    try:
        await dispatch_project_run(run.id)
    except Exception as exc:  # noqa: BLE001 - queued outbox remains retryable
        logger.warning("Project run dispatch deferred run=%s error=%s", run.id, exc)
    await db.refresh(run)
    return (await serialize_project_runs(db, project, [run]))[0]


@router.patch("/{project_id}/runs/{run_id}", response_model=ProjectRunOut)
async def patch_project_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    data: ProjectRunUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    run = (
        await db.execute(
            select(ProjectRun).where(
                ProjectRun.id == run_id, ProjectRun.project_id == project.id, ProjectRun.tenant_id == project.tenant_id
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Project run not found")
    updates = data.model_dump(exclude_unset=True)
    if "status" in updates:
        apply_run_status(run, updates.pop("status"))
    for key, value in updates.items():
        setattr(run, key, value)
    add_event(
        db,
        project,
        f"run.{run.status}",
        "Execution status updated",
        actor_user_id=current_user.id,
        actor_agent_id=run.agent_id,
        work_item_id=run.work_item_id,
        run_id=run.id,
    )
    await db.flush()
    terminal_group_run_id = (
        run.id if run.status in {"succeeded", "failed", "cancelled"} else None
    )
    if terminal_group_run_id is not None:
        # The websocket projection may only describe committed cohort state.
        await db.commit()
        from app.services.project_group_turn_lifecycle import (
            reconcile_and_publish_project_run_group_turn,
        )

        await reconcile_and_publish_project_run_group_turn(terminal_group_run_id)
    await db.refresh(run)
    return (await serialize_project_runs(db, project, [run]))[0]


@router.get("/{project_id}/runs/{run_id}/member-snapshots", response_model=list[ProjectRunMemberSnapshotOut])
async def list_run_snapshots(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    snapshots = (
        (
            await db.execute(
                select(ProjectRunMemberSnapshot)
                .where(
                    ProjectRunMemberSnapshot.run_id == run_id,
                    ProjectRunMemberSnapshot.project_id == project.id,
                    ProjectRunMemberSnapshot.tenant_id == project.tenant_id,
                )
                .order_by(ProjectRunMemberSnapshot.created_at)
            )
        )
        .scalars()
        .all()
    )
    return await serialize_project_run_member_snapshots(db, project, list(snapshots))


@router.get("/{project_id}/events", response_model=list[ProjectEventOut])
async def list_project_events(
    project_id: uuid.UUID,
    event_type: str | None = None,
    actor_agent_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    stmt = select(ProjectEvent).where(
        ProjectEvent.project_id == project.id, ProjectEvent.tenant_id == project.tenant_id
    )
    if event_type:
        stmt = stmt.where(ProjectEvent.event_type == event_type)
    if actor_agent_id:
        stmt = stmt.where(ProjectEvent.actor_agent_id == actor_agent_id)
    events = (await db.execute(stmt.order_by(ProjectEvent.created_at.desc()).limit(limit))).scalars().all()
    return await serialize_project_events(db, project, list(events))


@router.post("/{project_id}/events", response_model=ProjectEventOut, status_code=201)
async def create_project_event(
    project_id: uuid.UUID,
    data: ProjectEventCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    await _require_member_agent(db, project, data.actor_agent_id)
    event = add_event(
        db,
        project,
        data.event_type,
        data.summary,
        actor_user_id=current_user.id,
        actor_agent_id=data.actor_agent_id,
        work_item_id=data.work_item_id,
        run_id=data.run_id,
        metadata=data.metadata,
    )
    await db.flush()
    return (await serialize_project_events(db, project, [event]))[0]


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


@router.get("/{project_id}/leader-session")
async def get_project_leader_session(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    session = await ensure_project_leader_session(db, project)
    discussion_count = int(
        (
            await db.execute(select(func.count(ChatMessage.id)).where(ChatMessage.conversation_id == str(session.id)))
        ).scalar_one()
    )
    await db.commit()
    await db.refresh(session)
    return _leader_session_payload(session, discussion_count)


@router.post("/{project_id}/kickoff/confirm", status_code=202)
async def confirm_project_kickoff(
    project_id: uuid.UUID,
    data: ProjectKickoffConfirm,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Freeze project-owner planning evidence and start one durable child.

    Confirmation is the only implicit wake in this flow and targets only the
    enabled project owner. Group messages remain append-only and structured mentions
    keep their zero-broadcast default.
    """
    from app.services.subagent_runtime import dispatch_project_run

    authorized_project = await require_project(db, current_user, project_id, edit=True)
    project = (
        await db.execute(select(Project).where(Project.id == authorized_project.id).with_for_update())
    ).scalar_one()
    if project.status == "initializing":
        pending_run = (
            await db.execute(
                select(ProjectRun)
                .where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.trigger_type == "leader_kickoff",
                    ProjectRun.status.in_(["queued", "running"]),
                )
                .order_by(ProjectRun.created_at.desc(), ProjectRun.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if pending_run is not None and dict((pending_run.input or {}).get("dispatch") or {}):
            await db.commit()
            dispatch_result = await dispatch_project_run(pending_run.id)
            refreshed = await db.get(ProjectRun, pending_run.id)
            event_id = (
                await db.execute(
                    select(ProjectEvent.id)
                    .where(
                        ProjectEvent.run_id == pending_run.id,
                        ProjectEvent.event_type == "project.kickoff.confirmed",
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            payload = {
                **dict(pending_run.input or {}),
                **(dict(refreshed.output or {}) if refreshed else {}),
            }
            return {
                "status": "running" if dispatch_result.get("subagent_run_id") else "initializing",
                "project_id": str(project.id),
                "run_id": str(pending_run.id),
                "event_id": str(event_id) if event_id else None,
                "leader_session_id": payload.get("leader_session_id"),
                "group_session_id": payload.get("group_session_id"),
                "leader_agent_id": payload.get("leader_agent_id"),
                "discussion_source": payload.get("discussion_source", "leader_session"),
                "discussion_session_id": payload.get("discussion_session_id", payload.get("leader_session_id")),
                "awakened_agent_ids": [payload.get("leader_agent_id")]
                if dispatch_result.get("subagent_run_id")
                else [],
                "subagent_run_id": dispatch_result.get("subagent_run_id"),
                "subagent_session_id": dispatch_result.get("subagent_session_id"),
                "git_start_commit": payload.get("git_start_commit"),
                "transcript_path": payload.get("transcript_path"),
                "transcript_commit": payload.get("transcript_commit"),
                "recovered": True,
            }
    if project.status != "planning":
        raise HTTPException(status_code=409, detail="Only a planning project can be confirmed")

    leader = await ensure_enabled_project_leader(db, project)
    leader_session = await ensure_project_leader_session(db, project)
    group_session = await ensure_project_group_session(db, project)
    group_planning = _uses_project_group_planning(project)
    if group_planning:
        if await _has_active_project_planning_run(db, project):
            raise HTTPException(
                status_code=409,
                detail="Project planning is still being processed; confirm kickoff after the current turn completes",
            )
        discussion = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.conversation_id == str(group_session.id),
                        or_(
                            and_(ChatMessage.role == "user", ChatMessage.sender_user_id.is_not(None)),
                            and_(
                                ChatMessage.role == "assistant",
                                ChatMessage.sender_agent_id == leader.agent_id,
                            ),
                        ),
                    )
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        roles = {message.role for message in discussion}
        discussion_source = "project_group"
        discussion_session_id = group_session.id
    else:
        discussion = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(ChatMessage.conversation_id == str(leader_session.id))
                    .order_by(ChatMessage.created_at, ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
        if _has_unfinished_direct_planning_turn(discussion):
            raise HTTPException(
                status_code=409,
                detail="Project planning is still being processed; confirm kickoff after the current turn completes",
            )
        roles = {message.role for message in discussion}
        if "user" not in roles or "assistant" not in roles:
            # Older projects may have moved planning into the canonical group
            # before the transport marker existed. Keep that migration path.
            discussion = (
                (
                    await db.execute(
                        select(ChatMessage)
                        .where(
                            ChatMessage.conversation_id == str(group_session.id),
                            or_(
                                and_(ChatMessage.role == "user", ChatMessage.sender_user_id.is_not(None)),
                                and_(
                                    ChatMessage.role == "assistant",
                                    ChatMessage.sender_agent_id == leader.agent_id,
                                ),
                            ),
                        )
                        .order_by(ChatMessage.created_at, ChatMessage.id)
                    )
                )
                .scalars()
                .all()
            )
            roles = {message.role for message in discussion}
            discussion_source = "project_group"
            discussion_session_id = group_session.id
        else:
            discussion_source = "leader_session"
            discussion_session_id = leader_session.id
    if _has_unfinished_direct_planning_turn(discussion):
        raise HTTPException(
            status_code=409,
            detail="Project planning has an unanswered Human message; wait for the project owner response",
        )
    if "user" not in roles or "assistant" not in roles:
        raise HTTPException(
            status_code=422,
            detail=(
                "Kickoff requires at least one Human message and one project owner response in planning or project chat"
            ),
        )

    confirmation = (
        data.confirmation or "确认当前方案并开始执行。"
    ).strip()
    confirmed_at = datetime.now(UTC)
    transcript = _kickoff_transcript(project, discussion, confirmation, confirmed_at)
    transcript_sha256 = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    await reconcile_project_repository_operations(project.id, db=db)
    git_start = await repository_state(project, limit=1)
    transcript_commit = await write_project_file(
        project,
        "docs/kickoff-transcript.md",
        transcript,
        author_name=current_user.display_name,
        author_email=project_user_git_email(current_user.id),
    )
    now = confirmed_at
    kickoff_message = ChatMessage(
        id=uuid.uuid4(),
        agent_id=group_session.agent_id,
        user_id=current_user.id,
        sender_user_id=current_user.id,
        role="user",
        content=f"项目方案已确认，负责人 @{leader.name_snapshot} 可以开始推进。",
        conversation_id=str(group_session.id),
        external_event_key=f"project-kickoff:{project.id}:{transcript_sha256}",
        message_meta={
            "kind": "project_kickoff_confirmation",
            "project_id": str(project.id),
            "visible_to_group": True,
            "mentions": [],
            "awakened_agent_ids": [],
            "wake_policy": "kickoff_leader_only",
            "initiator_user_id": str(current_user.id),
            "leader_agent_id": str(leader.agent_id),
            "transcript_path": "docs/kickoff-transcript.md",
            "transcript_commit": transcript_commit["commit"],
        },
        created_at=now,
    )
    task = build_project_kickoff_task(transcript)
    kickoff_snapshot = {
        "confirmed_at": confirmed_at.isoformat(),
        "confirmed_by_user_id": str(current_user.id),
        "leader_session_id": str(leader_session.id),
        "group_session_id": str(group_session.id),
        "leader_agent_id": str(leader.agent_id),
        "git_start_commit": git_start["head"],
        "transcript_path": "docs/kickoff-transcript.md",
        "transcript_commit": transcript_commit["commit"],
        "transcript_sha256": transcript_sha256,
        "discussion_source": discussion_source,
        "discussion_session_id": str(discussion_session_id),
    }
    run = ProjectRun(
        tenant_id=project.tenant_id,
        project_id=project.id,
        agent_id=leader.agent_id,
        initiated_by_user_id=current_user.id,
        execution_user_id=project_execution_user_id(project),
        status="queued",
        trigger_type="leader_kickoff",
        input={
            "title": f"启动项目：{project.name}"[:120],
            "leader_session_id": str(leader_session.id),
            "group_session_id": str(group_session.id),
            "leader_agent_id": str(leader.agent_id),
            "discussion_source": discussion_source,
            "discussion_session_id": str(discussion_session_id),
            "confirmation": confirmation,
            "conversation_snapshot": {
                "message_count": len(discussion),
                "message_ids": [str(message.id) for message in discussion],
                "last_message_at": discussion[-1].created_at.isoformat() if discussion[-1].created_at else None,
                "source": discussion_source,
                "session_id": str(discussion_session_id),
                "transcript_path": "docs/kickoff-transcript.md",
                "transcript_sha256": transcript_sha256,
            },
            "git_start_commit": git_start["head"],
            "transcript_path": "docs/kickoff-transcript.md",
            "transcript_commit": transcript_commit["commit"],
            "dispatch": {
                "group_session_id": str(group_session.id),
                "project_member_id": str(leader.id),
                "turn_anchor_id": str(kickoff_message.id),
                "task": task,
                "execution_tools_enabled": True,
                "kickoff": kickoff_snapshot,
            },
        },
        output={
            "group_session_id": str(group_session.id),
            "leader_session_id": str(leader_session.id),
            "transcript_path": "docs/kickoff-transcript.md",
            "transcript_commit": transcript_commit["commit"],
        },
    )
    db.add_all([kickoff_message, run])
    group_session.last_message_at = now
    project.status = "initializing"
    project.settings = {
        **dict(project.settings or {}),
        "planning": {
            **dict(dict(project.settings or {}).get("planning") or {}),
            "state": "confirmed",
            "launch_confirmed": True,
            "confirmed_at": confirmed_at.isoformat(),
            "confirmed_by_user_id": str(current_user.id),
        },
    }
    await db.flush()
    from app.services.conversation_turn_lifecycle import (
        transition_conversation_turn,
    )

    await transition_conversation_turn(
        db,
        agent_id=group_session.agent_id,
        conversation_id=str(group_session.id),
        turn_anchor_id=kickoff_message.id,
        status="running",
    )
    await freeze_run_members(db, project, run)
    await db.commit()
    try:
        dispatch_result = await dispatch_project_run(run.id)
    except Exception as exc:
        # The committed ProjectRun is the durable outbox. A daemon retry owns
        # recovery, so this response never rolls the project back to planning.
        logger.exception("Project kickoff dispatch is waiting for retry: project={} run={}", project.id, run.id)
        dispatch_result = {"status": "initializing", "error": "项目正在等待处理。"}
    event_id = (
        await db.execute(
            select(ProjectEvent.id)
            .where(
                ProjectEvent.run_id == run.id,
                ProjectEvent.event_type == "project.kickoff.confirmed",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    child_id = dispatch_result.get("subagent_run_id")
    return {
        "status": "running" if child_id else "initializing",
        "project_id": str(project.id),
        "run_id": str(run.id),
        "event_id": str(event_id) if event_id else None,
        "leader_session_id": str(leader_session.id),
        "group_session_id": str(group_session.id),
        "leader_agent_id": str(leader.agent_id),
        "discussion_source": discussion_source,
        "discussion_session_id": str(discussion_session_id),
        "awakened_agent_ids": [str(leader.agent_id)] if child_id else [],
        "subagent_run_id": str(child_id) if child_id else None,
        "subagent_session_id": str(child_id) if child_id else None,
        "git_start_commit": git_start["head"],
        "transcript_path": "docs/kickoff-transcript.md",
        "transcript_commit": transcript_commit["commit"],
    }


@router.get("/{project_id}/group-session")
async def get_project_group_session(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    session = await ensure_project_group_session(db, project)
    await db.commit()
    await db.refresh(session)
    return _group_session_payload(session, project)


@router.get("/{project_id}/group-sessions/{session_id}/messages")
async def list_project_group_messages(
    project_id: uuid.UUID,
    session_id: uuid.UUID,
    limit: int = Query(500, ge=1, le=500),
    before: str | None = Query(
        None,
        description="Cursor: ISO timestamp, optionally followed by |message UUID",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.project_id == project.id,
                ChatSession.source_channel == "project",
                ChatSession.is_group.is_(True),
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Project group session not found")
    messages_query = (
        select(ChatMessage)
        .where(
            ChatMessage.conversation_id == str(session.id),
            ChatMessage.message_meta["kind"].as_string().is_distinct_from(
                "project_subagent_external_continuation"
            ),
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )
    if before:
        try:
            before_timestamp, separator, before_message_id = before.partition("|")
            before_dt = datetime.fromisoformat(before_timestamp.replace("Z", "+00:00"))
            if separator:
                cursor_id = uuid.UUID(before_message_id)
                messages_query = messages_query.where(
                    or_(
                        ChatMessage.created_at < before_dt,
                        and_(
                            ChatMessage.created_at == before_dt,
                            ChatMessage.id < cursor_id,
                        ),
                    )
                )
            else:
                messages_query = messages_query.where(ChatMessage.created_at < before_dt)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="分页位置无效，请刷新后重试。",
            )
    newest_first = (await db.execute(messages_query.limit(limit + 1))).scalars().all()
    has_more = len(newest_first) > limit
    messages = list(reversed(newest_first[:limit]))
    oldest_message = messages[0] if messages else None
    from app.services.project_group_turn_lifecycle import reconcile_project_group_turn

    turn_projection = await reconcile_project_group_turn(
        db,
        project_id=project.id,
        session=session,
    )
    timeline = await build_project_group_timeline(
        db,
        project_id=project.id,
        group_messages=messages,
    )
    await db.commit()
    return {
        "session": _group_session_payload(session, project),
        "items": timeline,
        "turn": turn_projection.to_client_dict(),
        "has_more": has_more,
        "next_cursor": (
            f"{oldest_message.created_at.isoformat()}|{oldest_message.id}" if oldest_message is not None else None
        ),
    }


@router.post("/{project_id}/group-sessions/{session_id}/messages", status_code=201)
async def create_project_group_message(
    project_id: uuid.UUID,
    session_id: uuid.UUID,
    data: ProjectGroupMessageCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Append a group message, route Human input to the project owner, and wake mentions."""
    from app.services.subagent_runtime import dispatch_project_run

    project = await require_project(db, current_user, project_id, edit=True)
    ensure_project_accepts_group_message(project)
    if data.sender_agent_id is not None:
        raise HTTPException(
            status_code=422,
            detail="当前请求不能以数字员工身份发送消息。",
        )
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.project_id == project.id,
                ChatSession.source_channel == "project",
                ChatSession.is_group.is_(True),
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Project group session not found")

    mention_ids = list(dict.fromkeys(data.mentions))
    if project.status in {"planning", "paused", "waiting", "completed"} and mention_ids:
        raise HTTPException(
            status_code=422,
            detail=(
                "Discussion outside active execution is handled by the project owner"
            ),
        )
    leader = await ensure_enabled_project_leader(db, project)
    default_leader_agent_id = leader.agent_id
    policies = dict((project.settings or {}).get("policies") or {})
    mention_limit = min(8, max(1, int(policies.get("max_group_mentions_per_message", 4))))
    configured_wake_budget = max(0, int(policies.get("max_a2a_wakes", mention_limit)))
    wake_budget = max(1, configured_wake_budget)
    if len(mention_ids) > mention_limit:
        raise HTTPException(
            status_code=422,
            detail=f"Structured mentions exceed this project's per-message limit ({mention_limit})",
        )
    wake_agent_ids = list(dict.fromkeys([default_leader_agent_id, *mention_ids]))
    if len(wake_agent_ids) > wake_budget:
        raise HTTPException(
            status_code=422,
            detail=f"Project owner and mentions exceed this project's per-message wake budget ({wake_budget})",
        )
    required_agent_ids = set(wake_agent_ids)
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id.in_(required_agent_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    member_by_agent = {member.agent_id: member for member in members}
    if set(member_by_agent) != required_agent_ids:
        raise HTTPException(status_code=422, detail="Every sender and mention must be a project member")
    disabled = [agent_id for agent_id in mention_ids if not member_by_agent[agent_id].is_enabled]
    if disabled:
        raise HTTPException(status_code=422, detail="Disabled project members cannot be awakened")

    event_key = f"project-group:{project.id}:{data.client_message_id}" if data.client_message_id else None
    existing = None
    if event_key:
        existing = (
            await db.execute(select(ChatMessage).where(ChatMessage.external_event_key == event_key))
        ).scalar_one_or_none()
    if existing is not None:
        existing_work_item_id = dict(existing.message_meta or {}).get("work_item_id")
        if str(existing_work_item_id or "") != str(data.work_item_id or ""):
            raise HTTPException(status_code=409, detail="client_message_id already refers to another work item")
        pending_runs = (
            (
                await db.execute(
                    select(ProjectRun).where(
                        ProjectRun.project_id == project.id,
                        ProjectRun.input["group_message_id"].as_string() == str(existing.id),
                        ProjectRun.trigger_type.in_(["group_leader_message", "group_mention"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        from app.services.project_group_turn_lifecycle import reconcile_project_group_turn

        turn_projection = await reconcile_project_group_turn(
            db,
            project_id=project.id,
            session=session,
        )
        await db.commit()
        for pending_run in pending_runs:
            if not dict(pending_run.output or {}).get("subagent_run_id"):
                await dispatch_project_run(pending_run.id)
        await db.refresh(existing)
        turn_projection = await reconcile_project_group_turn(
            db,
            project_id=project.id,
            session=session,
        )
        await db.commit()
        meta = dict(existing.message_meta or {})
        return {
            "message": _group_message_payload(existing),
            "awakened_agent_ids": meta.get("awakened_agent_ids", []),
            "default_leader_agent_id": meta.get("default_leader_agent_id"),
            "subagent_runs": meta.get("subagent_runs", []),
            "turn": turn_projection.to_client_dict(),
            "idempotent_replay": True,
        }

    from app.services.project_group_turn_lifecycle import (
        find_project_group_blocking_confirmation,
    )

    blocking_confirmation = await find_project_group_blocking_confirmation(
        db,
        project_id=project.id,
        session_id=session.id,
    )
    if blocking_confirmation is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "project_group_confirmation_pending",
                "message": "请先完成当前待确认操作。",
                "call_id": str(blocking_confirmation.row_id),
                "client_message_id": data.client_message_id,
            },
        )

    message = ChatMessage(
        agent_id=session.agent_id,
        user_id=current_user.id,
        sender_user_id=current_user.id,
        sender_agent_id=None,
        role="user",
        content=data.content.strip(),
        conversation_id=str(session.id),
        external_event_key=event_key,
        message_meta={
            "kind": "project_group_message",
            "project_id": str(project.id),
            "visible_to_group": True,
            "mentions": [str(agent_id) for agent_id in mention_ids],
            "default_leader_agent_id": str(default_leader_agent_id),
            "attachments": data.attachments,
            "work_item_id": str(data.work_item_id) if data.work_item_id else None,
            "awakened_agent_ids": [],
            "subagent_runs": [],
            "wake_policy": "default_leader_plus_structured_mentions",
            "initiator_user_id": str(current_user.id),
        },
    )
    db.add(message)
    session.last_message_at = func.now()
    await db.flush()
    execution_content = (data.llm_content or data.content).strip()
    if not execution_content:
        execution_content = "处理项目群聊中附带的文件，并把结论回复到项目群。附件：" + str(data.attachments)
    from app.services.project_collaboration_prompt import (
        build_project_group_task,
        build_project_planning_task,
        build_project_read_only_conversation_task,
    )

    message_title = execution_content.splitlines()[0].strip()[:96] or "处理项目群聊消息"
    project_runs: list[ProjectRun] = []
    for agent_id in wake_agent_ids:
        member = member_by_agent[agent_id]
        project_run = ProjectRun(
            tenant_id=project.tenant_id,
            project_id=project.id,
            work_item_id=data.work_item_id,
            agent_id=agent_id,
            initiated_by_user_id=current_user.id,
            execution_user_id=project_execution_user_id(project),
            status="queued",
            trigger_type="group_leader_message" if agent_id == default_leader_agent_id else "group_mention",
            input={
                "title": (
                    f"处理群聊：{message_title}"
                    if agent_id == default_leader_agent_id
                    else f"响应提及：{message_title}"
                )[:120],
                "group_session_id": str(session.id),
                "group_message_id": str(message.id),
                "mentioned_agent_id": str(agent_id),
                "wake_reason": "default_leader" if agent_id == default_leader_agent_id else "structured_mention",
                "initiator_user_id": str(current_user.id),
                "work_item_id": str(data.work_item_id) if data.work_item_id else None,
                "dispatch": {
                    "group_session_id": str(session.id),
                    "project_member_id": str(member.id),
                    "turn_anchor_id": str(message.id),
                    "execution_tools_enabled": project.status == "running",
                    "read_only_conversation": project.status in {"paused", "waiting", "completed"},
                    "task": (
                        build_project_planning_task(execution_content)
                        if project.status == "planning"
                        else build_project_read_only_conversation_task(
                            execution_content,
                            status=project.status,
                        )
                        if project.status in {"paused", "waiting", "completed"}
                        else build_project_group_task(
                            execution_content,
                            is_owner=agent_id == default_leader_agent_id,
                        )
                    ),
                },
            },
            output={"group_session_id": str(session.id)},
        )
        db.add(project_run)
        await db.flush()
        await freeze_run_members(db, project, project_run)
        project_runs.append(project_run)
    from app.services.project_group_turn_lifecycle import (
        publish_project_group_turn_event,
        reconcile_project_group_turn,
    )

    admitted_turn = await reconcile_project_group_turn(
        db,
        project_id=project.id,
        session=session,
    )
    # Message and every target run form one durable outbox transaction. The
    # Subagent daemon can recover all rows after a process exit.
    await db.commit()
    await publish_project_group_turn_event(
        session=session,
        projection=admitted_turn,
        payload={
            **_group_message_payload(message),
            "type": "user_message_committed",
            "client_message_id": data.client_message_id,
            "message_id": str(message.id),
        },
        event_kind="turn_user_committed",
    )

    awakened: list[str] = []
    subagent_rows: list[dict] = []
    for project_run in project_runs:
        agent_id = project_run.agent_id
        try:
            result = await dispatch_project_run(project_run.id)
            run_id = result.get("subagent_run_id")
            run_status = result.get("status", "queued")
            if run_id:
                awakened.append(str(agent_id))
            subagent_rows.append(
                {
                    "project_run_id": str(project_run.id),
                    "run_id": str(run_id) if run_id else None,
                    "session_id": str(run_id) if run_id else None,
                    "agent_id": str(agent_id),
                    "status": run_status,
                }
            )
        except Exception as exc:
            # Keep the durable queued run retryable; the daemon owns recovery.
            logger.exception(
                "Project member dispatch is waiting for retry: project={} run={} agent={}",
                project.id,
                project_run.id,
                agent_id,
            )
            subagent_rows.append(
                {
                    "project_run_id": str(project_run.id),
                    "run_id": None,
                    "session_id": None,
                    "agent_id": str(agent_id),
                    "status": "queued",
                    "error": "任务正在等待处理。",
                }
            )

    message = await db.get(ChatMessage, message.id, with_for_update=True)
    if message is None:
        raise HTTPException(status_code=500, detail="Group message disappeared during wake dispatch")
    # Dispatch executes in its own transaction and may defer the owner when
    # the Human addressed specialists explicitly. Refresh the cached row so
    # the API response exposes that durable routing decision immediately.
    await db.refresh(message)
    recovered_meta = dict(message.message_meta or {})
    awakened = list(dict.fromkeys([*recovered_meta.get("awakened_agent_ids", []), *awakened]))
    recovered_rows = list(recovered_meta.get("subagent_runs", []))
    for row in subagent_rows:
        if not any(str(existing_row.get("project_run_id")) == row["project_run_id"] for existing_row in recovered_rows):
            recovered_rows.append(row)
    message.message_meta = {**recovered_meta, "awakened_agent_ids": awakened, "subagent_runs": recovered_rows}
    subagent_rows = recovered_rows
    event = (
        await db.execute(
            select(ProjectEvent)
            .where(
                ProjectEvent.project_id == project.id,
                ProjectEvent.event_type == "group.message.created",
                ProjectEvent.event_metadata["group_message_id"].as_string() == str(message.id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    event_metadata = {
        "group_session_id": str(session.id),
        "group_message_id": str(message.id),
        "work_item_id": str(data.work_item_id) if data.work_item_id else None,
        "initiator_user_id": str(current_user.id),
        "visible_to_group": True,
        "mentioned_agent_ids": [str(agent_id) for agent_id in mention_ids],
        "default_leader_agent_id": str(default_leader_agent_id),
        "awakened_agent_ids": awakened,
        "subagent_runs": subagent_rows,
        "zero_wake_default": False,
    }
    if event is None:
        event = add_event(
            db,
            project,
            "group.message.created",
            "Appended project group message and dispatched its selected project members",
            actor_user_id=current_user.id,
            actor_agent_id=None,
            work_item_id=data.work_item_id,
            metadata=event_metadata,
        )
    else:
        event.event_metadata = event_metadata
    from app.services.project_group_turn_lifecycle import reconcile_project_group_turn

    turn_projection = await reconcile_project_group_turn(
        db,
        project_id=project.id,
        session=session,
    )
    await db.flush()
    await db.commit()
    await db.refresh(message)
    return {
        "message": _group_message_payload(message),
        "event_id": str(event.id),
        "awakened_agent_ids": awakened,
        "default_leader_agent_id": str(default_leader_agent_id),
        "subagent_runs": subagent_rows,
        "turn": turn_projection.to_client_dict(),
    }


@router.post("/{project_id}/a2a", status_code=202)
async def wake_project_agent(
    project_id: uuid.UUID,
    data: A2AWakeRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    ensure_project_running(project)
    if data.from_agent_id == data.to_agent_id:
        raise HTTPException(status_code=422, detail="from_agent_id and to_agent_id must differ")
    await _require_member_agent(db, project, data.from_agent_id)
    await _require_member_agent(db, project, data.to_agent_id)
    work_item = (
        await db.execute(
            select(ProjectWorkItem).where(
                ProjectWorkItem.id == data.work_item_id,
                ProjectWorkItem.project_id == project.id,
                ProjectWorkItem.tenant_id == project.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if work_item is None:
        raise HTTPException(status_code=422, detail="Work item is not in this project")
    dependency_ids = {uuid.UUID(str(value)) for value in (work_item.dependency_ids or [])}
    if dependency_ids:
        dependency_rows = (
            (
                await db.execute(
                    select(ProjectWorkItem).where(
                        ProjectWorkItem.project_id == project.id,
                        ProjectWorkItem.tenant_id == project.tenant_id,
                        ProjectWorkItem.id.in_(dependency_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        unfinished = [item.title for item in dependency_rows if item.status != "done"]
        missing = len(dependency_rows) != len(dependency_ids)
        if unfinished or missing:
            labels = ", ".join(unfinished) or "missing dependency records"
            raise HTTPException(
                status_code=409,
                detail=f"该任务的前置任务尚未完成，暂不能发起成员协作：{labels}",
            )
    run_title = data.title.strip()[:120]
    expected_output = data.expected_output.strip()
    message = data.message.strip()
    if not run_title or not message or not expected_output:
        raise HTTPException(
            status_code=422,
            detail="title, message and expected_output must contain non-whitespace text",
        )
    actionable_message = f"{message}\n\nExpected output: {expected_output}"
    group_session = await ensure_project_group_session(db, project)
    run = ProjectRun(
        tenant_id=project.tenant_id,
        project_id=project.id,
        work_item_id=data.work_item_id,
        agent_id=data.to_agent_id,
        initiated_by_user_id=current_user.id,
        execution_user_id=project_execution_user_id(project),
        status="queued",
        trigger_type="a2a",
        input={
            "title": run_title,
            "from_agent_id": str(data.from_agent_id),
            "to_agent_id": str(data.to_agent_id),
            "message": actionable_message,
            "mode": data.mode,
            "expected_output": expected_output,
            "new_conversation": data.new_conversation,
            "connector": "agent_tools.send_message_to_agent",
            "group_session_id": str(group_session.id),
        },
    )
    db.add(run)
    await db.flush()
    await freeze_run_members(db, project, run)
    event = add_event(
        db,
        project,
        "a2a.queued",
        f"Queued project collaboration action: {run_title}",
        actor_user_id=current_user.id,
        actor_agent_id=data.from_agent_id,
        from_agent_id=data.from_agent_id,
        to_agent_id=data.to_agent_id,
        work_item_id=data.work_item_id,
        run_id=run.id,
        metadata={
            "mode": data.mode,
            "title": run_title,
            "message": message,
            "expected_output": expected_output,
            "delivery": "queued",
            "connector": "agent_tools.send_message_to_agent",
            "group_session_id": str(group_session.id),
        },
    )
    await db.flush()
    await db.commit()
    background_tasks.add_task(deliver_project_a2a, run.id)
    return {
        "status": "queued",
        "run_id": str(run.id),
        "event_id": str(event.id),
        "from_agent_id": str(data.from_agent_id),
        "to_agent_id": str(data.to_agent_id),
        "group_session_id": str(group_session.id),
        "delivery_contract": "The existing A2A sender can consume this queued run without leader relay.",
    }


@router.get("/{project_id}/git/remotes")
async def get_git_remotes(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    return {"items": await list_git_remotes(project)}


@router.put("/{project_id}/git/remotes/{name}")
async def put_project_git_remote(
    project_id: uuid.UUID,
    name: str,
    data: GitRemoteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await put_git_remote(project, name, data.url)
    remotes = await list_git_remotes(project)
    _record_git_repository_settings(project, remotes=remotes)
    event = add_event(
        db,
        project,
        "git.remote.configured",
        f"Configured Git remote: {result['name']}",
        actor_user_id=current_user.id,
        metadata=_git_remote_audit_metadata(result, history_changed=False),
    )
    await db.flush()
    return {**result, "event_id": str(event.id)}


@router.delete("/{project_id}/git/remotes/{name}")
async def delete_project_git_remote(
    project_id: uuid.UUID,
    name: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await delete_git_remote(project, name)
    remotes = await list_git_remotes(project)
    _record_git_repository_settings(project, remotes=remotes)
    event = add_event(
        db,
        project,
        "git.remote.deleted",
        f"Deleted Git remote: {result['name']}",
        actor_user_id=current_user.id,
        metadata=_git_remote_audit_metadata(result, history_changed=False),
    )
    await db.flush()
    return {**result, "event_id": str(event.id)}


@router.post("/{project_id}/git/clone")
async def clone_project_git_repository(
    project_id: uuid.UUID,
    data: GitCloneRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    operation = await begin_project_repository_clone(project, data.url, data.branch)
    result = operation.result
    journal = ProjectRepositoryOperation(
        id=operation.id,
        tenant_id=project.tenant_id,
        project_id=project.id,
        operation_type="clone",
        state="prepared",
        old_head=operation.old_head,
        new_head=operation.new_head,
        backup_name=operation.backup.name,
        staging_name=operation.staging_root.name,
    )
    db.add(journal)
    try:
        # The journal must win its own transaction before the filesystem can
        # move. A process exit after this point is repaired on next Git access.
        await db.commit()
        await apply_project_repository_clone(operation)
        _record_git_repository_settings(
            project,
            source="cloned",
            head=str(result["head"]),
            default_branch=str(result["default_branch"]),
            remotes=list(result["remotes"]),
        )
        event = add_event(
            db,
            project,
            "git.repository.cloned",
            f"Cloned project repository at {str(result['head'])[:12]}",
            actor_user_id=current_user.id,
            metadata={
                **_git_remote_audit_metadata(
                    {"name": "origin", "url": result["url"]},
                    history_changed=True,
                ),
                "operation": "clone",
                "head": result["head"],
                "default_branch": result["default_branch"],
            },
        )
        journal.state = "committed"
        await db.flush()
        # The filesystem backup cannot be finalized by the dependency's
        # post-response commit: a commit failure there would be too late to
        # compensate. Commit the settings and audit event inside this unit.
        await db.commit()
    except BaseException as exc:  # noqa: BLE001 - cancellation must preserve the durable state machine
        persisted: ProjectRepositoryOperation | None = None
        state_known = False
        try:
            await db.rollback()
            persisted = await db.get(ProjectRepositoryOperation, operation.id)
            state_known = True
        except BaseException as state_exc:  # noqa: BLE001 - do not guess an ambiguous commit result
            logger.warning("Could not read project clone journal operation={}: {}", operation.id, state_exc)
        if persisted is not None and persisted.state == "committed":
            # The metadata transaction won even though the client observed an
            # exception (for example a disconnect after server-side COMMIT).
            # Keep the new repository and let this or the next access finish
            # cleanup; rolling it back would contradict durable project state.
            try:
                await finalize_project_repository_clone(operation)
            except BaseException as cleanup_exc:  # noqa: BLE001 - committed journal owns deferred cleanup
                logger.warning("Deferred ambiguous project clone cleanup operation={}: {}", operation.id, cleanup_exc)
        elif state_known:
            try:
                await rollback_project_repository_clone(operation)
            finally:
                try:
                    if persisted is not None:
                        await db.delete(persisted)
                        await db.commit()
                except BaseException:  # noqa: BLE001 - preserve the original operation failure
                    await db.rollback()
        else:
            # A DB outage leaves the commit result genuinely unknown. Release
            # only the lock: the durable journal decides recovery on access.
            await release_project_repository_clone_lock(operation)
        if isinstance(exc, IntegrityError):
            raise HTTPException(status_code=409, detail="A repository operation is already in progress") from exc
        raise
    finalized = False
    try:
        await finalize_project_repository_clone(operation)
        finalized = True
    except Exception as exc:  # noqa: BLE001 - committed journal owns deferred cleanup
        # DB state is authoritative after ``committed``. Keep the journal so a
        # later repository access can retry backup cleanup.
        logger.warning("Deferred committed project clone cleanup operation={}: {}", operation.id, exc)
    if finalized:
        try:
            persisted = await db.get(ProjectRepositoryOperation, operation.id)
            if persisted is not None:
                await db.delete(persisted)
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - committed journal remains recoverable
            await db.rollback()
            logger.warning("Deferred project clone journal deletion operation={}: {}", operation.id, exc)
    return {**result, "event_id": str(event.id)}


@router.post("/{project_id}/git/restore")
async def create_git_restore(
    project_id: uuid.UUID,
    data: GitRestoreRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Restoring an arbitrary tree may replace project-owned Agent identity
    # files, so this repository-wide mutation is owner-only.
    project = await require_owner(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await restore_as_new_commit(
        project,
        data.commit,
        data.message,
        author_name=current_user.display_name,
        author_email=project_user_git_email(current_user.id),
    )
    _record_git_head(project, result["commit"])
    event = add_event(
        db,
        project,
        "git.restore_commit.created",
        "项目版本已恢复",
        actor_user_id=current_user.id,
        metadata={**result, "forbidden_operations": ["reset", "force_push"]},
    )
    await db.flush()
    return {**result, "event_id": str(event.id)}


@router.get("/{project_id}/files")
async def get_project_files(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    return await list_project_files(project)


@router.get("/{project_id}/files/content")
async def get_project_file_content(
    project_id: uuid.UUID,
    path: str = Query(min_length=1, max_length=1024),
    max_chars: int = Query(default=200_000, ge=1, le=1024 * 1024),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return HEAD metadata and a bounded text preview plus signed media URLs."""

    project = await require_project(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await read_project_file_content(project, path, max_chars=max_chars)
    ticket = _create_project_file_ticket(current_user, project, result)
    encoded_path = quote(result["path"], safe="")
    encoded_ticket = quote(ticket, safe="")
    raw_url = f"/api/projects/{project.id}/files/raw?path={encoded_path}&ticket={encoded_ticket}"
    html_preview_url = None
    if result["is_text"] and result["mime_type"] == "text/html":
        preview_ticket = _create_project_snapshot_ticket(
            current_user,
            project,
            purpose="project_html_preview",
            path=result["path"],
            head=result["head"],
        )
        html_preview_url = (
            f"/api/projects/{project.id}/files/preview/"
            f"{quote(preview_ticket, safe='')}/{quote(result['path'], safe='/')}"
        )
    return {
        **result,
        "raw_url": raw_url,
        "download_url": f"{raw_url}&download=true",
        "html_preview_url": html_preview_url,
        "ticket_expires_in": _PROJECT_FILE_TICKET_TTL_SECONDS,
    }


@router.api_route("/{project_id}/files/raw", methods=["GET", "HEAD"])
async def get_project_file_raw(
    project_id: uuid.UUID,
    request: Request,
    path: str = Query(min_length=1, max_length=1024),
    ticket: str = Query(min_length=1),
    download: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Stream one immutable HEAD blob, including RFC single-range requests."""

    _user, project, metadata = await _authorize_project_file_ticket(db, project_id, path, ticket)
    size = int(metadata["size"])
    etag = f'"{metadata["object_id"]}"'
    range_header = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if range_header and if_range and if_range.strip() != etag:
        range_header = None
    try:
        start, end, partial = _parse_project_file_range(range_header, size)
    except (TypeError, ValueError):
        return Response(
            status_code=416,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "Content-Range": f"bytes */{size}",
                "Cross-Origin-Resource-Policy": "same-origin",
                "ETag": etag,
                "X-Content-Type-Options": "nosniff",
            },
        )

    content_length = max(0, end - start + 1)
    disposition = "attachment" if download else "inline"
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(metadata['name'], safe='')}",
        "Content-Length": str(content_length),
        "Cross-Origin-Resource-Policy": "same-origin",
        "ETag": etag,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    status_code = 206 if partial else 200
    if request.method == "HEAD" or content_length == 0:
        return Response(status_code=status_code, media_type=metadata["mime_type"], headers=headers)
    return StreamingResponse(
        iter_project_file_blob(project, metadata["object_id"], start=start, end=end),
        status_code=status_code,
        media_type=metadata["mime_type"],
        headers=headers,
    )


@router.get("/{project_id}/files/archive")
async def get_project_directory_archive_ticket(
    project_id: uuid.UUID,
    path: str = Query(default="", max_length=1024),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a short-lived download URL for one immutable HEAD directory."""

    project = await require_project(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    snapshot = await inspect_project_directory(project, path)
    ticket = _create_project_snapshot_ticket(
        current_user,
        project,
        purpose="project_directory_archive",
        path=snapshot["path"],
        head=snapshot["head"],
    )
    return {
        **snapshot,
        "download_url": (
            f"/api/projects/{project.id}/files/archive/raw?"
            f"ticket={quote(ticket, safe='')}&path={quote(snapshot['path'], safe='')}"
        ),
        "ticket_expires_in": _PROJECT_FILE_TICKET_TTL_SECONDS,
    }


@router.api_route("/{project_id}/files/archive/raw", methods=["GET", "HEAD"])
async def get_project_directory_archive(
    project_id: uuid.UUID,
    request: Request,
    ticket: str = Query(min_length=1),
    path: str = Query(default="", max_length=1024),
    db: AsyncSession = Depends(get_db),
):
    """Stream a prevalidated ZIP from the exact HEAD captured by its ticket."""

    _user, project, payload = await _authorize_project_snapshot_ticket(
        db,
        project_id,
        ticket,
        purpose="project_directory_archive",
    )
    if payload["path"] != path:
        raise HTTPException(status_code=401, detail="Project archive ticket does not match this directory")
    snapshot = await inspect_project_directory(project, path, revision=payload["head"])
    headers = {
        "Cache-Control": "private, no-store",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(snapshot['name'], safe='')}",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Project-Git-Head": snapshot["head"],
    }
    if request.method == "HEAD":
        return Response(status_code=200, media_type="application/zip", headers=headers)
    return StreamingResponse(
        iter_project_directory_archive(project, snapshot["head"], snapshot["path"]),
        media_type="application/zip",
        headers=headers,
    )


@router.api_route("/{project_id}/files/preview/{ticket}/{path:path}", methods=["GET", "HEAD"])
async def get_project_html_preview_resource(
    project_id: uuid.UUID,
    ticket: str,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Serve one sandbox-preview asset from a ticket's immutable Git HEAD."""

    _user, project, payload = await _authorize_project_snapshot_ticket(
        db,
        project_id,
        ticket,
        purpose="project_html_preview",
    )
    metadata = await inspect_project_file_at(project, path, payload["head"])
    preview_prefix = f"{str(request.base_url).rstrip('/')}/api/projects/{project.id}/files/preview/"
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "private, no-store",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(metadata['name'], safe='')}",
        "Content-Length": str(metadata["size"]),
        "Cross-Origin-Resource-Policy": "cross-origin",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Project-Git-Head": metadata["head"],
    }
    if metadata["mime_type"] == "text/html":
        headers["Content-Security-Policy"] = (
            "sandbox allow-scripts; default-src 'none'; "
            f"script-src 'unsafe-inline' {preview_prefix}; "
            f"style-src 'unsafe-inline' {preview_prefix}; "
            f"img-src data: blob: {preview_prefix}; "
            f"media-src data: blob: {preview_prefix}; "
            f"font-src data: {preview_prefix}; "
            "connect-src 'none'; frame-src 'none'; object-src 'none'; "
            "worker-src 'none'; base-uri 'none'; form-action 'none'; navigate-to 'none'"
        )
    if request.method == "HEAD" or metadata["size"] == 0:
        return Response(status_code=200, media_type=metadata["mime_type"], headers=headers)
    return StreamingResponse(
        iter_project_file_blob(project, metadata["object_id"], start=0, end=metadata["size"] - 1),
        media_type=metadata["mime_type"],
        headers=headers,
    )


@router.put("/{project_id}/files")
async def put_project_file(
    project_id: uuid.UUID,
    data: ProjectFileWriteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Atomically write one project file and immediately create its Git commit."""

    project = await require_project(db, current_user, project_id, edit=True)
    await _guard_project_agent_identity_paths(db, current_user, project, data.path)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await write_project_file(
        project,
        data.path,
        data.content,
        author_name=current_user.display_name,
        author_email=project_user_git_email(current_user.id),
    )
    _record_git_head(project, result["commit"])
    event = add_event(
        db,
        project,
        "project.file.committed",
        f"项目文件已保存：{result['path']}",
        actor_user_id=current_user.id,
        metadata={
            "path": result["path"],
            "commit": result["commit"],
            "operation": "write_and_commit",
        },
    )
    await db.flush()
    return {**result, "event_id": str(event.id)}


@router.post("/{project_id}/git/commit")
async def create_git_commit(
    project_id: uuid.UUID,
    data: GitCommitRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await commit_project_changes(
        project,
        data.message,
        data.paths,
        milestone=data.milestone,
        author_name=current_user.display_name,
        author_email=project_user_git_email(current_user.id),
    )
    _record_git_head(project, result["commit"])
    commit_event = add_event(
        db,
        project,
        "git.commit.created",
        "项目版本已创建",
        actor_user_id=current_user.id,
        metadata=result,
    )
    milestone_event = None
    if data.milestone:
        milestone_event = add_event(
            db,
            project,
            "git.milestone.created",
            "交付里程碑已创建",
            actor_user_id=current_user.id,
            metadata={
                **result,
                "milestone_message": data.message,
                "description": data.message,
            },
        )
    await db.flush()
    return {
        **result,
        "event_id": str(commit_event.id),
        "milestone_event_id": str(milestone_event.id) if milestone_event else None,
    }


@router.post("/{project_id}/git/branches")
async def create_git_branch(
    project_id: uuid.UUID,
    data: GitBranchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await create_branch(project, data.name, data.from_commit)
    event = add_event(
        db,
        project,
        "git.branch.created",
        f"Created branch: {data.name}",
        actor_user_id=current_user.id,
        metadata={**result, "forbidden_operations": ["reset", "force_push"]},
    )
    await db.flush()
    return {**result, "event_id": str(event.id)}


@router.get("/{project_id}/milestones", response_model=list[ProjectMilestoneOut])
async def list_project_milestones(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate durable milestone audit rows with verified Git commits."""
    project = await require_project(db, current_user, project_id)
    milestone_events = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(
                    ProjectEvent.project_id == project.id,
                    ProjectEvent.tenant_id == project.tenant_id,
                    ProjectEvent.event_type == "git.milestone.created",
                )
                .order_by(ProjectEvent.created_at.desc(), ProjectEvent.id.desc())
            )
        )
        .scalars()
        .all()
    )
    git_state = await repository_state(project, max(500, len(milestone_events) * 20))
    commit_by_hash = {str(commit["commit"]): commit for commit in git_state.get("commits", [])}
    run_ids = {event.run_id for event in milestone_events if event.run_id is not None}
    runs = (
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                    ProjectRun.id.in_(run_ids),
                )
            )
        )
        .scalars()
        .all()
        if run_ids
        else []
    )
    run_by_id = {str(payload["id"]): payload for payload in await serialize_project_runs(db, project, list(runs))}
    actor_agent_ids = {event.actor_agent_id for event in milestone_events if event.actor_agent_id is not None}
    actor_members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id.in_(actor_agent_ids),
                )
            )
        )
        .scalars()
        .all()
        if actor_agent_ids
        else []
    )
    member_name_by_agent = {str(member.agent_id): member.name_snapshot for member in actor_members}
    records = []
    for event in milestone_events:
        metadata = dict(event.event_metadata or {})
        commit_hash = str(metadata.get("commit") or "").strip()
        commit = commit_by_hash.get(commit_hash)
        # An audit event alone is not a milestone: expose only records whose
        # immutable Git commit still exists in the repository history.
        if commit is None:
            continue
        run = run_by_id.get(str(event.run_id)) if event.run_id else None
        records.append(
            {
                "id": event.id,
                "event_id": event.id,
                "project_id": project.id,
                "commit": commit_hash,
                "short_commit": commit.get("short_commit") or commit_hash[:12],
                "message": metadata.get("milestone_message")
                or metadata.get("description")
                or commit.get("message")
                or metadata.get("message")
                or event.summary,
                "author": commit.get("author"),
                "created_at": event.created_at,
                "commit_created_at": commit.get("created_at"),
                "run_id": event.run_id,
                "work_item_id": event.work_item_id or (run.get("work_item_id") if run else None),
                "session_id": metadata.get("session_id") or (run.get("session_id") if run else None),
                "subagent_session_id": metadata.get("subagent_session_id")
                or (run.get("subagent_session_id") if run else None),
                "agent_id": event.actor_agent_id or (run.get("agent_id") if run else None),
                "agent_name": (run.get("agent_name") if run else None)
                or member_name_by_agent.get(str(event.actor_agent_id)),
                "paths": list(metadata.get("paths") or []),
                "changed": metadata.get("changed"),
                "related_run_ids": list(metadata.get("related_run_ids") or []),
                "related_work_item_ids": list(metadata.get("related_work_item_ids") or []),
            }
        )
    return records


@router.get("/{project_id}/git")
async def get_git_state(
    project_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    state = await repository_state(project, limit)
    commits = list(state.get("commits") or [])
    commit_hashes = [str(commit.get("commit") or "") for commit in commits if commit.get("commit")]
    if not commit_hashes:
        return state

    events = (
        (
            await db.execute(
                select(ProjectEvent)
                .where(
                    ProjectEvent.project_id == project.id,
                    ProjectEvent.tenant_id == project.tenant_id,
                    ProjectEvent.event_metadata["commit"].as_string().in_(commit_hashes),
                )
                .order_by(ProjectEvent.created_at.desc(), ProjectEvent.id.desc())
            )
        )
        .scalars()
        .all()
    )
    run_ids = {event.run_id for event in events if event.run_id is not None}
    work_item_ids = {event.work_item_id for event in events if event.work_item_id is not None}
    runs = (
        (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.tenant_id == project.tenant_id,
                    ProjectRun.id.in_(run_ids),
                )
            )
        )
        .scalars()
        .all()
        if run_ids
        else []
    )
    work_items = (
        (
            await db.execute(
                select(ProjectWorkItem).where(
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.tenant_id == project.tenant_id,
                    ProjectWorkItem.id.in_(work_item_ids),
                )
            )
        )
        .scalars()
        .all()
        if work_item_ids
        else []
    )
    run_agents = {run.id: run.agent_id for run in runs if run.agent_id is not None}
    work_item_agents = {item.id: item.assignee_agent_id for item in work_items if item.assignee_agent_id is not None}
    event_agent_by_commit: dict[str, uuid.UUID] = {}
    for event in events:
        commit_hash = str((event.event_metadata or {}).get("commit") or "")
        if not commit_hash or commit_hash in event_agent_by_commit:
            continue
        agent_id = run_agents.get(event.run_id) or work_item_agents.get(event.work_item_id) or event.actor_agent_id
        if agent_id is not None:
            event_agent_by_commit[commit_hash] = agent_id
    agent_ids = set(event_agent_by_commit.values())
    members = (
        (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id.in_(agent_ids),
                )
            )
        )
        .scalars()
        .all()
        if agent_ids
        else []
    )
    member_names = {member.agent_id: member.name_snapshot for member in members}
    for commit in commits:
        agent_id = event_agent_by_commit.get(str(commit.get("commit") or ""))
        if agent_id is not None and member_names.get(agent_id):
            commit["author"] = member_names[agent_id]
            commit["author_agent_id"] = str(agent_id)
    return {**state, "commits": commits}


@router.get("/{project_id}/git/diff")
async def get_git_diff(
    project_id: uuid.UUID,
    commit: str = Query(..., min_length=7, max_length=64),
    parent: str | None = Query(None, min_length=7, max_length=64),
    path: str | None = Query(None, min_length=1, max_length=4096),
    max_patch_bytes: int = Query(256 * 1024, ge=1, le=1024 * 1024),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    return await project_commit_diff(project, commit, parent, path, max_patch_bytes)
