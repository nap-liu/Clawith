"""REST API for closed-loop AI-native project management."""

import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from loguru import logger
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.models.audit import ChatMessage
from app.models.chat_session import ChatSession
from app.models.mcp_server import MCPServer
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
from app.models.skill import Skill
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
    ProjectCapabilityCreate,
    ProjectCreate,
    ProjectEventCreate,
    ProjectEventOut,
    ProjectFileWriteRequest,
    ProjectFromTemplateCreate,
    ProjectGroupMessageCreate,
    ProjectKickoffConfirm,
    ProjectMemberCreate,
    ProjectMemberOut,
    ProjectMemberUpdate,
    ProjectRunCreate,
    ProjectRunMemberSnapshotOut,
    ProjectRunOut,
    ProjectRunUpdate,
    ProjectSettingsUpdate,
    ProjectTemplateCreate,
    ProjectUpdate,
    WorkItemCreate,
    WorkItemOut,
    WorkItemUpdate,
)
from app.services.project_git_service import (
    apply_project_repository_clone,
    begin_project_repository_clone,
    commit_project_changes,
    create_branch,
    delete_git_remote,
    finalize_project_repository_clone,
    list_git_remotes,
    list_project_files,
    put_git_remote,
    reconcile_project_repository_operations,
    release_project_repository_clone_lock,
    repository_state,
    restore_as_new_commit,
    rollback_project_repository_clone,
    write_project_file,
)
from app.services.project_service import (
    accessible_projects_clause,
    add_capability,
    add_event,
    add_member,
    apply_run_status,
    create_project,
    deliver_project_a2a,
    ensure_project_group_session,
    ensure_project_leader_session,
    freeze_run_members,
    project_summary,
    replace_access_grants,
    require_owner,
    require_project,
)

router = APIRouter(prefix="/projects", tags=["projects"])


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


