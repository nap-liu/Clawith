"""Enterprise KB and external skill import implementations for file APIs."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException


def enterprise_kb_dir_impl(api: Any, tenant_id: str) -> Path:
    local_root = api.settings.STORAGE_LOCAL_ROOT or api.settings.AGENT_DATA_DIR
    return Path(local_root) / f"enterprise_info_{tenant_id}" / "knowledge_base"


def enterprise_info_dir_impl(api: Any, tenant_id: str) -> Path:
    local_root = api.settings.STORAGE_LOCAL_ROOT or api.settings.AGENT_DATA_DIR
    return Path(local_root) / f"enterprise_info_{tenant_id}"


def enterprise_storage_key_impl(api: Any, tenant_id: str, rel_path: str = "") -> str:
    prefix = f"enterprise_info_{tenant_id}"
    rel = api.normalize_storage_key(rel_path)
    return f"{prefix}/{rel}" if rel else prefix


async def list_enterprise_kb_files_impl(
    api: Any,
    *,
    path: str,
    current_user: Any,
):
    """List files in enterprise knowledge base (tenant-scoped)."""

    if not current_user.tenant_id:
        return []
    storage = api.get_storage_backend()
    storage_key = api._enterprise_storage_key(str(current_user.tenant_id), path)
    if not await storage.exists(storage_key) or not await storage.is_dir(storage_key):
        return []
    items = []
    for entry in await storage.list_dir(storage_key):
        if entry.name == ".gitkeep":
            continue
        rel = str(Path(entry.key).relative_to(f"enterprise_info_{current_user.tenant_id}"))
        items.append({
            "name": entry.name,
            "path": rel,
            "is_dir": entry.is_dir,
            "size": entry.size,
            "url": f"/api/enterprise/knowledge-base/download?path={rel}" if not entry.is_dir else None,
        })
    return items


async def upload_enterprise_kb_file_impl(
    api: Any,
    *,
    file: Any,
    sub_path: str,
    current_user: Any,
):
    """Upload a file to enterprise knowledge base (tenant-scoped)."""

    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Only admins can upload to enterprise knowledge base")
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant associated")
    filename = (file.filename or "unnamed").replace("/", "_").replace("\\", "_")
    storage = api.get_storage_backend()
    rel_path = f"{sub_path}/{filename}" if sub_path else filename
    storage_key = api._enterprise_storage_key(str(current_user.tenant_id), rel_path)
    content = await file.read()
    await storage.write_bytes(storage_key, content, content_type=api.guess_content_type(filename))
    extracted_path = None
    from app.services.text_extractor import needs_extraction, save_extracted_text

    if needs_extraction(filename):
        save_path = await api.ensure_local_path(storage_key)
        txt_file = save_extracted_text(save_path, content, filename)
        if txt_file:
            extracted_path = f"{sub_path}/{txt_file.name}" if sub_path else txt_file.name
            await storage.write_bytes(
                api._enterprise_storage_key(str(current_user.tenant_id), extracted_path),
                txt_file.read_bytes(),
                content_type="text/plain; charset=utf-8",
            )
    return {
        "status": "ok",
        "path": rel_path,
        "url": f"/api/enterprise/knowledge-base/download?path={rel_path}",
        "filename": filename,
        "size": len(content),
        "extracted_text_path": extracted_path,
    }


async def read_enterprise_file_impl(
    api: Any,
    *,
    path: str,
    current_user: Any,
):
    """Read content of an enterprise knowledge base file (tenant-scoped)."""

    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant associated")
    storage = api.get_storage_backend()
    storage_key = api._enterprise_storage_key(str(current_user.tenant_id), path)
    if not await storage.exists(storage_key) or not await storage.is_file(storage_key):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content = await storage.read_text(storage_key, encoding="utf-8", errors="replace")
        return {"path": path, "content": content}
    except Exception:
        stat = await storage.stat(storage_key)
        return {"path": path, "content": f"[二进制文件: {Path(path).name}, {stat.size} bytes]"}


async def write_enterprise_file_impl(
    api: Any,
    *,
    path: str,
    data: Any,
    current_user: Any,
):
    """Write content to an enterprise file (tenant-scoped)."""

    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Only admins can edit enterprise knowledge base")
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant associated")
    storage = api.get_storage_backend()
    await storage.write_text(
        api._enterprise_storage_key(str(current_user.tenant_id), path),
        data.content,
        encoding="utf-8",
    )
    return {"status": "ok", "path": path}


async def delete_enterprise_file_impl(
    api: Any,
    *,
    path: str,
    current_user: Any,
):
    """Delete an enterprise knowledge base file (tenant-scoped)."""

    if current_user.role not in ("platform_admin", "org_admin"):
        raise HTTPException(status_code=403, detail="Only admins can delete enterprise knowledge base files")
    if not current_user.tenant_id:
        raise HTTPException(status_code=400, detail="No tenant associated")
    storage = api.get_storage_backend()
    storage_key = api._enterprise_storage_key(str(current_user.tenant_id), path)
    storage_exists = await storage.exists(storage_key)
    storage_is_dir = await storage.is_dir(storage_key)
    if not storage_exists and not storage_is_dir:
        raise HTTPException(status_code=404, detail="File not found")
    if storage_is_dir:
        await storage.delete_tree(storage_key)
    else:
        await storage.delete(storage_key)
    return {"status": "ok", "path": path}


async def agent_import_from_clawhub_impl(
    api: Any,
    *,
    agent_id: uuid.UUID,
    body: Any,
    current_user: Any,
    db: Any,
    workspace_agent: Any,
):
    """Import a skill from ClawHub directly into this agent's skills/ workspace."""

    await api.check_agent_access(db, current_user, agent_id)
    from app.api.skills import (
        _fetch_clawhub_skill_archive,
        _fetch_clawhub_skill_meta,
        _get_clawhub_key,
    )

    slug = body.slug
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    api_key = await _get_clawhub_key(tenant_id)
    try:
        meta, meta_base = await _fetch_clawhub_skill_meta(slug, api_key=api_key)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Failed to connect to ClawHub: {exc}")
    skill_info = meta.get("skill", {})
    files, _ = await _fetch_clawhub_skill_archive(slug, api_key=api_key, preferred_base=meta_base)
    base = api._agent_base_dir(agent_id)
    folder_name = slug
    skill_dir = base / "skills" / folder_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for file_entry in files:
        file_path = (skill_dir / file_entry["path"]).resolve()
        if not str(file_path).startswith(str(base.resolve())):
            continue
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(file_entry["content"], encoding="utf-8")
        written.append(file_entry["path"])
    await api._record_project_skill_change(
        db,
        workspace_agent,
        current_user,
        f"skills/{folder_name}/SKILL.md",
    )
    await db.commit()
    return {
        "status": "ok",
        "skill_name": skill_info.get("displayName", slug),
        "folder_name": folder_name,
        "files_written": len(written),
        "files": written,
    }


