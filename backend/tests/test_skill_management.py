"""Observable catalog permissions, seed persistence and explicit rollouts."""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.database import async_session
from app.models.skill import Skill, SkillFile, SkillInstall, SkillUpdateJob
from app.services.skill_management import edit_skill, hide_skill
from app.services.skill_market import get_visible_market_skill, install_market_skill, delete_offline_market_skill, publish_agent_skill
from app.services.skill_rollout import create_rollout, run_rollout
from app.services.skill_seeder import seed_skills
from app.services.agent_manager import agent_manager
from app.services.skill_install_defaults import install_seed_agent_skills
from app.services import skill_rollout
from app.services.storage import get_storage_backend
from tests.test_skill_market import _create_tenant_team


@pytest.mark.asyncio(loop_scope="session")
async def test_builtin_management_scope_and_seed_persistence(monkeypatch):
    _, admin, _, _ = await _create_tenant_team("manage")
    _, tenant_admin, _, _ = await _create_tenant_team("tenant")
    admin.role = "platform_admin"
    tenant_admin.role = "org_admin"
    folder = f"managed-{uuid.uuid4().hex[:10]}"
    template = dict(name="Managed", description="Initial", category="general", icon="📋",
                    folder_name=folder, files=[{"path": "SKILL.md", "content": "# Initial"}])
    monkeypatch.setattr("app.services.skill_seeder.BUILTIN_SKILLS", [template])
    await seed_skills()
    async with async_session() as db:
        skill = await db.scalar(select(Skill).where(Skill.folder_name == folder))
        skill_id = skill.id
        with pytest.raises(HTTPException) as error:
            await edit_skill(db, skill_id, tenant_admin, {"name": "Forbidden"})
        assert error.value.status_code == 403
        await db.rollback()
        await hide_skill(db, skill_id, tenant_admin, True)
        with pytest.raises(HTTPException):
            await get_visible_market_skill(db, skill_id=skill_id, tenant_id=tenant_admin.tenant_id)
        await get_visible_market_skill(db, skill_id=skill_id, tenant_id=admin.tenant_id)
        await edit_skill(db, skill_id, admin, {
            "name": "Edited", "is_default": True, "status": "offline",
            "files": [{"path": "SKILL.md", "content": "# Edited"}],
        })
    await seed_skills()
    async with async_session() as db:
        skill = await db.get(Skill, skill_id)
        assert (skill.name, skill.status, skill.is_default) == ("Edited", "offline", True)
        assert await db.scalar(select(SkillFile.content).where(SkillFile.skill_id == skill_id)) == "# Edited"
        await delete_offline_market_skill(db, skill_id=skill_id, actor=admin)
        await db.commit()
    await seed_skills()
    async with async_session() as db:
        assert await db.get(Skill, skill_id) is None
        assert await db.scalar(select(Skill.id).where(Skill.folder_name == folder)) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_rollout_frozen_content_cross_tenant_retry_and_uninstall(monkeypatch):
    tenant, owner, guest, agent_a = await _create_tenant_team("rollout-a")
    _, _, _, agent_b = await _create_tenant_team("rollout-b")
    folder = f"rollout-{uuid.uuid4().hex[:10]}"
    async with async_session() as db:
        skill = Skill(tenant_id=tenant.id, publisher_user_id=owner.id, name="Rollout", folder_name=folder,
                      status="published", visibility="public", files=[SkillFile(path="SKILL.md", content="# Original")])
        db.add(skill)
        await db.commit()
        skill_id = skill.id
        await install_market_skill(db, agent=agent_a, skill_id=skill_id, actor_user_id=owner.id)
        await install_market_skill(db, agent=agent_b, skill_id=skill_id, actor_user_id=owner.id)
        await edit_skill(db, skill_id, owner, {"files": [{"path": "SKILL.md", "content": "# Updated"}]})
        with pytest.raises(HTTPException):
            await create_rollout(db, skill_id, guest)
        job = await create_rollout(db, skill_id, owner)
        job_id = job.id
        await edit_skill(db, skill_id, owner, {"files": [{"path": "SKILL.md", "content": "# Later"}]})
    storage = get_storage_backend()
    await storage.write_text(f"{agent_a.id}/skills/{folder}/local.md", "local edits")
    await storage.write_text(f"{agent_a.id}/workspace/keep.md", "preserve")
    original = skill_rollout._update_target

    async def fail_one(job_id, agent_id):
        if agent_id == agent_b.id:
            raise OSError("temporary storage failure")
        return await original(job_id, agent_id)

    monkeypatch.setattr(skill_rollout, "_update_target", fail_one)
    await run_rollout(job_id)
    async with async_session() as db:
        job = await db.get(SkillUpdateJob, job_id)
        assert job.targets == {str(agent_a.id): "succeeded", str(agent_b.id): "failed"}
    assert await storage.read_text(f"{agent_a.id}/skills/{folder}/SKILL.md") == "# Updated"
    assert not await storage.exists(f"{agent_a.id}/skills/{folder}/local.md")
    assert await storage.read_text(f"{agent_a.id}/workspace/keep.md") == "preserve"
    monkeypatch.setattr(skill_rollout, "_update_target", original)
    await run_rollout(job_id, True)
    assert await storage.read_text(f"{agent_b.id}/skills/{folder}/SKILL.md") == "# Updated"
    async with async_session() as db:
        job = await create_rollout(db, skill_id, owner)
        job_id = job.id
        install = await db.scalar(select(SkillInstall).where(SkillInstall.agent_id == agent_b.id))
        install.is_active = False
        await db.commit()
    await run_rollout(job_id)
    async with async_session() as db:
        job = await db.get(SkillUpdateJob, job_id)
        assert job.targets[str(agent_b.id)] == "skipped"


