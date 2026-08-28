"""Minimal lifecycle for Agents owned by a project repository."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import build_visible_agents_query
from app.models.agent import Agent
from app.models.participant import Participant
from app.models.project import Project, ProjectMemberSnapshot
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.project import ProjectAgentCreate, ProjectAgentUpdate, ProjectMemberCreate
from app.services.project_agent_workspace import (
    build_project_agent_identity_defaults,
    create_project_agent_workspace,
    deactivate_project_agent_workspace,
    project_agent_workspace,
    promote_project_agent_workspace,
)
from app.services.project_git_service import (
    commit_project_changes,
    project_repo_path,
    project_user_git_email,
)
from app.services.project_member_runtime import (
    clone_source_agent_tool_dependencies,
    initialize_project_agent_tool_policy,
)
from app.services.project_service import (
    add_event,
    add_member,
    deactivate_project_member,
    restore_project_member,
)
from app.services.project_skill_assets import snapshot_source_agent_skills


def _project_root(project: Project) -> Path:
    return project_repo_path(project.tenant_id, project.id)


async def _visible_standard_source(
    db: AsyncSession,
    user: User,
    source_agent_id: uuid.UUID,
) -> Agent:
    source = (
        await db.execute(
            build_visible_agents_query(user, tenant_id=project_tenant_id(user)).where(
                Agent.id == source_agent_id,
                Agent.scope == "standard",
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=422, detail="源数字员工不可用")
    if source.agent_type != "native":
        raise HTTPException(status_code=422, detail="仅原生数字员工可复制到项目")
    return source


def project_tenant_id(user: User) -> uuid.UUID:
    if user.tenant_id is None:
        raise HTTPException(status_code=403, detail="A tenant membership is required")
    return user.tenant_id


def _new_project_agent(
    project: Project,
    owner: User,
    data: ProjectAgentCreate,
    source: Agent | None,
    tenant_default_model_id: uuid.UUID | None,
) -> Agent:
    agent_id = uuid.uuid4()
    name = (data.name or (source.name if source else "")).strip()
    if not name:
        raise HTTPException(status_code=422, detail="项目数字员工名称不能为空")
    role = data.role_description if data.role_description is not None else (source.role_description if source else "")
    agent = Agent(
        id=agent_id,
        name=name,
        avatar_url=source.avatar_url if source else None,
        role_description=role,
        bio=source.bio if source else None,
        welcome_message=source.welcome_message if source else None,
        creator_id=owner.id,
        tenant_id=project.tenant_id,
        scope="project",
        project_id=project.id,
        source_agent_id=source.id if source else None,
        agent_dir=Agent.project_agent_dir(agent_id),
        agent_type="native",
        status="idle",
        primary_model_id=source.primary_model_id if source else tenant_default_model_id,
        fallback_model_id=source.fallback_model_id if source else None,
        context_window_size=source.context_window_size if source else 100,
        max_tool_rounds=source.max_tool_rounds if source else 50,
        access_mode="private",
        company_access_level="use",
        heartbeat_enabled=False,
    )
    if source is not None:
        agent.autonomy_policy = dict(source.autonomy_policy or {})
    return agent


def _write_text_if_changed(path: Path, content: str) -> bool:
    if path.is_symlink():
        raise HTTPException(status_code=409, detail="项目数字员工身份文件不能是符号链接")
    current = path.read_text(encoding="utf-8") if path.is_file() else None
    if current == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return True


async def create_project_agent(
    db: AsyncSession,
    project: Project,
    owner: User,
    data: ProjectAgentCreate,
) -> tuple[Agent, ProjectMemberSnapshot]:
    """Create one project Agent and bind its durable member snapshot."""

    source = await _visible_standard_source(db, owner, data.source_agent_id) if data.source_agent_id else None
    tenant_default_model_id = None
    if source is None:
        tenant = await db.get(Tenant, project.tenant_id)
        tenant_default_model_id = tenant.default_model_id if tenant is not None else None
    agent = _new_project_agent(project, owner, data, source, tenant_default_model_id)
    db.add(agent)
    await db.flush()
    db.add(Participant(type="agent", ref_id=agent.id, display_name=agent.name, avatar_url=agent.avatar_url))
    member = await add_member(
        db,
        project,
        ProjectMemberCreate(agent_id=agent.id, is_leader=data.is_leader),
        actor_user_id=owner.id,
    )

    default_soul, default_memory = build_project_agent_identity_defaults(
        project_name=project.name,
        project_goal=project.goal or "",
        success_criteria=project.success_criteria or [],
        agent_name=agent.name,
        role_description=agent.role_description or "",
    )

    layout = project_agent_workspace(_project_root(project), agent.id)
    try:
        await create_project_agent_workspace(
            _project_root(project),
            agent.id,
            source_agent_id=source.id if source else None,
            default_soul=default_soul,
            default_memory=default_memory,
            # A project copy keeps the source employee's professional identity,
            # but starts with project-owned memory and an empty workspace. Runtime
            # history and delivery files belong to the source employee's global
            # scope and must never cross into a newly created project.
            copy_source_memory=False,
            copy_source_workspace=False,
        )
        skill_bindings = (
            await snapshot_source_agent_skills(
                db,
                project,
                source_agent_id=source.id,
                project_agent_id=agent.id,
            )
            if source is not None
            else []
        )
        tool_bindings = (
            await clone_source_agent_tool_dependencies(
                db,
                project,
                source_agent_id=source.id,
                project_agent_id=agent.id,
                project_defaults_only=True,
            )
            if source is not None
            else []
        )
        await initialize_project_agent_tool_policy(
            db,
            project,
            project_agent_id=agent.id,
        )
        # Empty form fields must not erase the professional identity generated for
        # a new project Agent. An intentional identity reset is an owner-only
        # update operation, not a side effect of creation.
        if data.soul is not None and data.soul.strip():
            await asyncio.to_thread(_write_text_if_changed, layout.soul, data.soul)
        if data.core_memory is not None and data.core_memory.strip():
            await asyncio.to_thread(_write_text_if_changed, layout.memory, data.core_memory)
        commit = await commit_project_changes(
            project,
            f"创建项目数字员工：{agent.name}",
            [agent.agent_dir],
            author_name=owner.display_name,
            author_email=project_user_git_email(owner.id),
        )
    except Exception:
        if layout.root.exists():
            await asyncio.to_thread(shutil.rmtree, layout.root, True)
        raise
    add_event(
        db,
        project,
        "project_agent.created",
        f"项目数字员工已创建：{agent.name}",
        actor_user_id=owner.id,
        actor_agent_id=agent.id,
        metadata={
            "agent_id": str(agent.id),
            "member_id": str(member.id),
            "source_agent_id": str(source.id) if source else None,
            "skill_count": len(skill_bindings),
            "tool_dependency_count": len(tool_bindings),
            "commit": commit["commit"],
        },
    )
    await db.flush()
    return agent, member


async def list_project_agents(
    db: AsyncSession,
    project: Project,
) -> list[tuple[Agent, ProjectMemberSnapshot]]:
    rows = await db.execute(
        select(Agent, ProjectMemberSnapshot)
        .join(
            ProjectMemberSnapshot,
            (ProjectMemberSnapshot.agent_id == Agent.id) & (ProjectMemberSnapshot.project_id == project.id),
        )
        .where(
            Agent.project_id == project.id,
            Agent.tenant_id == project.tenant_id,
            Agent.scope == "project",
            Agent.is_deleted.is_(False),
        )
        .order_by(ProjectMemberSnapshot.is_leader.desc(), Agent.created_at)
    )
    return list(rows.all())


async def get_project_agent(
    db: AsyncSession,
    project: Project,
    agent_id: uuid.UUID,
) -> tuple[Agent, ProjectMemberSnapshot]:
    row = (
        await db.execute(
            select(Agent, ProjectMemberSnapshot)
            .join(
                ProjectMemberSnapshot,
                (ProjectMemberSnapshot.agent_id == Agent.id) & (ProjectMemberSnapshot.project_id == project.id),
            )
            .where(
                Agent.id == agent_id,
                Agent.project_id == project.id,
                Agent.tenant_id == project.tenant_id,
                Agent.scope == "project",
                Agent.is_deleted.is_(False),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="未找到项目数字员工")
    return row[0], row[1]


async def serialize_project_agent(
    project: Project,
    agent: Agent,
    member: ProjectMemberSnapshot,
) -> dict:
    layout = project_agent_workspace(_project_root(project), agent.id)

    def read_assets() -> tuple[str, str]:
        if layout.soul.is_symlink() or layout.memory.is_symlink():
            raise HTTPException(
                status_code=409,
                detail="项目数字员工身份文件不能是符号链接",
            )
        soul = layout.soul.read_text(encoding="utf-8") if layout.soul.is_file() else ""
        memory = layout.memory.read_text(encoding="utf-8") if layout.memory.is_file() else ""
        return soul, memory

    soul, memory = await asyncio.to_thread(read_assets)
    return {
        "id": agent.id,
        "project_id": project.id,
        "member_id": member.id,
        "source_agent_id": agent.source_agent_id,
        "name": agent.name,
        "role_description": agent.role_description or "",
        "avatar_url": agent.avatar_url,
        "status": agent.status,
        "agent_dir": agent.agent_dir,
        "soul": soul,
        "core_memory": memory,
        "is_leader": member.is_leader,
        "is_enabled": member.is_enabled,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


async def update_project_agent(
    db: AsyncSession,
    project: Project,
    owner: User,
    agent: Agent,
    member: ProjectMemberSnapshot,
    data: ProjectAgentUpdate,
) -> None:
    updates = data.model_dump(exclude_unset=True)
    changed_paths: list[str] = []
    if updates.get("name") is not None:
        name = updates["name"].strip()
        if not name:
            raise HTTPException(status_code=422, detail="项目数字员工名称不能为空")
        agent.name = name
        member.name_snapshot = agent.name
        participant = (
            await db.execute(
                select(Participant).where(
                    Participant.type == "agent",
                    Participant.ref_id == agent.id,
                )
            )
        ).scalar_one_or_none()
        if participant is not None:
            participant.display_name = agent.name
    if "role_description" in updates:
        agent.role_description = updates["role_description"]
        member.role_snapshot = agent.role_description or ""

    layout = project_agent_workspace(_project_root(project), agent.id)
    if updates.get("soul") is not None and await asyncio.to_thread(
        _write_text_if_changed,
        layout.soul,
        updates["soul"],
    ):
        changed_paths.append(f"{agent.agent_dir}/soul.md")
    if updates.get("core_memory") is not None and await asyncio.to_thread(
        _write_text_if_changed,
        layout.memory,
        updates["core_memory"],
    ):
        changed_paths.append(f"{agent.agent_dir}/memory.md")
    commit = None
    if changed_paths:
        commit = await commit_project_changes(
            project,
            f"更新项目数字员工：{agent.name}",
            changed_paths,
            author_name=owner.display_name,
            author_email=project_user_git_email(owner.id),
        )
    add_event(
        db,
        project,
        "project_agent.updated",
        f"项目数字员工已更新：{agent.name}",
        actor_user_id=owner.id,
        actor_agent_id=agent.id,
        metadata={"agent_id": str(agent.id), "commit": commit["commit"] if commit else None},
    )
    await db.flush()


async def deactivate_project_agent(
    db: AsyncSession,
    project: Project,
    owner: User,
    agent: Agent,
    member: ProjectMemberSnapshot,
    *,
    reason: str | None,
) -> list[uuid.UUID]:
    deactivate_project_agent_workspace(_project_root(project), agent.id)
    child_ids = await deactivate_project_member(
        db,
        project,
        member,
        actor_user_id=owner.id,
        reason=reason,
    )
    agent.status = "stopped"
    await db.flush()
    return child_ids


async def restore_project_agent(
    db: AsyncSession,
    project: Project,
    owner: User,
    agent: Agent,
    member: ProjectMemberSnapshot,
    *,
    reason: str | None,
) -> None:
    await restore_project_member(
        db,
        project,
        member,
        actor_user_id=owner.id,
        reason=reason,
    )
    agent.status = "idle"
    await db.flush()


async def promote_project_agent(
    db: AsyncSession,
    project: Project,
    owner: User,
    project_agent: Agent,
    *,
    name: str | None = None,
) -> Agent:
    """Copy a project Agent into a new independent standard Agent."""

    promoted_name = (name or project_agent.name).strip()
    if not promoted_name:
        raise HTTPException(status_code=422, detail="数字员工名称不能为空")
    promoted = Agent(
        name=promoted_name,
        avatar_url=project_agent.avatar_url,
        role_description=project_agent.role_description,
        bio=project_agent.bio,
        welcome_message=project_agent.welcome_message,
        creator_id=owner.id,
        tenant_id=project.tenant_id,
        scope="standard",
        source_agent_id=project_agent.id,
        agent_type="native",
        status="idle",
        primary_model_id=project_agent.primary_model_id,
        fallback_model_id=project_agent.fallback_model_id,
        autonomy_policy=dict(project_agent.autonomy_policy or {}),
        context_window_size=project_agent.context_window_size,
        max_tool_rounds=project_agent.max_tool_rounds,
        access_mode="private",
        company_access_level="use",
    )
    db.add(promoted)
    await db.flush()
    db.add(
        Participant(
            type="agent",
            ref_id=promoted.id,
            display_name=promoted.name,
            avatar_url=promoted.avatar_url,
        )
    )
    await promote_project_agent_workspace(
        _project_root(project),
        project_agent.id,
        promoted.id,
        overwrite=True,
    )
    add_event(
        db,
        project,
        "project_agent.promoted",
        f"项目数字员工已转为独立数字员工：{project_agent.name}",
        actor_user_id=owner.id,
        actor_agent_id=project_agent.id,
        metadata={"agent_id": str(project_agent.id), "promoted_agent_id": str(promoted.id)},
    )
    await db.flush()
    return promoted
