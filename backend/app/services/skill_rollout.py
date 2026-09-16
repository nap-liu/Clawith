"""Explicit catalog rollouts using the existing background and workspace leases."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select, text

from app.database import async_session
from app.models.agent import Agent
from app.models.skill import Skill, SkillInstall, SkillUpdateJob
from app.services.redis_lease_lock import RedisLeaseBusyError, redis_lease_lock
from app.services.skill_management import get_managed_skill
from app.services.skill_market import _replace_storage_tree, _restore_storage_tree, _snapshot_storage_tree, lock_skill_folder, validate_skill_files
from app.services.skill_policy import require_skill_manager
from app.services.workspace_locking import workspace_locks


def rollout_summary(job):
    values = list(job.targets.values())
    return {
        "id": str(job.id), "version": job.version, "total": len(values),
        "succeeded": values.count("succeeded"), "failed": values.count("failed"),
        "pending": values.count("pending"), "skipped": values.count("skipped"),
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


async def create_rollout(db, skill_id, actor):
    skill = await get_managed_skill(db, skill_id, actor)
    require_skill_manager(skill, actor)
    await lock_skill_folder(db, skill.folder_name)
    await db.refresh(skill)
    await db.refresh(skill, attribute_names=["files"])
    validate_skill_files([{"path": item.path, "content": item.content} for item in skill.files])
    installs = (await db.scalars(select(SkillInstall).where(
        SkillInstall.skill_id == skill.id, SkillInstall.is_active.is_(True)
    ))).all()
    job = SkillUpdateJob(
        skill_id=skill.id, actor_id=actor.id, version=skill.version, folder_name=skill.folder_name,
        files=[{"path": item.path, "content": item.content} for item in skill.files],
        targets={str(item.agent_id): "pending" for item in installs},
    )
    db.add(job)
    await db.commit()
    return job


async def get_rollout(db, job_id, actor):
    job = await db.get(SkillUpdateJob, job_id)
    if not job:
        raise HTTPException(404, "Skill update not found")
    skill = await db.get(Skill, job.skill_id)
    if not skill:
        if job.actor_id != actor.id and actor.role != "platform_admin":
            raise HTTPException(403, "Skill management access required")
    else:
        require_skill_manager(skill, actor)
    return job


async def _update_target(job_id, agent_id):
    async with workspace_locks(agent_id, []):
        async with async_session() as db:
            job = await db.get(SkillUpdateJob, job_id)
            agent = await db.get(Agent, agent_id)
            skill = await db.get(Skill, job.skill_id)
            install = await db.scalar(select(SkillInstall).where(
                SkillInstall.skill_id == job.skill_id, SkillInstall.agent_id == agent_id,
            ))
            if not skill or not agent or agent.is_deleted or not install or not install.is_active:
                return "skipped"
            # Never let a delayed older rollout overwrite a newer installation.
            if install.installed_version > job.version:
                return "skipped"
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                             {"key": f"{agent_id}:{job.folder_name}"})
            prefix = f"{agent_id}/skills/{job.folder_name}"
            previous = await _snapshot_storage_tree(prefix)
            try:
                await _replace_storage_tree(prefix, job.files)
                install.installed_version = job.version
                await db.commit()
            except BaseException:
                await db.rollback()
                await _restore_storage_tree(prefix, previous)
                raise
            return "succeeded"


async def run_rollout(job_id, retry_failed=False):
    try:
        async with redis_lease_lock(str(job_id), namespace="skill-rollout", acquire_timeout_seconds=0.1):
            async with async_session() as db:
                job = await db.get(SkillUpdateJob, job_id)
                if not job:
                    return
                targets = dict(job.targets)
            for target, result in targets.items():
                if result != "pending" and not (retry_failed and result == "failed"):
                    continue
                try:
                    outcome = await _update_target(job_id, uuid.UUID(target))
                except Exception:
                    outcome = "failed"
                async with async_session() as db:
                    job = await db.get(SkillUpdateJob, job_id)
                    job.targets = {**job.targets, target: outcome}
                    await db.commit()
    except RedisLeaseBusyError:
        return
