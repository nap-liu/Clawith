"""Import an uploaded text Skill package into the existing catalog."""

import io
import re
import stat
import zipfile
from pathlib import PurePosixPath

from fastapi import HTTPException
from sqlalchemy import or_, select

from app.api.skill_helpers import _parse_skill_md_frontmatter
from app.models.skill import Skill, SkillFile
from app.services.skill_management import management_detail
from app.services.skill_market import MAX_SKILL_FILES, MAX_SKILL_SIZE, lock_skill_folder, validate_skill_files
from app.services.skill_policy import is_platform_admin


def read_skill_upload(filename, data):
    if len(data) > MAX_SKILL_SIZE:
        raise HTTPException(413, "Skill package is too large")
    if filename.lower().endswith('.md'):
        raw = [("SKILL.md", data)]
    elif filename.lower().endswith('.zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = [entry for entry in archive.infolist() if not entry.is_dir()]
                if len(entries) > MAX_SKILL_FILES or sum(entry.file_size for entry in entries) > MAX_SKILL_SIZE:
                    raise HTTPException(413, "Skill package is too large")
                for entry in entries:
                    path = PurePosixPath(entry.filename.replace('\\', '/'))
                    if path.is_absolute() or '..' in path.parts or stat.S_ISLNK(entry.external_attr >> 16):
                        raise HTTPException(400, "Invalid Skill file path")
                raw = [(entry.filename.replace('\\', '/'), archive.read(entry)) for entry in entries]
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            raise HTTPException(400, "Invalid Skill ZIP package") from exc
        if raw and all('/' in path for path, _ in raw):
            prefixes = {path.split('/')[0] for path, _ in raw}
            if len(prefixes) == 1:
                raw = [(path.split('/', 1)[1], content) for path, content in raw]
    else:
        raise HTTPException(400, "Upload a ZIP package or SKILL.md")
    files = []
    seen = set()
    for path, content in raw:
        path = str(PurePosixPath(path))
        if path.lower() == 'skill.md':
            path = 'SKILL.md'
        if path in seen:
            raise HTTPException(400, "Duplicate Skill file path")
        seen.add(path)
        try:
            files.append({"path": path, "content": content.decode('utf-8-sig')})
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "Skill files must be UTF-8 text") from exc
    validate_skill_files(files)
    return files


async def upload_skill(db, actor, filename, data, folder, scope):
    platform = is_platform_admin(actor)
    if not platform and actor.role != 'org_admin':
        raise HTTPException(403, "Administrator access required")
    if scope not in {'tenant', 'platform'} or (scope == 'platform' and not platform):
        raise HTTPException(403, "Platform administrator access required")
    tenant_id = None if scope == 'platform' else actor.tenant_id
    if scope == 'tenant' and not tenant_id:
        raise HTTPException(400, "Select a tenant")
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}', folder):
        raise HTTPException(400, "Invalid Skill folder name")
    files = read_skill_upload(filename, data)
    await lock_skill_folder(db, folder)
    conflict = select(Skill.id).where(Skill.folder_name == folder)
    if tenant_id:
        conflict = conflict.where(or_(Skill.tenant_id.is_(None), Skill.tenant_id == tenant_id))
    if await db.scalar(conflict):
        raise HTTPException(409, "A Skill with this folder name already exists")
    metadata = _parse_skill_md_frontmatter(next(item['content'] for item in files if item['path'] == 'SKILL.md'))
    skill = Skill(
        tenant_id=tenant_id, publisher_user_id=actor.id, folder_name=folder,
        name=str(metadata.get('name') or folder)[:100], description=str(metadata.get('description') or ''),
        status='draft', visibility='public' if tenant_id is None else 'tenant',
        is_builtin=False, is_default=False,
        files=[SkillFile(**item) for item in files],
    )
    db.add(skill)
    await db.commit()
    return await management_detail(db, skill, actor)
