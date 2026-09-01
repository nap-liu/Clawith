"""Seed builtin skills into the global skill registry."""

from loguru import logger
from sqlalchemy import select
from app.database import async_session
from app.models.skill import Skill, SkillFile
from app.services.skill_seeder_templates_core import BUILTIN_SKILLS_CORE
from app.services.skill_seeder_templates_extended import BUILTIN_SKILLS_EXTENDED


BUILTIN_SKILLS = [*BUILTIN_SKILLS_CORE, *BUILTIN_SKILLS_EXTENDED]


async def seed_skills():
    """Insert builtin skills if they don't exist."""
    from app.services.skill_creator_content import get_skill_creator_files
    from pathlib import Path as _Path

    _files_dir = _Path(__file__).parent / "skill_creator_files"
    _template_skills_dir = _Path(__file__).parent.parent.parent / "agent_template" / "skills"

    # Populate skill-creator files at runtime
    for s in BUILTIN_SKILLS:
        if s["folder_name"] == "skill-creator" and not s["files"]:
            s["files"] = get_skill_creator_files()
        elif s["folder_name"] == "content-research-writer" and not s["files"]:
            # Load from downloaded file
            crw_file = _files_dir / "content_research_writer__SKILL.md"
            if crw_file.exists():
                s["files"] = [{"path": "SKILL.md", "content": crw_file.read_text(encoding="utf-8")}]
        elif s["folder_name"] == "mcp-installer" and not s["files"]:
            mcp_file = _template_skills_dir / "mcp-installer" / "SKILL.md"
            if mcp_file.exists():
                s["files"] = [{"path": "SKILL.md", "content": mcp_file.read_text(encoding="utf-8")}]
            else:
                logger.warning("[SkillSeeder] mcp-installer/SKILL.md not found in agent_template/skills/")

    async with async_session() as db:
        from app.services.skill_market import lock_skill_folder

        for skill_data in BUILTIN_SKILLS:
            await lock_skill_folder(db, skill_data["folder_name"])
            result = await db.execute(
                select(Skill).where(
                    Skill.tenant_id.is_(None),
                    Skill.folder_name == skill_data["folder_name"],
                )
            )
            existing = result.scalar_one_or_none()
            is_default = skill_data.get("is_default", False)
            if not existing:
                tenant_conflict = await db.scalar(
                    select(Skill.id).where(
                        Skill.tenant_id.is_not(None),
                        Skill.folder_name == skill_data["folder_name"],
                    )
                )
                if tenant_conflict:
                    logger.error(
                        f"[SkillSeeder] Cannot create global Skill {skill_data['folder_name']}: tenant folder exists"
                    )
                    continue
            if existing:
                # Update metadata
                existing.name = skill_data["name"]
                existing.description = skill_data["description"]
                existing.category = skill_data["category"]
                existing.icon = skill_data["icon"]
                existing.is_default = is_default
                existing.visibility = "public"
                existing.status = "published"
                # Sync files — add missing ones
                from sqlalchemy.orm import selectinload
                res2 = await db.execute(
                    select(Skill).where(Skill.id == existing.id).options(selectinload(Skill.files))
                )
                sk = res2.scalar_one()
                existing_paths = {f.path: f for f in sk.files}
                for f in skill_data["files"]:
                    if f["path"] in existing_paths:
                        # Update content if changed
                        existing_file = existing_paths[f["path"]]
                        if existing_file.content != f["content"]:
                            existing_file.content = f["content"]
                            logger.info(f"[SkillSeeder] Updated {f['path']} in {skill_data['name']}")
                    else:
                        db.add(SkillFile(skill_id=existing.id, path=f["path"], content=f["content"]))
                        logger.info(f"[SkillSeeder] Added file {f['path']} to {skill_data['name']}")
            else:
                skill = Skill(
                    name=skill_data["name"],
                    description=skill_data["description"],
                    category=skill_data["category"],
                    icon=skill_data["icon"],
                    folder_name=skill_data["folder_name"],
                    is_builtin=True,
                    is_default=is_default,
                    visibility="public",
                    status="published",
                )
                db.add(skill)
                await db.flush()
                for f in skill_data["files"]:
                    db.add(SkillFile(skill_id=skill.id, path=f["path"], content=f["content"]))
                logger.info(f"[SkillSeeder] Created skill: {skill_data['name']}")
        await db.commit()
        logger.info("[SkillSeeder] Skills seeded")


async def push_default_skills_to_existing_agents():
    """Deploy all is_default skills into the workspace of every existing agent that is missing them.
    
    Called at startup after seed_skills() so existing agents automatically receive new default skills
    like mcp-installer without requiring manual re-creation.
    """
    from app.models.agent import Agent
    from app.models.skill import Skill
    from app.models.system_settings import SystemSetting
    from sqlalchemy.orm import selectinload
    from app.services.agent_manager import agent_manager
    from app.services.storage import get_storage_backend
    import hashlib

    async with async_session() as db:
        # Load all is_default skills with their files
        default_skills_r = await db.execute(
            select(Skill).where(Skill.is_default == True).options(selectinload(Skill.files))
        )
        default_skills = default_skills_r.scalars().all()
        if not default_skills:
            return

        # Compute a hash of default skill folder names to detect newly added skills
        hasher = hashlib.sha256()
        for skill in sorted(default_skills, key=lambda s: s.folder_name):
            hasher.update(skill.folder_name.encode("utf-8"))
        current_hash = hasher.hexdigest()

        # Check if we already synced this version of default skills
        setting_r = await db.execute(
            select(SystemSetting).where(SystemSetting.key == "default_skills_sync_hash")
        )
        setting = setting_r.scalar_one_or_none()
        if setting and setting.value.get("hash") == current_hash:
            logger.info(f"[SkillSeeder] Default skills sync hash '{current_hash}' matches, skipping sync for existing agents")
            return

        # Load all agents
        agents_r = await db.execute(select(Agent))
        agents = agents_r.scalars().all()

        pushed = 0
        removed_legacy = 0
        storage = get_storage_backend()
        for agent in agents:
            agent_prefix = agent_manager._agent_storage_prefix(agent.id)
            legacy_key = f"{agent_prefix}/skills/MCP_INSTALLER.md"
            if await storage.is_file(legacy_key):
                try:
                    await storage.delete(legacy_key)
                    removed_legacy += 1
                except Exception as exc:
                    logger.warning(f"[SkillSeeder] Failed to remove legacy MCP_INSTALLER.md for agent {agent.id}: {exc}")
            for skill in default_skills:
                if not skill.files:
                    continue

                # Determine if the agent already has this skill by checking if its first file exists in storage
                first_file_key = f"{agent_prefix}/skills/{skill.folder_name}/{skill.files[0].path}"
                if await storage.is_file(first_file_key):
                    continue  # Skill already exists, do not update

                for sf in skill.files:
                    key = f"{agent_prefix}/skills/{skill.folder_name}/{sf.path}"
                    await storage.write_text(key, sf.content, encoding="utf-8")
                    pushed += 1
                logger.info(f"[SkillSeeder] Pushed new default skill '{skill.name}' to agent {agent.id}")

        # Save/update the sync hash in settings
        if setting:
            setting.value = {"hash": current_hash}
        else:
            db.add(SystemSetting(key="default_skills_sync_hash", value={"hash": current_hash}))
        await db.commit()

        if pushed or removed_legacy:
            logger.info(
                f"[SkillSeeder] Pushed {pushed} new skill files "
                f"to existing agents; removed {removed_legacy} legacy MCP installer files"
            )
        else:
            logger.info("[SkillSeeder] All existing agents already have all default skills")
