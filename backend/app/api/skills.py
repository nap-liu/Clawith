"""Skills API — global skill registry CRUD."""

import asyncio
import base64
import io
import os
import re
import zipfile
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.skill import Skill, SkillFile
from app.core.security import get_current_admin, get_current_user, require_role
from app.models.user import User
from app.services.skill_policy import tenant_skill_visible
from loguru import logger

router = APIRouter(prefix="/skills", tags=["skills"])
from app.api.skill_helpers import (
    CLAWHUB_BASE,
    CLAWHUB_MIRROR_BASE,
    GITHUB_API,
    MAX_SKILL_SIZE,
    _get_tenant_setting,
    _get_github_token,
    _get_clawhub_key,
    _clawhub_headers,
    _clawhub_headers_for_base,
    _candidate_clawhub_bases,
    _clawhub_search_endpoint,
    _clawhub_skill_url,
    _clawhub_download_url,
    _public_clawhub_url,
    _extract_clawhub_zip_files,
    _fetch_clawhub_json,
    _fetch_clawhub_skill_meta,
    _fetch_clawhub_skill_archive,
    classify_portability,
    _parse_skill_md_frontmatter,
    _parse_github_url,
    _apply_skill_scope,
    _ensure_skill_write_access,
    _apply_skill_folder_scope,
    _refresh_skill_market_state,
    _fetch_github_directory,
    _save_skill_to_db,
)

class SkillFileIn(BaseModel):
    path: str
    content: str


class SkillCreateIn(BaseModel):
    name: str
    description: str = ""
    category: str = "custom"
    icon: str = "📋"
    folder_name: str
    files: list[SkillFileIn] = []


class ClawhubInstallIn(BaseModel):
    slug: str


class UrlImportIn(BaseModel):
    url: str

# ─── ClawHub Integration ─────────────────────────────


@router.get("/clawhub/search")
async def search_clawhub(q: str, current_user: User = Depends(get_current_user)):
    """Proxy search requests to the ClawHub API."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    api_key = await _get_clawhub_key(tenant_id)
    data, _ = await _fetch_clawhub_json(
        _clawhub_search_endpoint,
        api_key=api_key,
        params={"q": q},
    )
    results = data.get("results", [])
    return [
        {
            "slug": r.get("slug"),
            "displayName": r.get("displayName"),
            "summary": r.get("summary"),
            "score": r.get("score"),
            "version": r.get("version"),
            "updatedAt": r.get("updatedAt"),
        }
        for r in results
    ]


@router.get("/clawhub/detail/{slug}")
async def clawhub_detail(slug: str, current_user: User = Depends(get_current_user)):
    """Fetch full metadata for a skill from ClawHub."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    api_key = await _get_clawhub_key(tenant_id)
    try:
        data, _ = await _fetch_clawhub_skill_meta(slug, api_key=api_key)
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Failed to connect to ClawHub: {e}")


