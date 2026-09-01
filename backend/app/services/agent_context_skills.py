"""Skill index loading for agent context."""

import uuid
from pathlib import Path

from app.services.agent_runtime_workspace import current_agent_runtime_workspace
from app.services.storage import get_storage_backend


def _parse_skill_frontmatter(content: str, filename: str) -> tuple[str, str]:
    """Parse YAML frontmatter from a skill .md file.

    Returns (name, description).
    If no frontmatter, falls back to filename-based name and first-line description.
    """
    name = filename.replace("_", " ").replace("-", " ")
    description = ""

    stripped = content.strip()
    if stripped.startswith("---"):
        end = stripped.find("---", 3)
        if end != -1:
            frontmatter = stripped[3:end].strip()
            for line in frontmatter.split("\n"):
                line = line.strip()
                if line.lower().startswith("name:"):
                    val = line[5:].strip().strip('"').strip("'")
                    if val:
                        name = val
                elif line.lower().startswith("description:"):
                    val = line[12:].strip().strip('"').strip("'")
                    if val:
                        description = val[:200]
            if description:
                return name, description

    # Fallback: use first non-empty, non-heading line as description
    for line in stripped.split("\n"):
        line = line.strip()
        # Skip frontmatter delimiters and YAML lines
        if line in ("---",) or line.startswith("name:") or line.startswith("description:"):
            continue
        if line and not line.startswith("#"):
            description = line[:200]
            break
    if not description:
        lines = stripped.split("\n")
        if lines:
            description = lines[0].strip().lstrip("# ")[:200]

    return name, description


async def _load_skills_index(
    agent_id: uuid.UUID,
    *,
    allowed_names: set[str] | None = None,
) -> str:
    """Load skill index (name + description) from skills/ directory.

    Supports two formats:
    - Flat file:   skills/my-skill.md
    - Folder:      skills/my-skill/SKILL.md  (Claude-style, with optional scripts/, references/)

    Uses progressive disclosure: only name+description go into the system
    prompt. The model is instructed to call read_file to load full content
    when a skill is relevant.
    """
    skills: list[tuple[str, str, str]] = []  # (name, description, path_relative_to_skills)
    storage = get_storage_backend()
    skills_prefix = current_agent_runtime_workspace(agent_id).storage_key("skills")
    if await storage.exists(skills_prefix) and await storage.is_dir(skills_prefix):
        for entry in await storage.list_dir(skills_prefix):
            if entry.name.startswith("."):
                continue
            entry_key = entry.key

            # Case 1: Folder-based skill — skills/<folder>/SKILL.md
            if entry.is_dir:
                skill_md_key = f"{entry_key}/SKILL.md"
                if not await storage.exists(skill_md_key):
                    skill_md_key = f"{entry_key}/skill.md"
                if await storage.exists(skill_md_key):
                    try:
                        content = (await storage.read_text(skill_md_key, encoding="utf-8", errors="replace")).strip()
                        name, desc = _parse_skill_frontmatter(content, entry.name)
                        skills.append((name, desc, f"{entry.name}/SKILL.md"))
                    except Exception:
                        skills.append((entry.name, "", f"{entry.name}/SKILL.md"))

            # Case 2: Flat file — skills/<name>.md
            elif Path(entry.name).suffix == ".md" and not entry.is_dir:
                try:
                    content = (await storage.read_text(entry_key, encoding="utf-8", errors="replace")).strip()
                    name, desc = _parse_skill_frontmatter(content, Path(entry.name).stem)
                    skills.append((name, desc, entry.name))
                except Exception:
                    skills.append((Path(entry.name).stem, "", entry.name))

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    normalized_allowed = {name.casefold() for name in allowed_names} if allowed_names is not None else None
    for s in skills:
        if normalized_allowed is not None and s[0].casefold() not in normalized_allowed:
            continue
        if s[0] not in seen:
            seen.add(s[0])
            unique.append(s)

    if not unique:
        return ""

    # Build index table
    lines = [
        "You have the following skills available. Each skill defines specific instructions for a task domain.",
        "",
        "| Skill | Description | File |",
        "|-------|-------------|------|",
    ]
    for name, desc, rel_path in unique:
        lines.append(f"| {name} | {desc} | skills/{rel_path} |")

    lines.append("")
    lines.append("⚠️ SKILL USAGE RULES:")
    lines.append(
        "1. When a user request matches a skill, FIRST call `read_file` with the File path above to load the full instructions."
    )
    lines.append("2. Follow the loaded instructions to complete the task.")
    lines.append("3. Do NOT guess what the skill contains — always read it first.")
    lines.append(
        "4. Folder-based skills may contain auxiliary files (scripts/, references/, examples/). Use `list_files` on the skill folder to discover them."
    )

    return "\n".join(lines)
