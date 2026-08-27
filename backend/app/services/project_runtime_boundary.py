"""Narrow project-runtime checks away from standard Agent execution paths."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def is_project_agent(agent: Any) -> bool:
    """Return whether an already-loaded Agent belongs to a project."""

    return getattr(agent, "scope", "standard") == "project"


async def project_agent_runtime_allows(db: AsyncSession, agent: Any) -> bool:
    """Check the project switch only for an actual project Agent.

    The standard Agent path deliberately returns before importing project
    services or issuing a query against project-owned tables.
    """

    if not is_project_agent(agent):
        return True

    from app.services.project_service import project_runtime_allows_agent

    return await project_runtime_allows_agent(db, agent)


async def lock_and_check_project_agent_runtime(db: AsyncSession, agent: Any) -> bool:
    """Serialize project admission with pause/resume for project Agents only."""

    if not is_project_agent(agent):
        return True

    from app.models.project import Project

    await db.scalar(select(Project.id).where(Project.id == agent.project_id).with_for_update())
    return await project_agent_runtime_allows(db, agent)