@router.post("/clawhub/install")
async def install_from_clawhub(body: ClawhubInstallIn, current_user: User = Depends(get_current_user)):
    """Install a skill from ClawHub into the global registry."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    slug = body.slug

    # 1. Fetch metadata from ClawHub (with retry for rate limits)
    api_key = await _get_clawhub_key(tenant_id)
    meta = None
    meta_base = None
    for attempt in range(3):
        try:
            meta, meta_base = await _fetch_clawhub_skill_meta(slug, api_key=api_key)
            break
        except HTTPException:
            raise
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(1)
                continue
            raise HTTPException(502, f"Failed to connect to ClawHub: {e}")

    skill_info = meta.get("skill", {})
    owner_info = meta.get("owner", {})
    moderation = meta.get("moderation") or {}

    handle = owner_info.get("handle", "").lower()
    if not handle:
        raise HTTPException(400, "Could not determine skill owner handle from ClawHub")

    # 2. Build result with moderation warning
    is_suspicious = moderation.get("isSuspicious", False)
    moderation_summary = moderation.get("summary", "")

    # 3. Fetch files from the ClawHub archive
    files, archive_base = await _fetch_clawhub_skill_archive(slug, api_key=api_key, preferred_base=meta_base)

    if not files:
        raise HTTPException(404, "No files found in the ClawHub skill archive")

    # 4. Extract name/description from SKILL.md
    skill_md = next((f for f in files if f["path"].upper() == "SKILL.MD"), None)
    if not skill_md:
        raise HTTPException(400, "No SKILL.md found — not a valid skill package")

    frontmatter = _parse_skill_md_frontmatter(skill_md["content"])
    name = frontmatter.get("name", skill_info.get("displayName", slug))
    description = frontmatter.get("description", skill_info.get("summary", ""))

    # 5. Classify portability tier
    tier = classify_portability(skill_md["content"])
    tier_labels = {1: "clawhub-tier1", 2: "clawhub-tier2", 3: "clawhub-tier3"}
    has_scripts = any("/" in f["path"] for f in files if f["path"] != "SKILL.md")

    # 6. Save to DB
    result = await _save_skill_to_db(
        folder_name=slug,
        name=name,
        description=description,
        category=tier_labels.get(tier, "clawhub"),
        icon="",
        files=files,
        source_url=_public_clawhub_url(archive_base, slug),
        tenant_id=tenant_id,
    )

    result["tier"] = tier
    result["is_suspicious"] = is_suspicious
    result["moderation_summary"] = moderation_summary
    result["has_scripts"] = has_scripts
    result["file_count"] = len(files)
    result["source"] = "clawhub"
    result["archive_source"] = archive_base
    return result


@router.post("/import-from-url")
async def import_from_url(body: UrlImportIn, current_user: User = Depends(get_current_user)):
    """Import a skill from any GitHub URL into the global registry."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    token = await _get_github_token(tenant_id)
    parsed = _parse_github_url(body.url)
    if not parsed:
        raise HTTPException(400, "Invalid GitHub URL. Expected format: https://github.com/{owner}/{repo}/tree/{branch}/{path}")

    owner, repo, branch, path = parsed["owner"], parsed["repo"], parsed["branch"], parsed["path"]

    # Fetch files
    files = await _fetch_github_directory(owner, repo, path, branch, token=token)
    if not files:
        raise HTTPException(404, "No files found at the specified path")

    # Validate SKILL.md exists
    skill_md = next((f for f in files if f["path"].upper() == "SKILL.MD"), None)
    if not skill_md:
        raise HTTPException(400, "No SKILL.md found at this URL — not a valid skill package")

    frontmatter = _parse_skill_md_frontmatter(skill_md["content"])
    name = frontmatter.get("name", path.rstrip("/").split("/")[-1] if path else repo)
    description = frontmatter.get("description", "")

    # Derive folder_name from the last path segment
    folder_name = path.rstrip("/").split("/")[-1] if path else repo

    tier = classify_portability(skill_md["content"])
    tier_labels = {1: "url-import-tier1", 2: "url-import-tier2", 3: "url-import-tier3"}

    result = await _save_skill_to_db(
        folder_name=folder_name,
        name=name,
        description=description,
        category=tier_labels.get(tier, "url-import"),
        icon="",
        files=files,
        source_url=body.url,
        tenant_id=tenant_id,
    )

    result["tier"] = tier
    result["file_count"] = len(files)
    result["source"] = "url"
    return result


