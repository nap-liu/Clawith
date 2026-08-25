"""Normalized project-member runtime configuration and tool assignments."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.mcp_server import MCPServer
from app.models.project import Project, ProjectCapabilityBinding, ProjectMemberSnapshot
from app.models.tool import AgentTool, Tool
from app.services.tool_enablement import tool_is_required


# Project copies start with the smallest useful local execution surface. These
# are ordinary platform tools, not a second capability system: the copy still
# uses AgentTool and the existing Agent tool panel, while every omitted tool is
# available for the project owner to enable explicitly.
PROJECT_AGENT_DEFAULT_TOOL_NAMES = frozenset(
    {
        "complete_focus_item",
        "edit_file",
        "execute_code_aio",
        "find_files",
        "list_files",
        "list_focus_items",
        "move_file",
        "read_document",
        "read_file",
        "read_image",
        "search_files",
        "send_media",
        "upsert_focus_item",
        "write_file",
    }
)


async def initialize_project_agent_tool_policy(
    db: AsyncSession,
    project: Project,
    *,
    project_agent_id: uuid.UUID,
) -> None:
    """Persist one explicit project-local state for every visible platform tool."""

    tools = (
        await db.execute(
            select(Tool).where(
                Tool.source.in_(("builtin", "admin")),
                or_(Tool.tenant_id == project.tenant_id, Tool.tenant_id.is_(None)),
            )
        )
    ).scalars().all()
    assignments = {
        row.tool_id: row
        for row in (
            (
                await db.execute(
                    select(AgentTool).where(AgentTool.agent_id == project_agent_id)
                )
            )
            .scalars()
            .all()
        )
    }
    for tool in tools:
        enabled = tool_is_required(tool.name) or (
            tool.enabled and tool.name in PROJECT_AGENT_DEFAULT_TOOL_NAMES
        )
        assignment = assignments.get(tool.id)
        if assignment is None:
            db.add(
                AgentTool(
                    agent_id=project_agent_id,
                    tool_id=tool.id,
                    enabled=enabled,
                    config={},
                    source="user_installed",
                )
            )
        else:
            assignment.enabled = enabled
            assignment.config = {}
    await db.flush()


class ProjectMemberRuntimeConfig(BaseModel):
    """Typed product-owned fields stored in ``config_snapshot``.

    Extra fields preserve existing lifecycle and template metadata. The API
    owns ``membership`` and always restores it from the persisted snapshot.
    """

    model_config = ConfigDict(extra="allow")

    primary_model_id: uuid.UUID | None = None
    fallback_model_id: uuid.UUID | None = None
    max_tool_rounds: int | None = Field(default=None, ge=1, le=200)
    project_instruction: str = Field(default="", max_length=20_000)
    enabled_project_tools: list[str] | None = None
    disabled_project_tools: list[str] = Field(default_factory=list)
    enabled_platform_tools: list[str] = Field(default_factory=list)
    disabled_platform_tools: list[str] = Field(default_factory=list)

    @field_validator(
        "enabled_project_tools",
        "disabled_project_tools",
        "enabled_platform_tools",
        "disabled_platform_tools",
    )
    @classmethod
    def normalize_tool_names(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(name for item in value if (name := str(item).strip())))


async def merge_project_member_runtime_config(
    db: AsyncSession,
    project: Project,
    member: ProjectMemberSnapshot,
    patch: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate and merge one owner-authored member runtime override."""

    if patch is None:
        raise HTTPException(status_code=422, detail="config_snapshot must be an object")
    current = dict(member.config_snapshot or {})
    merged = {**current, **dict(patch)}
    # Membership state is lifecycle-owned and must not be overwritten through
    # the runtime settings form (which may echo the current value back).
    if "membership" in current:
        merged["membership"] = current["membership"]
    try:
        normalized = ProjectMemberRuntimeConfig.model_validate(merged)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc

    requested_ids = {
        value
        for value in (normalized.primary_model_id, normalized.fallback_model_id)
        if value is not None
    }
    if requested_ids:
        available_ids = set(
            (
                await db.execute(
                    select(LLMModel.id).where(
                        LLMModel.id.in_(requested_ids),
                        or_(LLMModel.tenant_id == project.tenant_id, LLMModel.tenant_id.is_(None)),
                        LLMModel.enabled.is_(True),
                    )
                )
            ).scalars()
        )
        if available_ids != requested_ids:
            raise HTTPException(status_code=422, detail="Selected model is unavailable in this tenant")

    result = normalized.model_dump(mode="json", exclude_none=False)
    if "membership" in current:
        result["membership"] = current["membership"]
    return result