async def _template_payload(db: AsyncSession, template: ProjectTemplate) -> dict:
    definition = template.definition or {}
    usage_count = (
        await db.execute(select(func.count(Project.id)).where(Project.template_id == template.id))
    ).scalar_one()
    author_name = None
    if template.created_by_user_id:
        author_name = (
            await db.execute(select(User.display_name).where(User.id == template.created_by_user_id))
        ).scalar_one_or_none()
    return {
        "id": str(template.id),
        "tenant_id": str(template.tenant_id) if template.tenant_id else None,
        "created_by_user_id": str(template.created_by_user_id) if template.created_by_user_id else None,
        "name": template.name,
        "description": template.description,
        "category": template.category,
        "version": template.version,
        "is_published": template.is_published,
        "definition": definition,
        "author_name": author_name or "Clawith",
        "usage_count": usage_count,
        "featured": bool(definition.get("featured", False)),
        "objective_hint": definition.get("goal") or definition.get("objective", ""),
        "success_criteria": definition.get("success_criteria", []),
        "roles": definition.get("roles", definition.get("members", [])),
        "skills": definition.get("skills", []),
        "mcp_servers": definition.get("mcp_servers", []),
        "created_at": template.created_at.isoformat() if template.created_at else None,
        "updated_at": template.updated_at.isoformat() if template.updated_at else None,
    }


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
            "description": "由项目 Leader 驱动需求、研发、测试与交付闭环。",
            "definition": {
                "featured": True,
                "goal": "按验收标准交付一个可发布的产品增量",
                "success_criteria": ["关键路径通过", "评审与测试留痕", "产出进入 Git 历史"],
                "roles": ["项目 Leader", "产品设计", "前端开发", "后端开发", "质量工程"],
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
                "roles": ["研究 Leader", "情报分析", "事实核查", "报告编辑"],
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
                "roles": ["内容 Leader", "作者", "审校", "发布运营"],
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
    tenant_id = _tenant_id(current_user)
    await _ensure_builtin_templates(db)
    stmt = select(ProjectTemplate).where(
        or_(
            ProjectTemplate.tenant_id.is_(None),
            ProjectTemplate.tenant_id == tenant_id,
        ),
        or_(ProjectTemplate.is_published.is_(True), ProjectTemplate.created_by_user_id == current_user.id),
    )
    if category:
        stmt = stmt.where(ProjectTemplate.category == category)
    if q:
        stmt = stmt.where(ProjectTemplate.name.ilike(f"%{q}%"))
    templates = (await db.execute(stmt.order_by(ProjectTemplate.created_at.desc()))).scalars().all()
    return [await _template_payload(db, template) for template in templates]


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_project_template(
    data: ProjectTemplateCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    template = ProjectTemplate(
        tenant_id=_tenant_id(current_user),
        created_by_user_id=current_user.id,
        **data.model_dump(),
    )
    db.add(template)
    await db.flush()
    return await _template_payload(db, template)


@router.get("/templates/{template_id}")
async def get_project_template(
    template_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    template = (
        await db.execute(
            select(ProjectTemplate).where(
                ProjectTemplate.id == template_id,
                or_(ProjectTemplate.tenant_id.is_(None), ProjectTemplate.tenant_id == tenant_id),
                or_(ProjectTemplate.is_published.is_(True), ProjectTemplate.created_by_user_id == current_user.id),
            )
        )
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="Project template not found")
    return await _template_payload(db, template)


@router.get("/bootstrap-options")
async def get_project_bootstrap_options(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    agents = (
        (
            await db.execute(
                select(Agent)
                .where(
                    Agent.tenant_id == tenant_id,
                    Agent.is_deleted.is_(False),
                    or_(Agent.creator_id == current_user.id, Agent.access_mode == "company"),
                )
                .order_by(Agent.name)
            )
        )
        .scalars()
        .all()
    )
    skills = (
        (
            await db.execute(
                select(Skill).where(or_(Skill.tenant_id == tenant_id, Skill.tenant_id.is_(None))).order_by(Skill.name)
            )
        )
        .scalars()
        .all()
    )
    mcp_servers = (
        (
            await db.execute(
                select(MCPServer)
                .where(or_(MCPServer.tenant_id == tenant_id, MCPServer.tenant_id.is_(None)))
                .order_by(MCPServer.display_name)
            )
        )
        .scalars()
        .all()
    )
    users = (
        (
            await db.execute(
                select(User).where(User.tenant_id == tenant_id, User.is_active.is_(True)).order_by(User.display_name)
            )
        )
        .scalars()
        .all()
    )
    agent_ids = [agent.id for agent in agents]
    inherited_tools = []
    if agent_ids:
        inherited_tools = (
            await db.execute(
                select(AgentTool, Tool)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(
                    AgentTool.agent_id.in_(agent_ids),
                    AgentTool.enabled.is_(True),
                    Tool.enabled.is_(True),
                    or_(Tool.tenant_id == tenant_id, Tool.tenant_id.is_(None)),
                )
                .order_by(AgentTool.agent_id, Tool.display_name)
            )
        ).all()
    capabilities = [
        {
            "id": str(skill.id),
            "capability_id": str(skill.id),
            "type": "skill",
            "name": skill.name,
            "description": skill.description,
            "source": "shared",
            "owner_agent_id": None,
            "enabled": True,
        }
        for skill in skills
    ] + [
        {
            "id": str(server.id),
            "capability_id": str(server.id),
            "type": "mcp",
            "name": server.display_name or server.name,
            "description": server.instructions or "",
            "source": "shared",
            "owner_agent_id": None,
            "enabled": True,
        }
        for server in mcp_servers
    ]
    capabilities.extend(
        {
            "id": f"{assignment.agent_id}:{tool.id}",
            "capability_id": str(tool.id),
            "type": "mcp" if tool.type == "mcp" else "tool",
            "name": tool.display_name or tool.name,
            "description": tool.description,
            "source": "inherited",
            "owner_agent_id": str(assignment.agent_id),
            "enabled": assignment.enabled,
            "config": assignment.config or {},
        }
        for assignment, tool in inherited_tools
    )
    return {
        "agents": [
            {
                "id": str(agent.id),
                "name": agent.name,
                "role_description": agent.role_description,
                "status": agent.status,
                "agent_type": agent.agent_type,
            }
            for agent in agents
        ],
        "skills": [
            {"id": str(skill.id), "name": skill.name, "description": skill.description, "category": skill.category}
            for skill in skills
        ],
        "mcp_servers": [
            {
                "id": str(server.id),
                "name": server.name,
                "display_name": server.display_name,
                "transport": server.transport,
            }
            for server in mcp_servers
        ],
        "users": [
            {
                "id": str(user.id),
                "name": user.display_name,
                "email": getattr(user, "email", None),
            }
            for user in users
            if user.id != current_user.id
        ],
        "capabilities": capabilities,
    }


@router.post("/from-template", status_code=status.HTTP_201_CREATED)
async def create_project_from_template(
    data: ProjectFromTemplateCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tenant_id = _tenant_id(current_user)
    template = (
        await db.execute(
            select(ProjectTemplate).where(
                ProjectTemplate.id == data.template_id,
                or_(ProjectTemplate.tenant_id.is_(None), ProjectTemplate.tenant_id == tenant_id),
                or_(ProjectTemplate.is_published.is_(True), ProjectTemplate.created_by_user_id == current_user.id),
            )
        )
    ).scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail="Project template not found")
    definition = {**(template.definition or {}), **data.overrides}
    payload = ProjectCreate(
        name=data.name or template.name,
        description=data.description if data.description is not None else template.description,
        goal=definition.get("goal", definition.get("objective", "")),
        success_criteria=definition.get("success_criteria", []),
        visibility=data.visibility,
        template_id=template.id,
        settings=definition.get("settings", {}),
        members=definition.get("members", []),
        capabilities=definition.get("capabilities", []),
        shared_with_user_ids=definition.get("shared_with_user_ids", []),
    )
    project = await create_project(db, current_user, payload)
    return await project_summary(db, project)


@router.get("")
async def list_projects(
    scope: str = Query("mine", pattern="^(mine|shared|running|archived|all)$"),
    q: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Project).where(accessible_projects_clause(current_user))
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
    return [await project_summary(db, project) for project in projects]


@router.post("", status_code=status.HTTP_201_CREATED)
async def post_project(
    data: ProjectCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await create_project(db, current_user, data)
    return await project_summary(db, project)


@router.get("/{project_id}")
async def get_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    return await project_summary(db, project)


@router.get("/{project_id}/dashboard")
async def get_project_dashboard(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    summary = await project_summary(db, project)
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
        "runs": runs,
        "events": events,
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
    acl_change = bool({"visibility", "shared_with_user_ids"} & data.model_fields_set)
    project = (
        await require_owner(db, current_user, project_id)
        if acl_change
        else await require_project(db, current_user, project_id, edit=True)
    )
    if project.status == "planning" and updates.get("status") == "running":
        raise HTTPException(status_code=409, detail="Confirm the Leader kickoff before starting a planning project")
    shared_ids = updates.pop("shared_with_user_ids", None)
    requested_visibility = updates.pop("visibility", None)
    if requested_visibility == "shared" and shared_ids is not None and not shared_ids:
        raise HTTPException(status_code=422, detail="A shared project requires at least one shared user")
    if requested_visibility == "private" and shared_ids:
        raise HTTPException(status_code=422, detail="A private project cannot include shared users")
    for key, value in updates.items():
        setattr(project, key, value)
    if shared_ids is not None:
        await replace_access_grants(db, project, shared_ids, actor_user_id=current_user.id)
    elif requested_visibility == "private":
        await replace_access_grants(db, project, [], actor_user_id=current_user.id)
    elif requested_visibility == "shared" and project.visibility != "shared":
        raise HTTPException(status_code=422, detail="Set shared_with_user_ids when sharing a project")
    add_event(db, project, "project.updated", "Project settings updated", actor_user_id=current_user.id)
    await db.flush()
    await db.refresh(project)
    return await project_summary(db, project)


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


@router.delete("/{project_id}")
async def archive_project(
    project_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_owner(db, current_user, project_id)
    project.status = "archived"
    add_event(
        db, project, "project.archived", "Project archived without deleting history", actor_user_id=current_user.id
    )
    return {"ok": True, "status": "archived"}


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
    project = await require_owner(db, current_user, project_id)
    deleted = await db.execute(
        delete(ProjectAccessGrant).where(
            ProjectAccessGrant.id == grant_id,
            ProjectAccessGrant.project_id == project.id,
            ProjectAccessGrant.tenant_id == project.tenant_id,
        )
    )
    if not deleted.rowcount:
        raise HTTPException(status_code=404, detail="Access grant not found")
    remaining = (
        await db.execute(select(func.count(ProjectAccessGrant.id)).where(ProjectAccessGrant.project_id == project.id))
    ).scalar_one()
    if remaining == 0:
        project.visibility = "private"
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


@router.post("/{project_id}/members", response_model=ProjectMemberOut, status_code=201)
async def create_project_member(
    project_id: uuid.UUID,
    data: ProjectMemberCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    return await add_member(db, project, data, actor_user_id=current_user.id)


@router.patch("/{project_id}/members/{member_id}", response_model=ProjectMemberOut)
async def patch_project_member(
    project_id: uuid.UUID,
    member_id: uuid.UUID,
    data: ProjectMemberUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
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
    updates = data.model_dump(exclude_unset=True)
    if updates.get("is_leader"):
        await db.execute(
            ProjectMemberSnapshot.__table__.update()
            .where(ProjectMemberSnapshot.project_id == project.id)
            .values(is_leader=False)
        )
    for key, value in updates.items():
        setattr(member, key, value)
    if member.is_leader and member.is_enabled is False:
        raise HTTPException(status_code=422, detail="Disable leadership or assign another leader first")
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
        raise HTTPException(status_code=422, detail="Leader must be an enabled project member")
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
    return (
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


@router.post("/{project_id}/capabilities", response_model=CapabilityOut, status_code=201)
async def create_project_capability(
    project_id: uuid.UUID,
    data: ProjectCapabilityCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    return await add_capability(db, project, data, actor_user_id=current_user.id)


@router.patch("/{project_id}/capabilities/{binding_id}", response_model=CapabilityOut)
async def patch_project_capability(
    project_id: uuid.UUID,
    binding_id: uuid.UUID,
    data: CapabilityUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
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
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(binding, key, value)
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
        raise HTTPException(status_code=422, detail="Agent must be an enabled project member")


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
    return (
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


@router.post("/{project_id}/runs", response_model=ProjectRunOut, status_code=201)
async def create_project_run(
    project_id: uuid.UUID,
    data: ProjectRunCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
    await _require_member_agent(db, project, data.agent_id)
    if data.work_item_id:
        exists_item = (
            await db.execute(
                select(ProjectWorkItem.id).where(
                    ProjectWorkItem.id == data.work_item_id,
                    ProjectWorkItem.project_id == project.id,
                    ProjectWorkItem.tenant_id == project.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if exists_item is None:
            raise HTTPException(status_code=422, detail="Work item is not in this project")
    run = ProjectRun(
        tenant_id=project.tenant_id, project_id=project.id, initiated_by_user_id=current_user.id, **data.model_dump()
    )
    db.add(run)
    await db.flush()
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
    )
    await db.flush()
    await db.refresh(run)
    return run


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
        f"Run status changed to {run.status}",
        actor_user_id=current_user.id,
        actor_agent_id=run.agent_id,
        work_item_id=run.work_item_id,
        run_id=run.id,
    )
    await db.flush()
    await db.refresh(run)
    return run


@router.get("/{project_id}/runs/{run_id}/member-snapshots", response_model=list[ProjectRunMemberSnapshotOut])
async def list_run_snapshots(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    return (
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
    return (await db.execute(stmt.order_by(ProjectEvent.created_at.desc()).limit(limit))).scalars().all()


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
    return event


def _group_session_payload(session: ChatSession, project: Project) -> dict:
    policies = dict((project.settings or {}).get("policies") or {})
    mention_limit = min(8, max(1, int(policies.get("max_group_mentions_per_message", 4))))
    configured_wake_budget = max(0, int(policies.get("max_a2a_wakes", mention_limit)))
    # A Human message always gets one Leader turn, even when an old project
    # policy configured a zero A2A wake budget. The remainder is available to
    # explicit, non-Leader mentions.
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
    metadata = dict(message.message_meta or {})
    return {
        "id": str(message.id),
        "session_id": message.conversation_id,
        "content": message.content,
        "display_content": message.content,
        "role": message.role,
        "sender_user_id": str(message.sender_user_id) if message.sender_user_id else None,
        "sender_agent_id": str(message.sender_agent_id) if message.sender_agent_id else None,
        "attachments": metadata.get("attachments", []),
        "metadata": metadata,
        "message_meta": metadata,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def _leader_session_payload(session: ChatSession, discussion_count: int) -> dict:
    return {
        "id": str(session.id),
        "project_id": str(session.project_id) if session.project_id else None,
        "agent_id": str(session.agent_id),
        "user_id": str(session.user_id) if session.user_id else None,
        "title": session.title,
        "source_channel": session.source_channel,
        "is_group": False,
        "discussion_count": discussion_count,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "last_message_at": session.last_message_at.isoformat() if session.last_message_at else None,
    }


def _kickoff_transcript(
    project: Project,
    messages: list[ChatMessage],
    confirmation: str,
    confirmed_at: datetime,
) -> str:
    lines = [
        f"# {project.name} · Kickoff transcript",
        "",
        f"Project ID: `{project.id}`",
        "",
        f"Goal: {project.goal}",
        "",
        "## Planning discussion",
        "",
    ]
    role_labels = {"user": "User", "assistant": "Leader", "system": "System", "tool_call": "Tool"}
    for message in messages:
        actor = role_labels.get(message.role, message.role.title())
        timestamp = message.created_at.isoformat() if message.created_at else "unknown time"
        lines.extend([f"### {actor} · {timestamp}", ""])
        content = message.content.strip() or "_(empty message)_"
        lines.extend([f"> {line}" if line else ">" for line in content.splitlines()])
        lines.append("")
    lines.extend(["## User confirmation", "", f"Confirmed at: {confirmed_at.isoformat()}", "", confirmation, ""])
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
            await db.execute(
                select(func.count(ChatMessage.id)).where(ChatMessage.conversation_id == str(session.id))
            )
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
    """Freeze Leader planning evidence and start one durable Leader child.

    Confirmation is the only implicit wake in this flow and targets only the
    enabled Leader. Group messages remain append-only and structured mentions
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
                "awakened_agent_ids": [payload.get("leader_agent_id")] if dispatch_result.get("subagent_run_id") else [],
                "subagent_run_id": dispatch_result.get("subagent_run_id"),
                "subagent_session_id": dispatch_result.get("subagent_session_id"),
                "git_start_commit": payload.get("git_start_commit"),
                "transcript_path": payload.get("transcript_path"),
                "transcript_commit": payload.get("transcript_commit"),
                "recovered": True,
            }
    if project.status != "planning":
        raise HTTPException(status_code=409, detail="Only a planning project can be confirmed")

    leader = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.is_leader.is_(True),
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if leader is None:
        raise HTTPException(status_code=422, detail="Project needs an enabled Leader before kickoff")
    leader_session = await ensure_project_leader_session(db, project)
    discussion = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == str(leader_session.id))
            .order_by(ChatMessage.created_at, ChatMessage.id)
        )
    ).scalars().all()
    roles = {message.role for message in discussion}
    if "user" not in roles or "assistant" not in roles:
        raise HTTPException(
            status_code=422,
            detail="Kickoff requires at least one User message and one Leader response",
        )

    confirmation = (data.confirmation or "I confirm this plan and authorize the Leader to begin execution.").strip()
    confirmed_at = datetime.now(timezone.utc)
    transcript = _kickoff_transcript(project, discussion, confirmation, confirmed_at)
    transcript_sha256 = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    await reconcile_project_repository_operations(project.id, db=db)
    git_start = await repository_state(project, limit=1)
    transcript_commit = await write_project_file(project, "docs/kickoff-transcript.md", transcript)
    group_session = await ensure_project_group_session(db, project)
    now = confirmed_at
    kickoff_message = ChatMessage(
        id=uuid.uuid4(),
        agent_id=group_session.agent_id,
        user_id=current_user.id,
        sender_user_id=current_user.id,
        role="user",
        content=(
            f"Kickoff confirmed. Leader @{leader.name_snapshot} may begin driving the project. "
            "The frozen planning record is in docs/kickoff-transcript.md."
        ),
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
    task = (
        "The User confirmed project kickoff. Act as Project Leader: drive the agreed goal autonomously, "
        "coordinate only explicit recipients, preserve all output in project Git, and report progress to the group.\n\n"
        + transcript
    )
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
    }
    run = ProjectRun(
        tenant_id=project.tenant_id,
        project_id=project.id,
        agent_id=leader.agent_id,
        initiated_by_user_id=current_user.id,
        status="queued",
        trigger_type="leader_kickoff",
        input={
            "leader_session_id": str(leader_session.id),
            "group_session_id": str(group_session.id),
            "leader_agent_id": str(leader.agent_id),
            "confirmation": confirmation,
            "conversation_snapshot": {
                "message_count": len(discussion),
                "message_ids": [str(message.id) for message in discussion],
                "last_message_at": discussion[-1].created_at.isoformat() if discussion[-1].created_at else None,
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
    await db.flush()
    await freeze_run_members(db, project, run)
    await db.commit()
    try:
        dispatch_result = await dispatch_project_run(run.id)
    except Exception as exc:
        # The committed ProjectRun is the durable outbox. A daemon retry owns
        # recovery, so this response never rolls the project back to planning.
        dispatch_result = {"status": "initializing", "error": str(exc)}
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
    messages = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == str(session.id))
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {
        "session": _group_session_payload(session, project),
        "items": [_group_message_payload(row) for row in reversed(messages)],
    }


@router.post("/{project_id}/group-sessions/{session_id}/messages", status_code=201)
async def create_project_group_message(
    project_id: uuid.UUID,
    session_id: uuid.UUID,
    data: ProjectGroupMessageCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Append a group message, route Human input to Leader, and wake mentions."""
    from app.services.subagent_runtime import dispatch_project_run

    project = await require_project(db, current_user, project_id, edit=True)
    if data.sender_agent_id is not None:
        raise HTTPException(
            status_code=422,
            detail="Human project REST callers cannot impersonate an Agent sender",
        )
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

    mention_ids = list(dict.fromkeys(data.mentions))
    leader = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.is_leader.is_(True),
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if leader is None:
        raise HTTPException(status_code=422, detail="Project needs an enabled Leader for Human group messages")
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
            detail=f"Leader and mentions exceed this project's per-message wake budget ({wake_budget})",
        )
    required_agent_ids = set(wake_agent_ids)
    members = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id.in_(required_agent_ids),
            )
        )
    ).scalars().all()
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
        pending_runs = (
            await db.execute(
                select(ProjectRun).where(
                    ProjectRun.project_id == project.id,
                    ProjectRun.input["group_message_id"].as_string() == str(existing.id),
                    ProjectRun.trigger_type.in_(["group_leader_message", "group_mention"]),
                )
            )
        ).scalars().all()
        await db.commit()
        for pending_run in pending_runs:
            if not dict(pending_run.output or {}).get("subagent_run_id"):
                await dispatch_project_run(pending_run.id)
        await db.refresh(existing)
        meta = dict(existing.message_meta or {})
        return {
            "message": _group_message_payload(existing),
            "awakened_agent_ids": meta.get("awakened_agent_ids", []),
            "default_leader_agent_id": meta.get("default_leader_agent_id"),
            "subagent_runs": meta.get("subagent_runs", []),
            "idempotent_replay": True,
        }

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
        execution_content = (
            "处理项目群聊中附带的文件，并把结论回复到项目群。附件："
            + str(data.attachments)
        )
    project_runs: list[ProjectRun] = []
    for agent_id in wake_agent_ids:
        member = member_by_agent[agent_id]
        project_run = ProjectRun(
            tenant_id=project.tenant_id,
            project_id=project.id,
            agent_id=agent_id,
            initiated_by_user_id=current_user.id,
            status="queued",
            trigger_type="group_leader_message" if agent_id == default_leader_agent_id else "group_mention",
            input={
                "group_session_id": str(session.id),
                "group_message_id": str(message.id),
                "mentioned_agent_id": str(agent_id),
                "wake_reason": "default_leader" if agent_id == default_leader_agent_id else "structured_mention",
                "initiator_user_id": str(current_user.id),
                "dispatch": {
                    "group_session_id": str(session.id),
                    "project_member_id": str(member.id),
                    "turn_anchor_id": str(message.id),
                    "task": execution_content,
                },
            },
            output={"group_session_id": str(session.id)},
        )
        db.add(project_run)
        await db.flush()
        await freeze_run_members(db, project, project_run)
        project_runs.append(project_run)
    # Message and every target run form one durable outbox transaction. The
    # Subagent daemon can recover all rows after a process exit.
    await db.commit()

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
            subagent_rows.append({
                "project_run_id": str(project_run.id),
                "run_id": str(run_id) if run_id else None,
                "session_id": str(run_id) if run_id else None,
                "agent_id": str(agent_id),
                "status": run_status,
            })
        except Exception as exc:
            # Keep the durable queued run retryable; the daemon owns recovery.
            subagent_rows.append({
                "project_run_id": str(project_run.id),
                "run_id": None,
                "session_id": None,
                "agent_id": str(agent_id),
                "status": "queued",
                "error": str(exc),
            })

    message = await db.get(ChatMessage, message.id, with_for_update=True)
    if message is None:
        raise HTTPException(status_code=500, detail="Group message disappeared during wake dispatch")
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
            "Appended project group message and dispatched Leader plus structured mentions",
            actor_user_id=current_user.id,
            actor_agent_id=None,
            metadata=event_metadata,
        )
    else:
        event.event_metadata = event_metadata
    await db.flush()
    await db.commit()
    await db.refresh(message)
    return {
        "message": _group_message_payload(message),
        "event_id": str(event.id),
        "awakened_agent_ids": awakened,
        "default_leader_agent_id": str(default_leader_agent_id),
        "subagent_runs": subagent_rows,
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
    if data.from_agent_id == data.to_agent_id:
        raise HTTPException(status_code=422, detail="from_agent_id and to_agent_id must differ")
    await _require_member_agent(db, project, data.from_agent_id)
    await _require_member_agent(db, project, data.to_agent_id)
    group_session = await ensure_project_group_session(db, project)
    run = ProjectRun(
        tenant_id=project.tenant_id,
        project_id=project.id,
        work_item_id=data.work_item_id,
        agent_id=data.to_agent_id,
        initiated_by_user_id=current_user.id,
        status="queued",
        trigger_type="a2a",
        input={
            "from_agent_id": str(data.from_agent_id),
            "to_agent_id": str(data.to_agent_id),
            "message": data.message,
            "mode": data.mode,
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
        f"Queued direct {data.mode} wake between project agents",
        actor_user_id=current_user.id,
        actor_agent_id=data.from_agent_id,
        from_agent_id=data.from_agent_id,
        to_agent_id=data.to_agent_id,
        work_item_id=data.work_item_id,
        run_id=run.id,
        metadata={
            "mode": data.mode,
            "message": data.message,
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
    project = await require_project(db, current_user, project_id, edit=True)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await restore_as_new_commit(project, data.commit, data.message)
    _record_git_head(project, result["commit"])
    event = add_event(
        db,
        project,
        "git.restore_commit.created",
        f"Created a new restore commit from {data.commit[:12]}",
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


@router.put("/{project_id}/files")
async def put_project_file(
    project_id: uuid.UUID,
    data: ProjectFileWriteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Atomically write one project file and immediately create its Git commit."""

    project = await require_project(db, current_user, project_id, edit=True)
    await reconcile_project_repository_operations(project.id, db=db)
    result = await write_project_file(project, data.path, data.content)
    _record_git_head(project, result["commit"])
    event = add_event(
        db,
        project,
        "project.file.committed",
        f"Wrote and committed project file {result['path']}",
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
    )
    _record_git_head(project, result["commit"])
    commit_event = add_event(
        db,
        project,
        "git.commit.created",
        f"Created project Git commit {result['commit'][:12]}",
        actor_user_id=current_user.id,
        metadata=result,
    )
    milestone_event = None
    if data.milestone:
        milestone_event = add_event(
            db,
            project,
            "git.milestone.created",
            f"Created delivery milestone: {data.message}",
            actor_user_id=current_user.id,
            metadata=result,
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


@router.get("/{project_id}/git")
async def get_git_state(
    project_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id)
    await reconcile_project_repository_operations(project.id, db=db)
    return await repository_state(project, limit)
