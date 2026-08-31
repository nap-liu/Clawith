"""Shared external-registry and persistence helpers for the Skills API."""

import asyncio
import base64
import io
import os
import re
import zipfile
from pathlib import Path

import httpx
from fastapi import HTTPException
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.skill import Skill, SkillFile
from app.models.user import User

CLAWHUB_BASE = os.getenv("CLAWHUB_BASE", "https://clawhub.ai/api").rstrip("/")
CLAWHUB_MIRROR_BASE = os.getenv("CLAWHUB_MIRROR_BASE", "https://cn.clawhub-mirror.com/api").rstrip("/")
GITHUB_API = "https://api.github.com"

MAX_SKILL_SIZE = int(os.getenv("MAX_SKILL_SIZE", "512000"))  # bytes; default 500 KB, override via env


async def _get_tenant_setting(tenant_id: str | None, key: str) -> str:
    """Resolve a tenant setting value: tenant_settings DB > empty."""
    if tenant_id:
        try:
            from app.models.tenant_setting import TenantSetting
            import uuid as _uid
            async with async_session() as db:
                result = await db.execute(
                    select(TenantSetting).where(
                        TenantSetting.tenant_id == _uid.UUID(tenant_id),
                        TenantSetting.key == key,
                    )
                )
                setting = result.scalar_one_or_none()
                if setting and setting.value.get("token"):
                    return setting.value["token"]
        except Exception:
            pass
    return ""


async def _get_github_token(tenant_id: str | None = None) -> str:
    """Resolve GitHub token from tenant settings DB."""
    return await _get_tenant_setting(tenant_id, "github_token")


async def _get_clawhub_key(tenant_id: str | None = None) -> str:
    """Resolve ClawHub API key from tenant settings DB."""
    return await _get_tenant_setting(tenant_id, "clawhub_key")


def _clawhub_headers(api_key: str) -> dict:
    """Build request headers for ClawHub API calls."""
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


def _clawhub_headers_for_base(api_key: str, base_url: str) -> dict:
    """Only send the official ClawHub API key to official ClawHub endpoints."""
    if "clawhub-mirror.com" in base_url:
        return {}
    return _clawhub_headers(api_key)


def _candidate_clawhub_bases(preferred: str | None = None) -> list[str]:
    """Return ClawHub API bases in fallback order without duplicates."""
    bases = [preferred, CLAWHUB_BASE, CLAWHUB_MIRROR_BASE]
    result: list[str] = []
    for base in bases:
        if not base:
            continue
        normalized = base.rstrip("/")
        if normalized not in result:
            result.append(normalized)
    return result


def _clawhub_search_endpoint(base_url: str) -> str:
    """The China mirror serves search under /v1/search, while clawhub.ai keeps /search."""
    if "clawhub-mirror.com" in base_url:
        return f"{base_url}/v1/search"
    return f"{base_url}/search"


def _clawhub_skill_url(base_url: str, slug: str) -> str:
    return f"{base_url}/v1/skills/{slug}"


def _clawhub_download_url(base_url: str) -> str:
    return f"{base_url}/v1/download"


def _public_clawhub_url(base_url: str, slug: str) -> str:
    if "clawhub-mirror.com" in base_url:
        return f"https://cn.clawhub-mirror.com/skills/{slug}"
    return f"https://clawhub.ai/skills/{slug}"


