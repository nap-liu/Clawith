"""Default assignment and conservative adoption of legacy bundled installations."""

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.agent import Agent
from app.models.skill import Skill, SkillInstall
from app.models.system_settings import SystemSetting
from app.services.skill_market import _snapshot_storage_tree
from app.services.skill_policy import tenant_skill_visible
from app.services.storage import get_storage_backend
from app.services.workspace_locking import workspace_locks


async def install_seed_agent_skills(db, agent, folders):
    skills = (await db.scalars(select(Skill).where(
        Skill.tenant_id.is_(None), Skill.status == "published", tenant_skill_visible(agent.tenant_id),
    ).options(selectinload(Skill.files)))).all()
    storage = get_storage_backend()
    for skill in skills:
        if not skill.is_default and skill.folder_name not in folders:
            continue
        for item in skill.files:
            await storage.write_text(f"{agent.id}/skills/{skill.folder_name}/{item.path}", item.content)
        db.add(SkillInstall(tenant_id=agent.tenant_id, agent_id=agent.id, skill_id=skill.id,
                            installed_version=skill.version))


async def adopt_legacy_builtin_installs(db):
    """Adopt only exact, unmodified copies. Never resurrect an uninstall record."""
    marker = "skill_install_provenance_initialized"
    if await db.get(SystemSetting, marker):
        return
    skills = (await db.scalars(select(Skill).where(Skill.is_builtin.is_(True)).options(
        selectinload(Skill.files)
    ))).all()
    agents = (await db.scalars(select(Agent).where(Agent.tenant_id.is_not(None), Agent.scope == "standard"))).all()
    for agent in agents:
        async with workspace_locks(agent.id, []):
            for skill in skills:
                existing = await db.scalar(select(SkillInstall.id).where(
                    SkillInstall.agent_id == agent.id, SkillInstall.skill_id == skill.id,
                ))
                if existing or not skill.files:
                    continue
                actual = await _snapshot_storage_tree(f"{agent.id}/skills/{skill.folder_name}")
                expected = {item.path: item.content.encode("utf-8") for item in skill.files}
                if actual == expected:
                    db.add(SkillInstall(tenant_id=agent.tenant_id, agent_id=agent.id, skill_id=skill.id,
                                       installed_version=skill.version))
    db.add(SystemSetting(key=marker, value={"complete": True}))
    await db.commit()