async def agent_import_from_url_impl(
    api: Any,
    *,
    agent_id: uuid.UUID,
    body: Any,
    current_user: Any,
    db: Any,
    workspace_agent: Any,
):
    """Import a skill from a GitHub URL directly into this agent's skills/ workspace."""

    await api.check_agent_access(db, current_user, agent_id)
    from app.api.skills import _fetch_github_directory, _get_github_token, _parse_github_url

    parsed = _parse_github_url(body.url)
    if not parsed:
        raise HTTPException(400, "Invalid GitHub URL")
    owner, repo, branch, path = parsed["owner"], parsed["repo"], parsed["branch"], parsed["path"]
    tenant_id = str(current_user.tenant_id) if current_user.tenant_id else None
    token = await _get_github_token(tenant_id)
    files = await _fetch_github_directory(owner, repo, path, branch, token)
    if not files:
        raise HTTPException(404, "No files found")
    folder_name = path.rstrip("/").split("/")[-1] if path else repo
    base = api._agent_base_dir(agent_id)
    skill_dir = base / "skills" / folder_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for file_entry in files:
        file_path = (skill_dir / file_entry["path"]).resolve()
        if not str(file_path).startswith(str(base.resolve())):
            continue
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(file_entry["content"], encoding="utf-8")
        written.append(file_entry["path"])
    await api._record_project_skill_change(
        db,
        workspace_agent,
        current_user,
        f"skills/{folder_name}/SKILL.md",
    )
    await db.commit()
    return {
        "status": "ok",
        "folder_name": folder_name,
        "files_written": len(written),
        "files": written,
    }