def _extract_clawhub_zip_files(data: bytes) -> list[dict]:
    """Convert a ClawHub skill zip into [{"path", "content"}] records."""
    files: list[dict] = []
    total_size = 0

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise HTTPException(502, "ClawHub download did not return a valid skill archive") from exc

    entries = [info for info in archive.infolist() if not info.is_dir()]
    raw_paths = [Path(info.filename) for info in entries]
    strip_prefix = ""
    if raw_paths:
        first_parts = raw_paths[0].parts
        if len(first_parts) > 1:
            candidate = first_parts[0]
            has_root_skill = any(p.name.upper() == "SKILL.MD" and len(p.parts) == 1 for p in raw_paths)
            has_prefixed_skill = any(
                len(p.parts) > 1 and p.parts[0] == candidate and p.parts[-1].upper() == "SKILL.MD"
                for p in raw_paths
            )
            all_share_prefix = all(len(p.parts) > 1 and p.parts[0] == candidate for p in raw_paths)
            if not has_root_skill and has_prefixed_skill and all_share_prefix:
                strip_prefix = f"{candidate}/"

    for info in entries:
        rel = info.filename.lstrip("/")
        if strip_prefix and rel.startswith(strip_prefix):
            rel = rel[len(strip_prefix):]
        path = Path(rel)
        if not rel or path.is_absolute() or ".." in path.parts:
            continue

        total_size += info.file_size
        if total_size > MAX_SKILL_SIZE:
            raise HTTPException(413, f"Skill exceeds size limit ({MAX_SKILL_SIZE // 1024}KB)")

        content = archive.read(info).decode("utf-8", errors="replace")
        files.append({"path": rel, "content": content})

    if not any(f["path"].upper() == "SKILL.MD" for f in files):
        raise HTTPException(400, "No SKILL.md found in ClawHub archive — not a valid skill package")
    return files


async def _fetch_clawhub_json(
    path_builder,
    api_key: str = "",
    preferred_base: str | None = None,
    params: dict | None = None,
) -> tuple[dict, str]:
    """Fetch JSON from ClawHub, falling back to the mirror when available."""
    last_error = ""
    for base_url in _candidate_clawhub_bases(preferred_base):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    path_builder(base_url),
                    params=params,
                    headers=_clawhub_headers_for_base(api_key, base_url),
                )
            content_type = resp.headers.get("content-type", "")
            if resp.status_code == 404:
                last_error = f"ClawHub not found at {base_url}"
                continue
            if resp.status_code == 429:
                last_error = f"ClawHub rate limit exceeded at {base_url}"
                continue
            if resp.status_code == 200 and "json" in content_type:
                return resp.json(), base_url
            last_error = f"ClawHub API error from {base_url}: HTTP {resp.status_code}"
        except HTTPException:
            raise
        except Exception as exc:
            last_error = f"Failed to connect to ClawHub at {base_url}: {exc}"
    if "rate limit" in last_error:
        raise HTTPException(429, "ClawHub rate limit exceeded. Please wait a moment and try again.")
    raise HTTPException(502, last_error or "Failed to connect to ClawHub")


async def _fetch_clawhub_skill_meta(
    slug: str,
    api_key: str = "",
    preferred_base: str | None = None,
) -> tuple[dict, str]:
    return await _fetch_clawhub_json(
        lambda base_url: _clawhub_skill_url(base_url, slug),
        api_key=api_key,
        preferred_base=preferred_base,
    )


async def _fetch_clawhub_skill_archive(
    slug: str,
    api_key: str = "",
    preferred_base: str | None = None,
    version: str | None = None,
    tag: str | None = None,
) -> tuple[list[dict], str]:
    """Download a ClawHub skill archive from official API or mirror."""
    params = {"slug": slug}
    if version:
        params["version"] = version
    if tag:
        params["tag"] = tag

    last_error = ""
    for base_url in _candidate_clawhub_bases(preferred_base):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(
                    _clawhub_download_url(base_url),
                    params=params,
                    headers=_clawhub_headers_for_base(api_key, base_url),
                )
            content_type = resp.headers.get("content-type", "")
            if resp.status_code == 404:
                last_error = f"Skill '{slug}' not found on ClawHub at {base_url}"
                continue
            if resp.status_code == 429:
                last_error = f"ClawHub rate limit exceeded at {base_url}"
                continue
            if resp.status_code == 200 and ("zip" in content_type or resp.content.startswith(b"PK")):
                return _extract_clawhub_zip_files(resp.content), base_url
            last_error = f"ClawHub download failed from {base_url}: HTTP {resp.status_code}"
        except HTTPException:
            raise
        except Exception as exc:
            last_error = f"Failed to download ClawHub skill from {base_url}: {exc}"
    if "rate limit" in last_error:
        raise HTTPException(429, "ClawHub rate limit exceeded. Please wait a moment and try again.")
    raise HTTPException(502, last_error or f"Failed to download skill '{slug}' from ClawHub")
