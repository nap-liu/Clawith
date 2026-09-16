"""Unified Skill administration and explicit rollout endpoints."""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.skill_management import (
    copy_skill, edit_skill, get_managed_skill, hide_skill, list_managed_skills, management_detail,
)
from app.services.skill_rollout import create_rollout, get_rollout, rollout_summary, run_rollout
from app.services.skill_upload import upload_skill
from app.services.skill_market import MAX_SKILL_SIZE

router = APIRouter(prefix="/skills/manage", tags=["skill-management"])


class FileIn(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str


class SkillEditIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    category: str | None = Field(default=None, max_length=50)
    icon: str | None = Field(default=None, max_length=10)
    is_default: bool | None = None
    status: str | None = Field(default=None, pattern="^(draft|published|offline)$")
    visibility: str | None = Field(default=None, pattern="^(tenant|public)$")
    files: list[FileIn] | None = None
    expected_version: int | None = None


class HideIn(BaseModel):
    hidden: bool


@router.get("")
async def catalog(actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await list_managed_skills(db, actor)


@router.post("/upload")
async def upload(file: UploadFile = File(...), folder: str = Form(...), scope: str = Form("tenant"),
                 actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    data = await file.read(MAX_SKILL_SIZE + 1)
    return await upload_skill(db, actor, file.filename or '', data, folder, scope)


@router.get("/updates/{job_id}")
async def update_status(job_id: uuid.UUID, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return rollout_summary(await get_rollout(db, job_id, actor))


@router.post("/updates/{job_id}/retry")
async def retry_update(job_id: uuid.UUID, tasks: BackgroundTasks, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    job = await get_rollout(db, job_id, actor)
    tasks.add_task(run_rollout, job.id, True)
    return rollout_summary(job)


@router.get("/{skill_id}")
async def detail(skill_id: uuid.UUID, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    skill = await get_managed_skill(db, skill_id, actor)
    data = await management_detail(db, skill, actor)
    data["files"] = [{"path": item.path, "content": item.content} for item in skill.files]
    return data


@router.patch("/{skill_id}")
async def edit(skill_id: uuid.UUID, body: SkillEditIn, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await edit_skill(db, skill_id, actor, body.model_dump(exclude_none=True))


@router.put("/{skill_id}/hidden")
async def hide(skill_id: uuid.UUID, body: HideIn, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await hide_skill(db, skill_id, actor, body.hidden)


@router.post("/{skill_id}/copy")
async def copy(skill_id: uuid.UUID, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    return await copy_skill(db, skill_id, actor)


@router.post("/{skill_id}/updates")
async def update_all(skill_id: uuid.UUID, tasks: BackgroundTasks, actor: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    job = await create_rollout(db, skill_id, actor)
    tasks.add_task(run_rollout, job.id)
    return rollout_summary(job)