@router.post("/import-from-url/preview")
async def preview_url_import(body: UrlImportIn, current_user: User = Depends(get_current_user)):
    """Preview what will be imported from a GitHub URL without saving."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    token = await _get_github_token(tenant_id)
    parsed = _parse_github_url(body.url)
    if not parsed:
        raise HTTPException(400, "Invalid GitHub URL format")

    owner, repo, branch, path = parsed["owner"], parsed["repo"], parsed["branch"], parsed["path"]

    files = await _fetch_github_directory(owner, repo, path, branch, token=token)
    if not files:
        raise HTTPException(404, "No files found at the specified path")

    skill_md = next((f for f in files if f["path"].upper() == "SKILL.MD"), None)
    if not skill_md:
        raise HTTPException(400, "No SKILL.md found — not a valid skill package")

    frontmatter = _parse_skill_md_frontmatter(skill_md["content"])
    tier = classify_portability(skill_md["content"])

    return {
        "name": frontmatter.get("name", path.rstrip("/").split("/")[-1] if path else repo),
        "description": frontmatter.get("description", ""),
        "tier": tier,
        "files": [{"path": f["path"], "size": len(f["content"])} for f in files],
        "total_size": sum(len(f["content"]) for f in files),
        "has_scripts": any("/" in f["path"] for f in files if f["path"] != "SKILL.md"),
    }


# ─── Standard CRUD ────────────────────────────────────


@router.get("/")
async def list_skills(current_user: User = Depends(get_current_user)):
    """List global skills scoped by tenant (builtin + tenant-specific)."""
    import uuid as _uuid
    from sqlalchemy import or_ as _or
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    async with async_session() as db:
        query = select(Skill).where(_or(Skill.status == "draft", Skill.is_builtin.is_(True))).order_by(Skill.name)
        query = query.where(Skill.status != "offline", tenant_skill_visible(current_user.tenant_id))
        # Scope by tenant: show builtin (tenant_id is NULL) + tenant-specific skills
        if tenant_id:
            query = query.where(_or(Skill.tenant_id.is_(None), Skill.tenant_id == _uuid.UUID(tenant_id)))
        result = await db.execute(query)
        skills = result.scalars().all()
        return [
            {
                "id": str(s.id),
                "name": s.name,
                "description": s.description,
                "category": s.category,
                "icon": s.icon,
                "folder_name": s.folder_name,
                "is_builtin": s.is_builtin,
                "is_default": s.is_default,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in skills
        ]


@router.get("/{skill_id}")
async def get_skill(skill_id: str, current_user: User = Depends(get_current_user)):
    """Get a skill with its files."""
    async with async_session() as db:
        query = select(Skill).where(Skill.id == skill_id).options(selectinload(Skill.files))
        result = await db.execute(_apply_skill_scope(query, current_user))
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(404, "Skill not found")
        return {
            "id": str(skill.id),
            "name": skill.name,
            "description": skill.description,
            "category": skill.category,
            "icon": skill.icon,
            "folder_name": skill.folder_name,
            "is_builtin": skill.is_builtin,
            "files": [
                {"path": f.path, "content": f.content}
                for f in skill.files
            ],
        }


@router.post("/")
async def create_skill(body: SkillCreateIn, current_user: User = Depends(get_current_admin)):
    """Create a custom skill."""
    async with async_session() as db:
        from sqlalchemy import or_ as _or
        from app.services.skill_market import lock_skill_folder

        await lock_skill_folder(db, body.folder_name)
        conflict_q = select(Skill.id).where(Skill.folder_name == body.folder_name)
        if current_user.tenant_id:
            conflict_q = conflict_q.where(
                _or(Skill.tenant_id.is_(None), Skill.tenant_id == current_user.tenant_id)
            )
        if await db.scalar(conflict_q):
            raise HTTPException(409, f"A skill with folder name '{body.folder_name}' already exists")

        skill = Skill(
            name=body.name,
            description=body.description,
            category=body.category,
            icon=body.icon,
            folder_name=body.folder_name,
            is_builtin=False,
            tenant_id=current_user.tenant_id,
        )
        db.add(skill)
        await db.flush()

        if not body.files:
            # Auto-create a SKILL.md template
            db.add(SkillFile(
                skill_id=skill.id,
                path="SKILL.md",
                content=f"---\nname: {body.name}\ndescription: {body.description}\n---\n\n# {body.name}\n\n## Overview\n{body.description}\n",
            ))
        else:
            for f in body.files:
                db.add(SkillFile(skill_id=skill.id, path=f.path, content=f.content))

        await db.commit()
        return {"id": str(skill.id), "name": skill.name}


class SkillUpdateIn(BaseModel):
    name: str | None = None
    description: str | None = None
    category: str | None = None
    icon: str | None = None
    files: list[SkillFileIn] | None = None


@router.put("/{skill_id}")
async def update_skill(skill_id: str, body: SkillUpdateIn, current_user: User = Depends(get_current_admin)):
    """Update a skill's metadata and/or files."""
    async with async_session() as db:
        query = select(Skill).where(Skill.id == skill_id).options(selectinload(Skill.files))
        result = await db.execute(_apply_skill_scope(query, current_user))
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(404, "Skill not found")
        from app.services.skill_market import lock_skill_folder

        await lock_skill_folder(db, skill.folder_name)
        await _refresh_skill_market_state(db, skill)
        _ensure_skill_write_access(skill, current_user)

        if body.name is not None:
            skill.name = body.name
        if body.description is not None:
            skill.description = body.description
        if body.category is not None:
            skill.category = body.category
        if body.icon is not None:
            skill.icon = body.icon

        # Replace files if provided
        if body.files is not None:
            for f in skill.files:
                await db.delete(f)
            await db.flush()
            for f in body.files:
                db.add(SkillFile(skill_id=skill.id, path=f.path, content=f.content))

        await db.commit()
        return {"id": str(skill.id), "name": skill.name}


