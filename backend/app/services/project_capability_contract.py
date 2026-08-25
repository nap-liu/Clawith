"""One product-facing contract for project capability bindings."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp_server import MCPServer
from app.models.project import Project, ProjectCapabilityBinding
from app.models.skill import Skill
from app.models.tool import Tool
from app.services.project_skill_assets import observe_project_skill_binding


async def serialize_project_capability(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
) -> dict:
    """Return availability and neutral metadata without platform configuration."""

    metadata = {
        "key": None,
        "asset_id": None,
        "affected_member_count": 0,
        "description": "",
        "availability": "missing",
        "version": None,
        "file_count": 0,
        "size_bytes": 0,
    }
    if binding.capability_type == "skill":
        metadata = await observe_project_skill_binding(db, project, binding)
        if binding.capability_id is not None:
            skill = (
                await db.execute(
                    select(Skill).where(
                        Skill.id == binding.capability_id,
                        or_(Skill.tenant_id == project.tenant_id, Skill.tenant_id.is_(None)),
                    )
                )
            ).scalar_one_or_none()
            if skill is not None and skill.description:
                metadata["description"] = skill.description
    elif binding.capability_type == "tool":
        tool = await db.get(Tool, binding.capability_id) if binding.capability_id else None
        if tool is not None and tool.tenant_id not in {None, project.tenant_id}:
            metadata["availability"] = "restricted"
        elif tool is not None:
            metadata["key"] = tool.name
            metadata["availability"] = "available" if tool.enabled else "restricted"
            metadata["description"] = tool.description or ""
    elif binding.capability_type == "mcp":
        server = await db.get(MCPServer, binding.capability_id) if binding.capability_id else None
        if server is not None and server.tenant_id not in {None, project.tenant_id}:
            metadata["availability"] = "restricted"
        elif server is not None:
            metadata["key"] = server.name
            tools = list(
                (
                    await db.execute(
                        select(Tool).where(
                            Tool.mcp_server_id == server.id,
                            or_(Tool.tenant_id == project.tenant_id, Tool.tenant_id.is_(None)),
                        )
                    )
                ).scalars()
            )
            metadata["availability"] = (
                "available" if any(tool.enabled for tool in tools) else "restricted" if tools else "missing"
            )
            # MCP instructions are model-facing connection details, not
            # product copy. The member panel communicates the dependency by
            # its configured display name and availability state.
            metadata["description"] = ""

    public_config = dict(binding.config or {})
    if binding.capability_type == "skill":
        public_config.pop("skill_asset", None)
    return {
        "id": binding.id,
        "project_id": binding.project_id,
        "capability_type": binding.capability_type,
        "capability_id": binding.capability_id,
        "capability_name": binding.capability_name,
        "source": binding.source,
        "inherited_from_agent_id": binding.inherited_from_agent_id,
        "is_enabled": binding.is_enabled,
        "scope": binding.scope or {},
        "config": public_config,
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
        **metadata,
    }
