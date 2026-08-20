"""REST API for closed-loop AI-native project management."""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, or_, select
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
    ProjectRun,
    ProjectRunMemberSnapshot,
    ProjectTemplate,
    ProjectWorkItem,
)
from app.models.skill import Skill
from app.models.subagent_run import SubagentRun
from app.models.tool import AgentTool, Tool
from app.models.user import User
from app.schemas.project import (
    A2AWakeRequest,
    CapabilityOut,
    CapabilityUpdate,
    GitBranchRequest,
    GitCommitRequest,
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
    commit_project_changes,
    create_branch,
    list_project_files,
    repository_state,
    restore_as_new_commit,
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
    project = await require_project(db, current_user, project_id, edit=True)
    updates = data.model_dump(exclude_unset=True)
    shared_ids = updates.pop("shared_with_user_ids", None)
    requested_visibility = updates.pop("visibility", None)
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


def _group_session_payload(session: ChatSession) -> dict:
    return {
        "id": str(session.id),
        "project_id": str(session.project_id) if session.project_id else None,
        "title": session.title,
        "group_name": session.group_name,
        "source_channel": session.source_channel,
        "access_agent_id": str(session.agent_id),
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
    return _group_session_payload(session)


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
    return {"session": _group_session_payload(session), "items": [_group_message_payload(row) for row in reversed(messages)]}


@router.post("/{project_id}/group-sessions/{session_id}/messages", status_code=201)
async def create_project_group_message(
    project_id: uuid.UUID,
    session_id: uuid.UUID,
    data: ProjectGroupMessageCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Append one group-visible message and wake only structured mentions."""
    from app.services.subagent_runtime import (
        SubagentError,
        append_subagent_message,
        create_subagent,
    )

    project = await require_project(db, current_user, project_id, edit=True)
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
    policies = dict((project.settings or {}).get("policies") or {})
    mention_limit = min(8, max(1, int(policies.get("max_group_mentions_per_message", 4))))
    wake_budget = min(mention_limit, max(0, int(policies.get("max_a2a_wakes", mention_limit))))
    if len(mention_ids) > mention_limit or len(mention_ids) > wake_budget:
        raise HTTPException(
            status_code=422,
            detail=f"Structured mentions exceed this project's per-message wake budget ({wake_budget})",
        )
    if data.sender_agent_id and data.sender_agent_id in mention_ids:
        raise HTTPException(status_code=422, detail="An Agent cannot mention itself")

    required_agent_ids = set(mention_ids)
    if data.sender_agent_id:
        required_agent_ids.add(data.sender_agent_id)
    members = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id.in_(required_agent_ids),
            )
        )
    ).scalars().all() if required_agent_ids else []
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
        meta = dict(existing.message_meta or {})
        return {
            "message": _group_message_payload(existing),
            "awakened_agent_ids": meta.get("awakened_agent_ids", []),
            "subagent_runs": meta.get("subagent_runs", []),
            "idempotent_replay": True,
        }

    message = ChatMessage(
        agent_id=session.agent_id,
        user_id=current_user.id,
        sender_user_id=None if data.sender_agent_id else current_user.id,
        sender_agent_id=data.sender_agent_id,
        role="user",
        content=data.content.strip(),
        conversation_id=str(session.id),
        external_event_key=event_key,
        message_meta={
            "kind": "project_group_message",
            "project_id": str(project.id),
            "visible_to_group": True,
            "mentions": [str(agent_id) for agent_id in mention_ids],
            "attachments": data.attachments,
            "awakened_agent_ids": [],
            "subagent_runs": [],
            "wake_policy": "structured_mentions_only",
            "initiator_user_id": str(current_user.id),
        },
    )
    db.add(message)
    session.last_message_at = func.now()
    await db.flush()
    await db.commit()
    await db.refresh(message)

    awakened: list[str] = []
    subagent_rows: list[dict] = []
    execution_content = (data.llm_content or data.content).strip()
    if not execution_content:
        execution_content = (
            "处理项目群聊中附带的文件，并把结论回复到项目群。附件："
            + str(data.attachments)
        )
    for agent_id in mention_ids:
        member = member_by_agent[agent_id]
        existing_run = (
            await db.execute(
                select(SubagentRun).where(
                    SubagentRun.parent_session_id == session.id,
                    SubagentRun.project_member_id == member.id,
                ).order_by(SubagentRun.id).limit(1)
            )
        ).scalar_one_or_none()
        project_run = ProjectRun(
            tenant_id=project.tenant_id,
            project_id=project.id,
            agent_id=agent_id,
            initiated_by_user_id=current_user.id,
            status="queued",
            trigger_type="group_mention",
            input={
                "group_session_id": str(session.id),
                "group_message_id": str(message.id),
                "mentioned_agent_id": str(agent_id),
                "initiator_user_id": str(current_user.id),
            },
            output={"group_session_id": str(session.id)},
        )
        db.add(project_run)
        await db.flush()
        await freeze_run_members(db, project, project_run)
        # Commit the auditable wake before publishing the child input. A fast
        # worker can then always resolve project_run_id when it finishes.
        await db.commit()
        try:
            if existing_run is None:
                durable_run, _created = await create_subagent(
                    agent_id=agent_id,
                    execution_user_id=project.owner_user_id,
                    parent_session_id=str(session.id),
                    origin_tool_call_id=f"project-member:{member.id}",
                    task=execution_content,
                    mode="async",
                    fork=True,
                    turn_anchor_id=message.id,
                    project_run_id=project_run.id,
                )
                run_status = durable_run.status
                run_id = durable_run.id
            else:
                run_id = existing_run.id
                run_status = await append_subagent_message(
                    agent_id=agent_id,
                    parent_session_id=str(session.id),
                    subagent_id=str(existing_run.id),
                    message=execution_content,
                    execution_user_id=existing_run.execution_user_id,
                    origin_tool_call_id=f"group-message:{message.id}:{agent_id}",
                    project_run_id=project_run.id,
                )
            awakened.append(str(agent_id))
            project_run.status = "queued" if run_status == "queued" else "running"
            project_run.output = {
                **dict(project_run.output or {}),
                "subagent_run_id": str(run_id),
                "subagent_session_id": str(run_id),
            }
            subagent_rows.append({
                "project_run_id": str(project_run.id),
                "run_id": str(run_id),
                "session_id": str(run_id),
                "agent_id": str(agent_id),
                "status": run_status,
            })
        except SubagentError as exc:
            project_run.status = "failed"
            project_run.finished_at = func.now()
            project_run.error = str(exc)
            subagent_rows.append({
                "project_run_id": str(project_run.id),
                "run_id": str(existing_run.id) if existing_run else None,
                "session_id": str(existing_run.id) if existing_run else None,
                "agent_id": str(agent_id),
                "status": "rejected",
                "error": str(exc),
            })

    message = await db.get(ChatMessage, message.id, with_for_update=True)
    if message is None:
        raise HTTPException(status_code=500, detail="Group message disappeared during wake dispatch")
    message.message_meta = {
        **dict(message.message_meta or {}),
        "awakened_agent_ids": awakened,
        "subagent_runs": subagent_rows,
    }
    event = add_event(
        db,
        project,
        "group.message.created",
        "Appended project group message and dispatched structured mentions",
        actor_user_id=current_user.id,
        actor_agent_id=data.sender_agent_id,
        metadata={
            "group_session_id": str(session.id),
            "group_message_id": str(message.id),
            "initiator_user_id": str(current_user.id),
            "visible_to_group": True,
            "mentioned_agent_ids": [str(agent_id) for agent_id in mention_ids],
            "awakened_agent_ids": awakened,
            "subagent_runs": subagent_rows,
            "zero_wake_default": not mention_ids,
        },
    )
    await db.flush()
    await db.commit()
    await db.refresh(message)
    return {
        "message": _group_message_payload(message),
        "event_id": str(event.id),
        "awakened_agent_ids": awakened,
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


@router.post("/{project_id}/git/restore")
async def create_git_restore(
    project_id: uuid.UUID,
    data: GitRestoreRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = await require_project(db, current_user, project_id, edit=True)
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
    return await repository_state(project, limit)