async def sync_project_capability_assignment(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
) -> None:
    """Keep ordinary Tool/MCP project bindings on the AgentTool boundary.

    Skill assets have their own lifecycle. Ordinary tool bindings target the
    inherited member; shared MCP dependencies target every active member. Both
    records are updated in the caller's transaction, so no parallel enablement
    catalog is introduced.
    """

    if binding.capability_type not in {"tool", "mcp"} or binding.capability_id is None:
        return

    tool_statement = select(Tool).where(
        or_(Tool.tenant_id == project.tenant_id, Tool.tenant_id.is_(None)),
    )
    if binding.capability_type == "tool":
        tool_statement = tool_statement.where(Tool.id == binding.capability_id)
    else:
        tool_statement = tool_statement.where(
            or_(Tool.id == binding.capability_id, Tool.mcp_server_id == binding.capability_id),
            Tool.type == "mcp",
        )
    tools = (await db.execute(tool_statement)).scalars().all()
    if not tools:
        raise HTTPException(status_code=422, detail="Tool capability is unavailable in this tenant")

    member_statement = (
        select(ProjectMemberSnapshot)
        .join(Agent, Agent.id == ProjectMemberSnapshot.agent_id)
        .where(
            ProjectMemberSnapshot.project_id == project.id,
            ProjectMemberSnapshot.tenant_id == project.tenant_id,
            ProjectMemberSnapshot.is_enabled.is_(True),
            Agent.scope == "project",
            Agent.project_id == project.id,
        )
    )
    if binding.source == "inherited":
        if binding.inherited_from_agent_id is None:
            raise HTTPException(status_code=422, detail="Inherited tool capability requires a project member")
        member_statement = member_statement.where(
            ProjectMemberSnapshot.agent_id == binding.inherited_from_agent_id,
        )
    members = (await db.execute(member_statement)).scalars().all()
    if not members:
        raise HTTPException(status_code=422, detail="Tool capability requires an active project member")

    agent_ids = {member.agent_id for member in members}
    tool_ids = {tool.id for tool in tools}
    existing = (
        (
            await db.execute(
                select(AgentTool).where(
                    AgentTool.agent_id.in_(agent_ids),
                    AgentTool.tool_id.in_(tool_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    by_pair = {(row.agent_id, row.tool_id): row for row in existing}
    for agent_id in agent_ids:
        for tool_id in tool_ids:
            assignment = by_pair.get((agent_id, tool_id))
            if assignment is None:
                db.add(
                    AgentTool(
                        agent_id=agent_id,
                        tool_id=tool_id,
                        enabled=binding.is_enabled,
                        source="user_installed",
                    )
                )
            else:
                assignment.enabled = binding.is_enabled
    await db.flush()


async def clone_source_agent_tool_dependencies(
    db: AsyncSession,
    project: Project,
    *,
    source_agent_id: uuid.UUID,
    project_agent_id: uuid.UUID,
    project_defaults_only: bool = False,
) -> list[ProjectCapabilityBinding]:
    """Reference Tool/MCP dependencies for a project-owned copy.

    The ordinary clone path preserves the source Agent's enabled dependency
    identities. Product-created project copies use the normalized project
    starter set instead. Per-Agent configuration can contain credentials, so
    the new project Agent always starts with empty config.
    """

    source_rows = (
        await db.execute(
            select(AgentTool, Tool)
            .join(Tool, Tool.id == AgentTool.tool_id)
            .where(
                AgentTool.agent_id == source_agent_id,
                AgentTool.enabled.is_(True),
                Tool.enabled.is_(True),
                or_(Tool.tenant_id == project.tenant_id, Tool.tenant_id.is_(None)),
            )
        )
    ).all()
    source_rows = [
        (assignment, tool)
        for assignment, tool in source_rows
        if tool.type != "mcp" or tool.mcp_server_id is not None
    ]
    if project_defaults_only:
        default_tools = (
            await db.execute(
                select(Tool).where(
                    Tool.name.in_(PROJECT_AGENT_DEFAULT_TOOL_NAMES),
                    Tool.enabled.is_(True),
                    or_(Tool.tenant_id == project.tenant_id, Tool.tenant_id.is_(None)),
                )
            )
        ).scalars().all()
        source_rows = [(None, tool) for tool in default_tools]
    if not source_rows:
        return []

    dependencies = {
        ("mcp", tool.mcp_server_id) if tool.type == "mcp" else ("tool", tool.id)
        for _, tool in source_rows
    }
    dependency_ids = {capability_id for _, capability_id in dependencies if capability_id is not None}
    mcp_server_ids = {
        capability_id
        for capability_type, capability_id in dependencies
        if capability_type == "mcp" and capability_id is not None
    }
    mcp_names = {
        server.id: (server.display_name or server.name)
        for server in (
            (
                await db.execute(
                    select(MCPServer).where(
                        MCPServer.id.in_(mcp_server_ids),
                        or_(MCPServer.tenant_id == project.tenant_id, MCPServer.tenant_id.is_(None)),
                    )
                )
            )
            .scalars()
            .all()
            if mcp_server_ids
            else []
        )
    }

    existing_assignments = {
        row.tool_id: row
        for row in (
            (
                await db.execute(
                    select(AgentTool).where(
                        AgentTool.agent_id == project_agent_id,
                        AgentTool.tool_id.in_([tool.id for _, tool in source_rows]),
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    existing_bindings = {
        (row.capability_type, row.capability_id): row
        for row in (
            (
                await db.execute(
                    select(ProjectCapabilityBinding).where(
                        ProjectCapabilityBinding.project_id == project.id,
                        ProjectCapabilityBinding.tenant_id == project.tenant_id,
                        ProjectCapabilityBinding.source == "inherited",
                        ProjectCapabilityBinding.inherited_from_agent_id == project_agent_id,
                        ProjectCapabilityBinding.capability_id.in_(dependency_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
    }
    tool_by_dependency = {
        ("mcp", tool.mcp_server_id) if tool.type == "mcp" else ("tool", tool.id): tool
        for _, tool in source_rows
    }
    for _source_assignment, tool in source_rows:
        assignment = existing_assignments.get(tool.id)
        if assignment is None:
            db.add(
                AgentTool(
                    agent_id=project_agent_id,
                    tool_id=tool.id,
                    enabled=True,
                    config={},
                    source="user_installed",
                )
            )
        else:
            assignment.enabled = True

    bindings: list[ProjectCapabilityBinding] = []
    for capability_type, capability_id in sorted(dependencies, key=lambda item: (item[0], str(item[1]))):
        if capability_id is None:
            continue
        tool = tool_by_dependency[(capability_type, capability_id)]
        binding = existing_bindings.get((capability_type, capability_id))
        if binding is None:
            binding = ProjectCapabilityBinding(
                tenant_id=project.tenant_id,
                project_id=project.id,
                capability_type=capability_type,
                capability_id=capability_id,
                capability_name=(
                    mcp_names.get(capability_id, tool.mcp_server_name or tool.display_name or tool.name)
                    if capability_type == "mcp"
                    else tool.display_name or tool.name
                ),
                source="inherited",
                inherited_from_agent_id=project_agent_id,
                is_enabled=True,
                scope={},
                config={},
            )
            db.add(binding)
        else:
            binding.is_enabled = True
        bindings.append(binding)
    await db.flush()
    return bindings
