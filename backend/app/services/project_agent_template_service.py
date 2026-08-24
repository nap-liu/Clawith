"""Database orchestration for project Agents embedded in project templates."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.mcp_server import MCPServer
from app.models.participant import Participant
from app.models.project import Project, ProjectCapabilityBinding, ProjectMemberSnapshot
from app.models.skill import Skill
from app.models.tenant import Tenant
from app.models.tool import Tool
from app.models.user import User
from app.schemas.project import ProjectCapabilityCreate, ProjectMemberCreate
from app.services.project_agent_template_assets import (
    ProjectAgentTemplateInstance,
    ProjectAgentTemplateSource,
    export_project_agent_template_assets,
    instantiate_project_agent_template_assets,
    remove_project_agent_template_instances,
)
from app.services.project_git_service import (
    commit_project_changes,
    project_repo_path,
    project_user_git_email,
)
from app.services.project_service import add_capability, add_event, add_member
from app.services.project_template_snapshot import ProjectTemplateSnapshotError, sanitize_template_scope


def _project_root(project: Project) -> Path:
    return project_repo_path(project.tenant_id, project.id)


async def export_project_agents_for_template(
    db: AsyncSession,
    project: Project,
    *,
    project_root: Path | None = None,
) -> list[dict]:
    """Return sanitized project-owned Agent assets for ``definition.agents``."""

    rows = (
        await db.execute(
            select(Agent, ProjectMemberSnapshot)
            .join(
                ProjectMemberSnapshot,
                (ProjectMemberSnapshot.project_id == project.id) & (ProjectMemberSnapshot.agent_id == Agent.id),
            )
            .where(
                Agent.scope == "project",
                Agent.project_id == project.id,
                Agent.tenant_id == project.tenant_id,
                Agent.is_deleted.is_(False),
            )
            .order_by(ProjectMemberSnapshot.is_leader.desc(), Agent.created_at)
        )
    ).all()
    sources = [
        ProjectAgentTemplateSource(
            agent_id=agent.id,
            name=agent.name,
            role_description=agent.role_description or "",
            is_leader=member.is_leader and member.is_enabled,
            is_enabled=member.is_enabled,
            runtime={
                "primary_model_id": str(agent.primary_model_id) if agent.primary_model_id else None,
                "fallback_model_id": str(agent.fallback_model_id) if agent.fallback_model_id else None,
                "autonomy_policy": dict(agent.autonomy_policy or {}),
                "context_window_size": agent.context_window_size or 100,
                "max_tool_rounds": agent.max_tool_rounds or 50,
                "max_tokens_per_day": agent.max_tokens_per_day,
                "max_tokens_per_month": agent.max_tokens_per_month,
            },
            member_config={
                key: dict(member.config_snapshot or {}).get(key, [] if key.endswith("_tools") else "")
                for key in ("project_instruction", "enabled_project_tools", "disabled_project_tools")
            },
        )
        for agent, member in rows
    ]
    return export_project_agent_template_assets(
        project_root or _project_root(project),
        sources,
        redact_values=(project.id, project.tenant_id, project.owner_user_id),
    )


async def instantiate_project_agents_from_template(
    db: AsyncSession,
    project: Project,
    owner: User,
    template_agents: list[dict],
) -> tuple[tuple[Agent, ProjectMemberSnapshot], ...]:
    """Create fresh project Agent rows and assets from a sanitized template."""

    if not template_agents:
        return ()
    leader_count = sum(
        bool(item.get("is_leader")) and bool(item.get("is_enabled", True))
        for item in template_agents
        if isinstance(item, dict)
    )
    if leader_count > 1:
        raise HTTPException(
            status_code=422, detail="A project template can define only one responsible digital employee"
        )

    agent_ids = tuple(uuid.uuid4() for _ in template_agents)
    instances = await instantiate_project_agent_template_assets(
        _project_root(project),
        template_agents,
        agent_ids=agent_ids,
    )
    try:
        tenant = await db.get(Tenant, project.tenant_id)
        default_model_id = tenant.default_model_id if tenant is not None else None
        requested_model_ids = {
            uuid.UUID(str(model_id))
            for item in template_agents
            if isinstance(item, dict)
            for model_id in (
                dict(item.get("runtime") or {}).get("primary_model_id"),
                dict(item.get("runtime") or {}).get("fallback_model_id"),
            )
            if model_id
        }
        available_model_ids = set(
            (
                await db.execute(
                    select(LLMModel.id).where(
                        LLMModel.id.in_(requested_model_ids),
                        or_(LLMModel.tenant_id == project.tenant_id, LLMModel.tenant_id.is_(None)),
                        LLMModel.enabled.is_(True),
                    )
                )
            ).scalars()
        )
        created: list[tuple[Agent, ProjectMemberSnapshot]] = []
        first_enabled_index = next(
            (index for index, instance in enumerate(instances) if instance.is_enabled),
            None,
        )
        for index, instance in enumerate(instances):
            agent = _agent_from_template(project, owner, instance, default_model_id, available_model_ids)
            db.add(agent)
            await db.flush()
            db.add(Participant(type="agent", ref_id=agent.id, display_name=agent.name))
            member = await add_member(
                db,
                project,
                ProjectMemberCreate(
                    agent_id=agent.id,
                    is_leader=instance.is_enabled
                    and (
                        instance.is_leader
                        or (leader_count == 0 and index == first_enabled_index)
                    ),
                ),
                actor_user_id=owner.id,
            )
            member.config_snapshot = {
                **dict(member.config_snapshot or {}),
                **dict(instance.member_config),
            }
            member.is_enabled = instance.is_enabled
            if not instance.is_enabled:
                agent.status = "stopped"
            created.append((agent, member))

        commit = await commit_project_changes(
            project,
            f"Create {len(created)} project digital employees from template",
            [".agents"],
            author_name=owner.display_name,
            author_email=project_user_git_email(owner.id),
        )
        add_event(
            db,
            project,
            "project_agent.template_instantiated",
            f"Created {len(created)} project digital employees from the project template",
            actor_user_id=owner.id,
            metadata={
                "agent_ids": [str(agent.id) for agent, _member in created],
                "commit": commit["commit"],
            },
        )
        await db.flush()
        return tuple(created)
    except Exception:
        await remove_project_agent_template_instances(_project_root(project), instances)
        raise


async def export_project_capabilities_for_template(db: AsyncSession, project: Project) -> list[dict]:
    """Export portable bindings only; private MCP configuration and overrides never cross."""

    agent_ids = list(
        (
            await db.execute(
                select(Agent.id)
                .join(
                    ProjectMemberSnapshot,
                    (ProjectMemberSnapshot.project_id == project.id) & (ProjectMemberSnapshot.agent_id == Agent.id),
                )
                .where(
                    Agent.scope == "project",
                    Agent.project_id == project.id,
                    Agent.tenant_id == project.tenant_id,
                    Agent.is_deleted.is_(False),
                )
                .order_by(ProjectMemberSnapshot.is_leader.desc(), Agent.created_at)
            )
        ).scalars()
    )
    agent_index = {agent_id: index for index, agent_id in enumerate(agent_ids)}
    bindings = list(
        (
            await db.execute(
                select(ProjectCapabilityBinding)
                .where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                )
                .order_by(ProjectCapabilityBinding.created_at, ProjectCapabilityBinding.id)
            )
        ).scalars()
    )
    exported: list[dict] = []
    for binding in bindings:
        if binding.capability_id is None:
            continue
        if binding.capability_type == "mcp":
            server = await db.get(MCPServer, binding.capability_id)
            if server is None or server.tenant_id is not None:
                continue
        elif binding.capability_type == "skill":
            skill = await db.get(Skill, binding.capability_id)
            if skill is None or skill.tenant_id not in {None, project.tenant_id}:
                continue
        elif binding.capability_type == "tool":
            tool = await db.get(Tool, binding.capability_id)
            if tool is None or tool.type == "mcp" or tool.tenant_id not in {None, project.tenant_id}:
                continue
        else:
            continue
        inherited_index = agent_index.get(binding.inherited_from_agent_id)
        if binding.source == "inherited" and inherited_index is None:
            continue
        exported.append(
            {
                "schema_version": 1,
                "capability_type": binding.capability_type,
                "capability_id": str(binding.capability_id),
                "capability_name": binding.capability_name,
                "source": binding.source,
                "digital_employee_index": inherited_index,
                "is_enabled": binding.is_enabled,
                "scope": sanitize_template_scope(binding.scope or {}),
            }
        )
    return exported


async def instantiate_project_capabilities_from_template(
    db: AsyncSession,
    project: Project,
    owner: User,
    raw_capabilities: object,
    created_agents: tuple[tuple[Agent, ProjectMemberSnapshot], ...],
) -> None:
    if raw_capabilities is None:
        return
    if not isinstance(raw_capabilities, list) or len(raw_capabilities) > 512:
        raise ProjectTemplateSnapshotError("Project template capability list is invalid")
    allowed_keys = {
        "schema_version",
        "capability_type",
        "capability_id",
        "capability_name",
        "source",
        "digital_employee_index",
        "is_enabled",
        "scope",
    }
    for item in raw_capabilities:
        if not isinstance(item, dict) or set(item) != allowed_keys or item.get("schema_version") != 1:
            raise ProjectTemplateSnapshotError("Project template capability entry is invalid")
        capability_type = item.get("capability_type")
        source = item.get("source")
        if capability_type not in {"skill", "mcp", "tool"} or source not in {"shared", "inherited"}:
            raise ProjectTemplateSnapshotError("Project template capability type is invalid")
        try:
            capability_id = uuid.UUID(str(item.get("capability_id")))
        except ValueError as exc:
            raise ProjectTemplateSnapshotError("Project template capability identifier is invalid") from exc
        if capability_type == "mcp":
            server = await db.get(MCPServer, capability_id)
            if server is None or server.tenant_id is not None:
                raise ProjectTemplateSnapshotError("Private MCP servers cannot be restored from a project template")
        elif capability_type == "skill":
            skill = await db.get(Skill, capability_id)
            if skill is None or skill.tenant_id not in {None, project.tenant_id}:
                raise ProjectTemplateSnapshotError("Project template skill is unavailable")
        else:
            tool = await db.get(Tool, capability_id)
            if tool is None or tool.type == "mcp" or tool.tenant_id not in {None, project.tenant_id}:
                raise ProjectTemplateSnapshotError("Project template tool is unavailable")
        inherited_agent_id = None
        if source == "inherited":
            index = item.get("digital_employee_index")
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(created_agents):
                raise ProjectTemplateSnapshotError("Project template capability owner is invalid")
            inherited_agent_id = created_agents[index][0].id
        elif item.get("digital_employee_index") is not None:
            raise ProjectTemplateSnapshotError("Shared project capability cannot have a digital employee owner")
        await add_capability(
            db,
            project,
            ProjectCapabilityCreate(
                capability_type=capability_type,
                capability_id=capability_id,
                capability_name=str(item.get("capability_name") or ""),
                source=source,
                inherited_from_agent_id=inherited_agent_id,
                is_enabled=bool(item.get("is_enabled", True)),
                scope=sanitize_template_scope(item.get("scope") or {}),
                config={},
            ),
            actor_user_id=owner.id,
        )


def _agent_from_template(
    project: Project,
    owner: User,
    instance: ProjectAgentTemplateInstance,
    default_model_id: uuid.UUID | None,
    available_model_ids: set[uuid.UUID],
) -> Agent:
    runtime = dict(instance.runtime)
    requested_primary = uuid.UUID(runtime["primary_model_id"]) if runtime.get("primary_model_id") else None
    requested_fallback = uuid.UUID(runtime["fallback_model_id"]) if runtime.get("fallback_model_id") else None
    return Agent(
        id=instance.agent_id,
        name=instance.name,
        role_description=instance.role_description,
        creator_id=owner.id,
        tenant_id=project.tenant_id,
        scope="project",
        project_id=project.id,
        agent_dir=instance.agent_dir,
        agent_type="native",
        status="idle",
        primary_model_id=requested_primary if requested_primary in available_model_ids else default_model_id,
        fallback_model_id=requested_fallback if requested_fallback in available_model_ids else None,
        autonomy_policy=dict(runtime.get("autonomy_policy") or {}),
        context_window_size=int(runtime.get("context_window_size") or 100),
        max_tool_rounds=int(runtime.get("max_tool_rounds") or 50),
        max_tokens_per_day=runtime.get("max_tokens_per_day"),
        max_tokens_per_month=runtime.get("max_tokens_per_month"),
        access_mode="private",
        company_access_level="use",
        heartbeat_enabled=False,
    )