@pytest.mark.asyncio(loop_scope="session")
async def test_default_configuration_and_template_do_not_bypass_tenant_policy():
    tenant, owner, _, agent = await _create_tenant_team("defaults")
    owner.role = "org_admin"
    suffix = uuid.uuid4().hex[:10]
    async with async_session() as db:
        skills = [Skill(name=label, folder_name=f"{label}-{suffix}", is_default=True,
                        status="published", visibility="public",
                        files=[SkillFile(path="SKILL.md", content=f"# {label}")])
                  for label in ("allowed", "hidden", "offline")]
        skills[2].status = "offline"
        db.add_all(skills)
        await db.commit()
        await hide_skill(db, skills[1].id, owner, True)
        await agent_manager.initialize_agent_files(db, agent)
        storage = get_storage_backend()
        assert not await storage.exists(f"{agent.id}/skills/mcp-installer/SKILL.md")
        await install_seed_agent_skills(db, agent, [])
        await db.commit()
        installed = set((await db.scalars(select(SkillInstall.skill_id).where(SkillInstall.agent_id == agent.id))).all())
        assert skills[0].id in installed
        assert skills[1].id not in installed
        assert skills[2].id not in installed


@pytest.mark.asyncio(loop_scope="session")
async def test_republished_draft_cannot_be_overwritten_by_older_rollout():
    _, owner, _, source = await _create_tenant_team("republish-source")
    _, _, _, target = await _create_tenant_team("republish-target")
    folder = f"republish-{uuid.uuid4().hex[:10]}"
    storage = get_storage_backend()
    source_key = f"{source.id}/skills/{folder}/SKILL.md"
    target_key = f"{target.id}/skills/{folder}/SKILL.md"
    await storage.write_text(source_key, "# Original")
    async with async_session() as db:
        skill = await publish_agent_skill(
            db, agent=source, actor=owner, path=f"skills/{folder}",
            name="Republish", description="", category="general", visibility="public",
        )
        await db.commit()
        assert skill.version == 1
        skill_id = skill.id
        await install_market_skill(db, agent=target, skill_id=skill_id, actor_user_id=owner.id)
        await edit_skill(db, skill_id, owner, {"status": "draft"})
        old_job = await create_rollout(db, skill_id, owner)
        job_id, old_version = old_job.id, old_job.version
        await storage.write_text(source_key, "# New publication")
        skill = await publish_agent_skill(
            db, agent=source, actor=owner, path=f"skills/{folder}",
            name="Republish", description="", category="general", visibility="public",
        )
        await db.commit()
        new_version = skill.version
        await install_market_skill(db, agent=target, skill_id=skill_id, actor_user_id=owner.id)
    assert await storage.read_text(target_key) == "# New publication"
    await run_rollout(job_id)
    assert await storage.read_text(target_key) == "# New publication"
    assert new_version > old_version
    async with async_session() as db:
        job = await db.get(SkillUpdateJob, job_id)
        assert job.targets[str(target.id)] == "skipped"
        install = await db.scalar(select(SkillInstall).where(
            SkillInstall.skill_id == skill_id, SkillInstall.agent_id == target.id,
        ))
        assert install.installed_version == new_version
