"""Catalog management shared by all administration surfaces."""

import uuid
import hashlib
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.orm import selectinload

from app.models.skill import Skill, SkillFile, SkillInstall, SkillTenantPolicy, SkillUpdateJob
from app.models.system_settings import SystemSetting
from app.services.skill_market import lock_skill_folder, serialize_market_skill, validate_skill_files
from app.services.skill_policy import can_manage_skill, is_platform_admin, managed_catalog_scope, require_skill_manager


def skill_capabilities(skill, actor):
    manage = can_manage_skill(skill, actor)
    return {
        "edit": manage,
        "default": manage and is_platform_admin(actor) and skill.tenant_id is None,
        "hide": (actor.role == "org_admin" or is_platform_admin(actor)) and bool(actor.tenant_id) and skill.tenant_id is None,
        "copy": (actor.role == "org_admin" or is_platform_admin(actor)) and bool(actor.tenant_id),
        "update_installs": manage,
    }


async def management_detail(db, skill, actor):
    data = serialize_market_skill(skill)
    data["is_default"] = skill.is_default
    data["capabilities"] = skill_capabilities(skill, actor)
    policy = await db.get(SkillTenantPolicy, (actor.tenant_id, skill.id)) if actor.tenant_id else None
    data["hidden"] = bool(policy and policy.hidden)
    data["active_installs"] = await db.scalar(select(func.count()).select_from(SkillInstall).where(
        SkillInstall.skill_id == skill.id, SkillInstall.is_active.is_(True)
    ))
    if can_manage_skill(skill, actor):
        latest = await db.scalar(select(SkillUpdateJob.id).where(SkillUpdateJob.skill_id == skill.id)
                                 .order_by(SkillUpdateJob.created_at.desc()).limit(1))
        data["latest_update_id"] = str(latest) if latest else None
    return data


async def list_managed_skills(db, actor):
    counts = select(func.count(SkillInstall.id)).where(
        SkillInstall.skill_id == Skill.id, SkillInstall.is_active.is_(True)
    ).correlate(Skill).scalar_subquery()
    latest = select(SkillUpdateJob.id).where(SkillUpdateJob.skill_id == Skill.id).order_by(
        SkillUpdateJob.created_at.desc()
    ).limit(1).correlate(Skill).scalar_subquery()
    rows = (await db.execute(select(Skill, SkillTenantPolicy.hidden, counts, latest).outerjoin(
        SkillTenantPolicy, and_(SkillTenantPolicy.skill_id == Skill.id,
                               SkillTenantPolicy.tenant_id == actor.tenant_id)
    ).where(managed_catalog_scope(actor)).order_by(Skill.updated_at.desc(), Skill.id))).all()
    return [{**serialize_market_skill(skill), "hidden": bool(hidden), "active_installs": count,
             "capabilities": skill_capabilities(skill, actor),
             "latest_update_id": str(job) if job and can_manage_skill(skill, actor) else None}
            for skill, hidden, count, job in rows]


async def get_managed_skill(db, skill_id, actor):
    skill = await db.scalar(select(Skill).where(
        Skill.id == skill_id, managed_catalog_scope(actor)
    ).options(selectinload(Skill.files)))
    if not skill:
        raise HTTPException(404, "Skill not found")
    return skill


async def edit_skill(db, skill_id, actor, changes):
    skill = await get_managed_skill(db, skill_id, actor)
    require_skill_manager(skill, actor)
    await lock_skill_folder(db, skill.folder_name)
    await db.refresh(skill)
    await db.refresh(skill, attribute_names=["files"])
    expected_version = changes.pop("expected_version", None)
    if expected_version is not None and expected_version != skill.version:
        raise HTTPException(409, "Skill changed; reload before saving")
    if "is_default" in changes and not skill_capabilities(skill, actor)["default"]:
        raise HTTPException(403, "Only platform administrators can configure default Skills")
    files = changes.pop("files", None)
    if changes.get("status") == "published" and files is None:
        validate_skill_files([{"path": item.path, "content": item.content} for item in skill.files])
    if files is not None:
        validate_skill_files(files)
        for item in list(skill.files):
            await db.delete(item)
        await db.flush()
        skill.files = [SkillFile(path=item["path"], content=item["content"]) for item in files]
    for field in ("name", "description", "category", "icon", "is_default", "status", "visibility"):
        if field in changes:
            setattr(skill, field, changes[field])
    skill.version += 1
    skill.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return await management_detail(db, skill, actor)


async def hide_skill(db, skill_id, actor, hidden):
    skill = await get_managed_skill(db, skill_id, actor)
    if not skill_capabilities(skill, actor)["hide"]:
        raise HTTPException(403, "Tenant administrator access required")
    policy = await db.get(SkillTenantPolicy, (actor.tenant_id, skill.id))
    if policy is None:
        policy = SkillTenantPolicy(tenant_id=actor.tenant_id, skill_id=skill.id)
        db.add(policy)
    policy.hidden = hidden
    await db.commit()
    return await management_detail(db, skill, actor)


async def copy_skill(db, skill_id, actor):
    source = await get_managed_skill(db, skill_id, actor)
    if not skill_capabilities(source, actor)["copy"]:
        raise HTTPException(403, "Tenant administrator access required")
    skill = Skill(
        tenant_id=actor.tenant_id, publisher_user_id=actor.id,
        name=source.name, description=source.description, category=source.category, icon=source.icon,
        folder_name=f"{source.folder_name[:80]}-{uuid.uuid4().hex[:12]}",
        is_builtin=False, is_default=False, status="draft", visibility="tenant", version=1,
        files=[SkillFile(path=item.path, content=item.content) for item in source.files],
    )
    db.add(skill)
    await db.commit()
    return await management_detail(db, skill, actor)


async def record_builtin_import(db, folder):
    # The historical 100-character setting key limit also applies to long folders.
    key = "skill_import:" + hashlib.sha256(folder.encode()).hexdigest()
    if await db.get(SystemSetting, key) is None:
        db.add(SystemSetting(key=key, value={"folder": folder}))
    return key
