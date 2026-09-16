"""Import bundled catalog entries once; administrator configuration owns them thereafter."""

import hashlib
from pathlib import Path

from sqlalchemy import select

from app.database import async_session
from app.models.skill import Skill, SkillFile
from app.models.system_settings import SystemSetting
from app.services.skill_creator_content import get_skill_creator_files
from app.services.skill_market import lock_skill_folder
from app.services.skill_install_defaults import adopt_legacy_builtin_installs
from app.services.skill_seeder_templates_core import BUILTIN_SKILLS_CORE
from app.services.skill_seeder_templates_extended import BUILTIN_SKILLS_EXTENDED

BUILTIN_SKILLS = [*BUILTIN_SKILLS_CORE, *BUILTIN_SKILLS_EXTENDED]


async def seed_skills():
    async with async_session() as db:
        initialized = await db.get(SystemSetting, "skill_catalog_initialized")
        for data in BUILTIN_SKILLS:
            folder = data["folder_name"]
            await lock_skill_folder(db, folder)
            key = "skill_import:" + hashlib.sha256(folder.encode()).hexdigest()
            if await db.get(SystemSetting, key):
                continue
            existing = await db.scalar(select(Skill).where(
                Skill.tenant_id.is_(None), Skill.folder_name == folder,
            ))
            if existing is None:
                conflict = await db.scalar(select(Skill.id).where(Skill.folder_name == folder))
                if conflict:
                    continue
                files = data["files"]
                if folder == "skill-creator" and not files:
                    files = get_skill_creator_files()
                elif folder == "content-research-writer" and not files:
                    path = Path(__file__).parent / "skill_creator_files/content_research_writer__SKILL.md"
                    files = [{"path": "SKILL.md", "content": path.read_text(encoding="utf-8")}]
                elif folder == "mcp-installer" and not files:
                    path = Path(__file__).parents[2] / "agent_template/skills/mcp-installer/SKILL.md"
                    files = [{"path": "SKILL.md", "content": path.read_text(encoding="utf-8")}]
                skill = Skill(
                    name=data["name"], description=data["description"], category=data["category"],
                    icon=data["icon"], folder_name=folder, is_builtin=True,
                    is_default=data.get("is_default", False) if initialized is None else False,
                    visibility="public", status="published",
                    files=[SkillFile(path=item["path"], content=item["content"]) for item in files],
                )
                db.add(skill)
            db.add(SystemSetting(key=key, value={"folder": folder}))
        if initialized is None:
            db.add(SystemSetting(key="skill_catalog_initialized", value={"initialized": True}))
        await db.flush()
        await adopt_legacy_builtin_installs(db)
        await db.commit()


async def push_default_skills_to_existing_agents():
    """Compatibility entrypoint: defaults now apply only when creating Agents."""
    return
