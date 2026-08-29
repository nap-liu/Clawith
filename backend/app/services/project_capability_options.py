"""Canonical project capability choices and authorization boundaries."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.mcp_server import MCPServer
from app.models.skill import Skill, SkillInstall
from app.models.tool import AgentTool, Tool


@dataclass(frozen=True)
class ProjectCapabilityOptions:
    capabilities: list[dict[str, Any]]
    shared_mcp_ids: frozenset[uuid.UUID]
    shared_tool_ids: frozenset[uuid.UUID]
    market_skill_ids: frozenset[uuid.UUID]
    agent_mcp_ids: dict[uuid.UUID, frozenset[uuid.UUID]]
    agent_tool_ids: dict[uuid.UUID, frozenset[uuid.UUID]]
    agent_skill_ids: dict[uuid.UUID, frozenset[uuid.UUID]]

    def allows(self, agent_id: uuid.UUID, kind: str, capability_id: uuid.UUID) -> bool:
        if kind == "mcp":
            return capability_id in self.shared_mcp_ids or capability_id in self.agent_mcp_ids.get(
                agent_id, frozenset()
            )
        if kind == "skill":
            return capability_id in self.market_skill_ids or capability_id in self.agent_skill_ids.get(
                agent_id, frozenset()
            )
        return False

    def allows_shared(self, kind: str, capability_id: uuid.UUID) -> bool:
        if kind == "mcp":
            return capability_id in self.shared_mcp_ids
        if kind == "skill":
            return capability_id in self.market_skill_ids
        return False

    def allows_tool(self, agent_id: uuid.UUID, tool_id: uuid.UUID) -> bool:
        return tool_id in self.shared_tool_ids or tool_id in self.agent_tool_ids.get(
            agent_id, frozenset()
        )


def _display_name(value: str | None) -> str | None:
    name = (value or "").strip()
    if not name or not any(character.isalnum() for character in name):
        return None
    tokens = re.findall(r"[0-9A-Za-z]+", name)
    if (
        tokens
        and sum(len(token) for token in tokens) >= 6
        and all(re.fullmatch(r"[0-9a-fA-F]+", token) for token in tokens)
    ):
        return None
    compact = re.sub(r"[^0-9a-fA-F]", "", name)
    if len(compact) >= 24 and len(compact) == len(re.sub(r"[\s_-]", "", name)):
        return None
    return name


def _mcp_display_name(value: str | None) -> str | None:
    """Return only MCP names that can identify a service to an end user.

    Historical imports and local connection probes can leave server rows named
    with one-letter aliases, short hashes, or implementation placeholders such
    as ``srv``.  They are not actionable choices in project configuration and
    must not become visible groups in the shared tool catalog.
    """

    name = _display_name(value)
    if name is None:
        return None
    compact = re.sub(r"[^0-9A-Za-z]", "", name)
    lowered = name.casefold()
    if len(compact) < 4:
        return None
    if lowered in {"legacy", "mcpserver", "server", "testserver"}:
        return None
    if re.fullmatch(r"legacy[-_. ]*[0-9a-f]+(?:[-_. ][0-9a-f]+)*", lowered):
        return None
    return name


async def load_project_capability_options(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agents: Iterable[Agent],
) -> ProjectCapabilityOptions:
    """Return the only MCP and Skill assets a project may select.

    The same result drives bootstrap rendering and write authorization so the
    client cannot submit an asset that was outside its selectable boundary.
    """

    agent_list = list(agents)
    agent_ids = [agent.id for agent in agent_list]
    agent_names = {agent.id: agent.name for agent in agent_list}

    def tenant_scope(column):
        return or_(column == tenant_id, column.is_(None))

    shared_tool_ids = frozenset(
        (
            await db.execute(
                select(Tool.id).where(
                    Tool.enabled.is_(True),
                    Tool.source.in_(("builtin", "admin")),
                    tenant_scope(Tool.tenant_id),
                )
            )
        ).scalars()
    )

    mcp_servers = list(
        (
            await db.execute(
                select(MCPServer)
                .where(tenant_scope(MCPServer.tenant_id))
                .order_by(MCPServer.display_name)
            )
        ).scalars()
    )
    mcp_server_by_id = {server.id: server for server in mcp_servers}
    mcp_server_ids = list(mcp_server_by_id)
    mcp_tool_counts: dict[uuid.UUID, int] = {}
    private_mcp_server_ids: set[uuid.UUID] = set()
    if mcp_server_ids:
        private_mcp_server_ids = set(
            (
                await db.execute(
                    select(Tool.mcp_server_id).where(
                        Tool.type == "mcp",
                        Tool.source == "agent",
                        Tool.mcp_server_id.in_(mcp_server_ids),
                        tenant_scope(Tool.tenant_id),
                    )
                )
            ).scalars()
        )
        tool_rows = (
            await db.execute(
                select(Tool.mcp_server_id, func.count(Tool.id))
                .where(
                    Tool.type == "mcp",
                    Tool.enabled.is_(True),
                    Tool.mcp_server_id.in_(mcp_server_ids),
                    tenant_scope(Tool.tenant_id),
                )
                .group_by(Tool.mcp_server_id)
            )
        ).all()
        for server_id, count in tool_rows:
            if server_id is None:
                continue
            mcp_tool_counts[server_id] = mcp_tool_counts.get(server_id, 0) + int(count)

    company_servers = [
        server
        for server in mcp_servers
        if server.id not in private_mcp_server_ids
        and mcp_tool_counts.get(server.id, 0) > 0
        and _mcp_display_name(server.display_name)
    ]
    shared_mcp_ids = frozenset(server.id for server in company_servers)

    agent_mcp_ids: dict[uuid.UUID, set[uuid.UUID]] = {agent_id: set() for agent_id in agent_ids}
    agent_tool_ids: dict[uuid.UUID, set[uuid.UUID]] = {agent_id: set() for agent_id in agent_ids}
    if agent_ids:
        assignments = (
            await db.execute(
                select(AgentTool.agent_id, Tool.mcp_server_id, Tool.id)
                .join(Tool, Tool.id == AgentTool.tool_id)
                .where(
                    AgentTool.agent_id.in_(agent_ids),
                    AgentTool.enabled.is_(True),
                    Tool.enabled.is_(True),
                    Tool.type == "mcp",
                    Tool.source == "agent",
                    Tool.mcp_server_id.is_not(None),
                    tenant_scope(Tool.tenant_id),
                )
                .distinct()
            )
        ).all()
        for agent_id, server_id, tool_id in assignments:
            server = mcp_server_by_id.get(server_id)
            if server is not None and _mcp_display_name(server.display_name):
                agent_mcp_ids[agent_id].add(server_id)
                agent_tool_ids[agent_id].add(tool_id)

    public_skills = list(
        (
            await db.execute(
                select(Skill)
                .where(
                    tenant_scope(Skill.tenant_id),
                    Skill.visibility == "public",
                    Skill.status == "published",
                )
                .order_by(Skill.name)
            )
        ).scalars()
    )
    public_skills = [skill for skill in public_skills if _display_name(skill.name)]
    market_skill_ids = frozenset(skill.id for skill in public_skills)

    agent_skills: dict[tuple[uuid.UUID, uuid.UUID], Skill] = {}
    if agent_ids:
        installed = (
            await db.execute(
                select(SkillInstall, Skill)
                .join(Skill, Skill.id == SkillInstall.skill_id)
                .where(
                    SkillInstall.agent_id.in_(agent_ids),
                    SkillInstall.tenant_id == tenant_id,
                    SkillInstall.is_active.is_(True),
                    tenant_scope(Skill.tenant_id),
                    Skill.visibility != "public",
                    Skill.status == "published",
                )
            )
        ).all()
        agent_skills.update(
            {
                (install.agent_id, skill.id): skill
                for install, skill in installed
                if _display_name(skill.name)
            }
        )
        published_by_agents = list(
            (
                await db.execute(
                    select(Skill).where(
                        Skill.publisher_agent_id.in_(agent_ids),
                        tenant_scope(Skill.tenant_id),
                        Skill.visibility != "public",
                        Skill.status == "published",
                    )
                )
            ).scalars()
        )
        agent_skills.update(
            {
                (skill.publisher_agent_id, skill.id): skill
                for skill in published_by_agents
                if skill.publisher_agent_id is not None and _display_name(skill.name)
            }
        )

    frozen_agent_mcp_ids = {
        agent_id: frozenset(server_ids) for agent_id, server_ids in agent_mcp_ids.items()
    }
    frozen_agent_tool_ids = {
        agent_id: frozenset(tool_ids) for agent_id, tool_ids in agent_tool_ids.items()
    }
    frozen_agent_skill_ids = {
        agent_id: frozenset(
            skill_id for owner_agent_id, skill_id in agent_skills if owner_agent_id == agent_id
        )
        for agent_id in agent_ids
    }

    capabilities: list[dict[str, Any]] = [
        {
            "id": str(skill.id),
            "capability_id": str(skill.id),
            "type": "skill",
            "name": skill.name,
            "description": skill.description,
            "source": "shared",
            "origin": "market",
            "owner_agent_id": None,
            "enabled": True,
            "enabled_by_default": False,
            "version": str(skill.version),
        }
        for skill in public_skills
    ]
    capabilities.extend(
        {
            "id": str(server.id),
            "capability_id": str(server.id),
            "type": "mcp",
            "name": server.display_name,
            "description": None,
            "source": "shared",
            "origin": "company",
            "owner_agent_id": None,
            "enabled": True,
            "enabled_by_default": False,
            "tool_count": mcp_tool_counts[server.id],
        }
        for server in company_servers
    )
    capabilities.extend(
        {
            "id": f"{agent_id}:{server_id}",
            "capability_id": str(server_id),
            "type": "mcp",
            "name": mcp_server_by_id[server_id].display_name,
            "description": None,
            "source": "inherited",
            "origin": "digital_employee",
            "owner_agent_id": str(agent_id),
            "owner_agent_name": agent_names.get(agent_id),
            "enabled": True,
            "enabled_by_default": False,
            "tool_count": mcp_tool_counts.get(server_id, 0),
        }
        for agent_id in agent_ids
        for server_id in sorted(frozen_agent_mcp_ids[agent_id], key=str)
    )
    capabilities.extend(
        {
            "id": f"{agent_id}:{skill_id}",
            "capability_id": str(skill_id),
            "type": "skill",
            "name": agent_skills[(agent_id, skill_id)].name,
            "description": agent_skills[(agent_id, skill_id)].description,
            "source": "inherited",
            "origin": "digital_employee",
            "owner_agent_id": str(agent_id),
            "owner_agent_name": agent_names.get(agent_id),
            "version": str(agent_skills[(agent_id, skill_id)].version),
            "enabled": True,
            "enabled_by_default": False,
        }
        for agent_id in agent_ids
        for skill_id in sorted(frozen_agent_skill_ids[agent_id], key=str)
    )
    return ProjectCapabilityOptions(
        capabilities=capabilities,
        shared_mcp_ids=shared_mcp_ids,
        shared_tool_ids=shared_tool_ids,
        market_skill_ids=market_skill_ids,
        agent_mcp_ids=frozen_agent_mcp_ids,
        agent_tool_ids=frozen_agent_tool_ids,
        agent_skill_ids=frozen_agent_skill_ids,
    )
