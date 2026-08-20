"""First-party Skill market HTTP API."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import check_agent_access
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.skill_market import (
    delete_offline_market_skill,
    get_market_skill_detail,
    install_market_skill,
    list_market_skills,
    list_my_published_skills,
    publish_agent_skill,
    relist_market_skill,
    serialize_market_skill,
    take_skill_offline,
    uninstall_market_skill,
)

market_router = APIRouter(prefix="/skills", tags=["skill-market"])
agent_market_router = APIRouter(prefix="/agents/{agent_id}/skills", tags=["skill-market"])


class PublishAgentSkillIn(BaseModel):
    path: str = Field(min_length=1, max_length=220)
    name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    category: str = Field(default="general", max_length=50)
    visibility: str = Field(default="tenant", pattern="^(tenant|public)$")


class MarketSkillActionIn(BaseModel):
    skill_id: uuid.UUID


async def _managed_agent(
    db: AsyncSession,
    current_user: User,
    agent_id: uuid.UUID,
):
    agent, access_level = await check_agent_access(db, current_user, agent_id)
    if access_level != "manage":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent manage access required")
    return agent


@market_router.get("/market")
async def search_market(
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_market_skills(
        db,
        tenant_id=current_user.tenant_id,
        query=q,
        limit=limit,
    )


@market_router.get("/market/{skill_id}")
async def market_detail(
    skill_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await get_market_skill_detail(
        db,
        skill_id=skill_id,
        tenant_id=current_user.tenant_id,
        viewer_user_id=current_user.id,
    )


@market_router.delete("/market/{skill_id}")
async def delete_offline_skill(
    skill_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await delete_offline_market_skill(db, skill_id=skill_id, actor=current_user)


@market_router.get("/mine")
async def my_published_skills(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_my_published_skills(db, user=current_user)


@market_router.post("/{skill_id}/offline")
async def offline_skill(
    skill_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    skill = await take_skill_offline(db, skill_id=skill_id, actor=current_user)
    return serialize_market_skill(skill)


@market_router.post("/{skill_id}/relist")
async def relist_skill(
    skill_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    skill = await relist_market_skill(db, skill_id=skill_id, actor=current_user)
    return serialize_market_skill(skill)


@agent_market_router.post("/publish")
async def publish_from_agent(
    agent_id: uuid.UUID,
    body: PublishAgentSkillIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await _managed_agent(db, current_user, agent_id)
    skill = await publish_agent_skill(
        db,
        agent=agent,
        actor=current_user,
        path=body.path,
        name=body.name,
        description=body.description,
        category=body.category,
        visibility=body.visibility,
    )
    return serialize_market_skill(skill)


@agent_market_router.post("/install")
async def install_to_agent(
    agent_id: uuid.UUID,
    body: MarketSkillActionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await _managed_agent(db, current_user, agent_id)
    return await install_market_skill(
        db,
        agent=agent,
        skill_id=body.skill_id,
        actor_user_id=current_user.id,
    )


@agent_market_router.post("/uninstall")
async def uninstall_from_agent(
    agent_id: uuid.UUID,
    body: MarketSkillActionIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await _managed_agent(db, current_user, agent_id)
    return await uninstall_market_skill(db, agent=agent, skill_id=body.skill_id)