# ─── Helpers ──────────────────────────────────────────


def classify_portability(content: str) -> int:
    """Classify skill portability: 1=pure prompt, 2=CLI/API, 3=OpenClaw native."""
    openclaw_markers = [
        "bash pty:", "process action:", "Clawdbot", "exec tool",
        "openclaw.json", "imessage tool", "slack tool",
    ]
    cli_markers = [
        "requires:", "bins:", 'env:', "OPENAI_API_KEY", "GITHUB_TOKEN",
        "python3", "brew ", "pip install", "npm install", "curl ",
    ]
    lower = content.lower()
    for kw in openclaw_markers:
        if kw.lower() in lower:
            return 3
    for kw in cli_markers:
        if kw.lower() in lower:
            return 2
    return 1


def _parse_skill_md_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from SKILL.md content."""
    import yaml
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    try:
        return yaml.safe_load(match.group(1)) or {}
    except Exception:
        return {}


def _parse_github_url(url: str) -> dict | None:
    """Parse a GitHub URL into owner/repo/branch/path components."""
    # https://github.com/{owner}/{repo}/tree/{branch}/{path}
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*?)/?$", url
    )
    if m:
        return {"owner": m.group(1), "repo": m.group(2), "branch": m.group(3), "path": m.group(4)}
    # https://github.com/{owner}/{repo}/{path} (assume main branch)
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url
    )
    if m:
        return {"owner": m.group(1), "repo": m.group(2), "branch": "main", "path": ""}
    return None


def _apply_skill_scope(query, current_user: User):
    """Scope skill queries for tenant admins while leaving platform admins unrestricted."""
    from sqlalchemy import or_ as _or

    if current_user.role == "platform_admin" or not current_user.tenant_id:
        return query
    return query.where(_or(Skill.tenant_id.is_(None), Skill.tenant_id == current_user.tenant_id))


def _ensure_skill_write_access(skill: Skill, current_user: User):
    """Protect market-managed Skills; retain legacy draft CRUD permissions."""
    if getattr(skill, "status", "draft") != "draft":
        raise HTTPException(409, "Published market Skills must be managed through the Skill market")
    if current_user.role == "platform_admin":
        return
    if not current_user.tenant_id:
        raise HTTPException(403, "Cannot modify skills without a tenant")
    # Allow org_admin to manage: their own tenant skills OR builtin (preset) skills
    if skill.tenant_id is not None and skill.tenant_id != current_user.tenant_id:
        raise HTTPException(403, "Cannot modify other-tenant skills")


def _apply_skill_folder_scope(query, current_user: User):
    """Keep legacy folder paths unambiguous under tenant-scoped uniqueness."""
    from sqlalchemy import or_ as _or
    from sqlalchemy.exc import ArgumentError

    if current_user.tenant_id:
        try:
            scope = _or(Skill.tenant_id.is_(None), Skill.tenant_id == current_user.tenant_id)
        except (ArgumentError, TypeError):
            # A few unit tests replace ORM fields with minimal query doubles.
            return query
        return query.where(scope)
    return query.where(Skill.tenant_id.is_(None))


async def _refresh_skill_market_state(db, skill: Skill) -> None:
    """Refresh lock-protected fields; lightweight API fakes may omit refresh."""
    refresh = getattr(db, "refresh", None)
    if refresh:
        await refresh(skill, attribute_names=["status", "folder_name", "tenant_id"])


async def _fetch_github_directory(
    owner: str, repo: str, path: str, branch: str = "main",
    token: str = "",
) -> list[dict]:
    """Recursively fetch all files from a GitHub directory via API.
    Returns [{"path": relative_path, "content": text}].
    """
    _token = token
    files: list[dict] = []
    total_size = 0
    max_depth = 3  # Prevent runaway recursion
    headers = {"Authorization": f"Bearer {_token}"} if _token else {}

    async def _recurse(dir_path: str, rel_prefix: str, depth: int = 0):
        nonlocal total_size
        if depth > max_depth:
            return
        api_url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{dir_path}?ref={branch}"
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.get(api_url)
            if resp.status_code == 404:
                raise HTTPException(404, f"GitHub path not found: {dir_path}")
            if resp.status_code == 403:
                raise HTTPException(429, "GitHub API rate limit exceeded. Try again later.")
            if resp.status_code != 200:
                raise HTTPException(502, f"GitHub API error: {resp.status_code}")
            items = resp.json()

        if isinstance(items, dict):
            # Single file (not a directory)
            items = [items]

        # Early guard: if at top level, check that SKILL.md exists
        if depth == 0:
            has_skill_md = any(
                i["name"].upper() == "SKILL.MD" and i["type"] == "file"
                for i in items
            )
            dir_count = sum(1 for i in items if i["type"] == "dir")
            if not has_skill_md:
                if dir_count > 5:
                    raise HTTPException(
                        400, f"This directory contains {dir_count} subdirectories but no SKILL.md. "
                             "Please provide the URL to a specific skill directory."
                    )
                raise HTTPException(400, "No SKILL.md found at the root of this directory — not a valid skill package.")

        for item in items:
            name = item["name"]
            rel = f"{rel_prefix}{name}" if rel_prefix else name

            if item["type"] == "dir":
                await _recurse(item["path"], f"{rel}/", depth + 1)
            elif item["type"] == "file":
                size = item.get("size", 0)
                total_size += size
                if total_size > MAX_SKILL_SIZE:
                    raise HTTPException(413, f"Skill exceeds size limit ({MAX_SKILL_SIZE // 1024}KB)")
                # Download file content
                async with httpx.AsyncClient(timeout=30, headers=headers) as client:
                    dl_resp = await client.get(item["url"])
                    if dl_resp.status_code == 200:
                        data = dl_resp.json()
                        content = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
                        files.append({"path": rel, "content": content})

    try:
        await _recurse(path, "")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Failed to fetch files from GitHub: {e}")
    return files


async def _save_skill_to_db(
    folder_name: str, name: str, description: str,
    category: str, icon: str, files: list[dict],
    source_url: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    """Create a Skill + SkillFile records in the database."""
    import uuid as _uuid
    async with async_session() as db:
        from app.services.skill_market import lock_skill_folder

        await lock_skill_folder(db, folder_name)
        # Tenant folders may repeat across tenants, but cannot shadow a global
        # folder used by legacy path-based APIs.
        conflict_q = select(Skill).where(Skill.folder_name == folder_name)
        if tenant_id:
            from sqlalchemy import or_ as _or

            conflict_q = conflict_q.where(
                _or(Skill.tenant_id.is_(None), Skill.tenant_id == _uuid.UUID(tenant_id))
            )
        existing = await db.execute(conflict_q)
        if existing.scalar_one_or_none():
            raise HTTPException(
                409, f"A skill with folder name '{folder_name}' already exists. "
                     "Delete it first or use a different name."
            )

        skill = Skill(
            name=name,
            description=description,
            category=category,
            icon=icon,
            folder_name=folder_name,
            is_builtin=False,
            tenant_id=_uuid.UUID(tenant_id) if tenant_id else None,
        )
        db.add(skill)
        await db.flush()

        for f in files:
            # PostgreSQL text columns cannot store null bytes
            content = f["content"].replace("\x00", "") if f.get("content") else ""
            db.add(SkillFile(skill_id=skill.id, path=f["path"], content=content))

        await db.commit()
        return {"id": str(skill.id), "name": skill.name, "folder_name": skill.folder_name}