@router.delete("/{skill_id}")
async def delete_skill(skill_id: str, current_user: User = Depends(get_current_admin)):
    """Delete a skill (not builtin)."""
    async with async_session() as db:
        query = select(Skill).where(Skill.id == skill_id)
        result = await db.execute(_apply_skill_scope(query, current_user))
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(404, "Skill not found")
        from app.services.skill_market import lock_skill_folder

        await lock_skill_folder(db, skill.folder_name)
        await _refresh_skill_market_state(db, skill)
        _ensure_skill_write_access(skill, current_user)
        await db.delete(skill)
        await db.commit()
        return {"ok": True}


# ─── Tenant GitHub Token Settings ───────────────────────────


class SkillSettingsIn(BaseModel):
    github_token: str | None = None
    clawhub_key: str | None = None


async def _upsert_tenant_setting(tenant_id, key: str, value: str):
    """Helper to upsert a tenant setting."""
    from app.models.tenant_setting import TenantSetting
    async with async_session() as db:
        result = await db.execute(
            select(TenantSetting).where(
                TenantSetting.tenant_id == tenant_id,
                TenantSetting.key == key,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.value = {"token": value}
        else:
            db.add(TenantSetting(
                tenant_id=tenant_id,
                key=key,
                value={"token": value},
            ))
        await db.commit()


def _mask_token(token: str) -> str:
    if token and len(token) > 8:
        return f"{token[:4]}...{token[-4:]}"
    return "****" if token else ""


@router.get("/settings/token")
async def get_skill_token_status(
    current_user=Depends(require_role("org_admin", "platform_admin")),
):
    """Check if GitHub token and ClawHub key are configured for this tenant."""
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    gh_token = await _get_github_token(tenant_id)
    ch_key = await _get_clawhub_key(tenant_id)
    return {
        "configured": bool(gh_token),
        "source": "tenant" if tenant_id else "env",
        "masked": _mask_token(gh_token),
        "clawhub_configured": bool(ch_key),
        "clawhub_masked": _mask_token(ch_key),
    }


@router.put("/settings/token")
async def set_skill_token(
    body: SkillSettingsIn,
    current_user=Depends(require_role("org_admin", "platform_admin")),
):
    """Save GitHub token and/or ClawHub key for this tenant.

    Accessible by org_admin (to manage their own company's credentials) and
    platform_admin. require_role performs exact-match checks, so both roles
    must be listed explicitly.
    """
    if not current_user.tenant_id:
        raise HTTPException(400, "No tenant associated")

    if body.github_token is not None:
        await _upsert_tenant_setting(current_user.tenant_id, "github_token", body.github_token)
    if body.clawhub_key is not None:
        await _upsert_tenant_setting(current_user.tenant_id, "clawhub_key", body.clawhub_key)
    return {"ok": True}


# ─── Path-based browse endpoints for FileBrowser ───────────


@router.get("/browse/list")
async def browse_list(path: str = "", current_user: User = Depends(get_current_user)):
    """List skill folders (root) or files/subdirs within a skill folder."""
    import uuid as _uuid
    from sqlalchemy import or_ as _or
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    async with async_session() as db:
        if not path or path == "/":
            # Root: list all skill folders (scoped by tenant)
            query = select(Skill).order_by(Skill.name)
            if tenant_id:
                query = query.where(_or(Skill.tenant_id.is_(None), Skill.tenant_id == _uuid.UUID(tenant_id)))
            result = await db.execute(query)
            skills = result.scalars().all()
            return [
                {"name": s.folder_name, "path": s.folder_name, "is_dir": True, "size": 0}
                for s in skills
            ]

        # Inside a skill folder — resolve the skill and relative subpath
        clean = path.strip("/")
        folder = clean.split("/")[0]
        # Resolve skill folder scoped by tenant
        skill_q = select(Skill).where(Skill.folder_name == folder).options(selectinload(Skill.files))
        if tenant_id:
            skill_q = skill_q.where(_or(Skill.tenant_id.is_(None), Skill.tenant_id == _uuid.UUID(tenant_id)))
        else:
            skill_q = skill_q.where(Skill.tenant_id.is_(None))
        result = await db.execute(skill_q)
        skill = result.scalar_one_or_none()
        if not skill:
            return []

        # Calculate the relative prefix within the skill (empty = skill root)
        sub = clean[len(folder):].strip("/")  # e.g. "" or "scripts" or "scripts/sub"

        items = []
        seen_dirs: set[str] = set()
        for f in skill.files:
            if sub:
                # Only files that start with this sub prefix
                if not f.path.startswith(sub + "/"):
                    continue
                remainder = f.path[len(sub) + 1:]  # strip "scripts/" prefix
            else:
                remainder = f.path

            if "/" in remainder:
                # This file is in a subdirectory — show the directory
                dir_name = remainder.split("/")[0]
                if dir_name not in seen_dirs:
                    seen_dirs.add(dir_name)
                    dir_path = f"{folder}/{sub}/{dir_name}" if sub else f"{folder}/{dir_name}"
                    items.append({"name": dir_name, "path": dir_path, "is_dir": True, "size": 0})
            else:
                # Direct child file
                file_path = f"{folder}/{f.path}"
                items.append({"name": remainder, "path": file_path, "is_dir": False, "size": len(f.content.encode())})

        return items


@router.get("/browse/read")
async def browse_read(path: str, current_user: User = Depends(get_current_user)):
    """Read a file from a skill folder."""
    import uuid as _uuid
    from sqlalchemy import or_ as _or
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    parts = path.strip("/").split("/", 1)
    if len(parts) < 2:
        raise HTTPException(400, "Path must include folder and file")
    folder, file_path = parts
    async with async_session() as db:
        skill_q = select(Skill).where(Skill.folder_name == folder).options(selectinload(Skill.files))
        if tenant_id:
            skill_q = skill_q.where(_or(Skill.tenant_id.is_(None), Skill.tenant_id == _uuid.UUID(tenant_id)))
        else:
            skill_q = skill_q.where(Skill.tenant_id.is_(None))
        result = await db.execute(skill_q)
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(404, "Skill not found")
        for f in skill.files:
            if f.path == file_path:
                return {"content": f.content}
        raise HTTPException(404, "File not found")


class BrowseWriteIn(BaseModel):
    path: str
    content: str


@router.put("/browse/write")
async def browse_write(body: BrowseWriteIn, current_user: User = Depends(get_current_admin)):
    """Write a file in a skill folder. Creates the skill if the folder doesn't exist."""
    parts = body.path.strip("/").split("/", 1)
    if len(parts) < 2:
        raise HTTPException(400, "Path must include folder and file")
    folder, file_path = parts
    async with async_session() as db:
        from app.services.skill_market import lock_skill_folder

        await lock_skill_folder(db, folder)
        skill_q = select(Skill).where(Skill.folder_name == folder).options(selectinload(Skill.files))
        result = await db.execute(_apply_skill_folder_scope(skill_q, current_user))
        skill = result.scalar_one_or_none()
        created_new_skill = False
        if not skill:
            if not current_user.tenant_id and await db.scalar(
                select(Skill.id).where(Skill.folder_name == folder)
            ):
                raise HTTPException(409, f"A tenant Skill already uses folder '{folder}'")
            # Auto-create skill from folder name, scoped to tenant
            skill = Skill(
                name=folder.replace("-", " ").title(),
                description="",
                category="custom",
                icon="--",
                folder_name=folder,
                is_builtin=False,
                tenant_id=current_user.tenant_id,
            )
            db.add(skill)
            await db.flush()
            created_new_skill = True
        else:
            await _refresh_skill_market_state(db, skill)
            _ensure_skill_write_access(skill, current_user)

        # Upsert file
        existing = None
        if not created_new_skill:
            for f in skill.files:
                if f.path == file_path:
                    existing = f
                    break
        if existing:
            existing.content = body.content
        else:
            db.add(SkillFile(skill_id=skill.id, path=file_path, content=body.content))
        await db.commit()
        return {"ok": True}


@router.delete("/browse/delete")
async def browse_delete(path: str, current_user: User = Depends(get_current_admin)):
    """Delete a file or an entire skill folder."""
    parts = path.strip("/").split("/", 1)
    folder = parts[0]
    async with async_session() as db:
        from app.services.skill_market import lock_skill_folder

        await lock_skill_folder(db, folder)
        skill_q = select(Skill).where(Skill.folder_name == folder).options(selectinload(Skill.files))
        result = await db.execute(_apply_skill_folder_scope(skill_q, current_user))
        skill = result.scalar_one_or_none()
        if not skill:
            raise HTTPException(404, "Skill not found")
        await _refresh_skill_market_state(db, skill)
        _ensure_skill_write_access(skill, current_user)

        if len(parts) == 1:
            # Delete entire skill
            await db.delete(skill)
        else:
            # Delete specific file
            file_path = parts[1]
            for f in skill.files:
                if f.path == file_path:
                    await db.delete(f)
                    break
        await db.commit()
        return {"ok": True}
