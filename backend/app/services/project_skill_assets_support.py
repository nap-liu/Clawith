"""Shared project Skill asset validation and filesystem helpers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agent import Agent
from app.models.project import Project, ProjectCapabilityBinding, ProjectMemberSnapshot
from app.models.skill import Skill
from app.services.project_agent_workspace import (
    is_sensitive_project_asset_path,
    project_agent_workspace,
    resolve_project_agent_path,
)
from app.services.project_git_service import commit_project_changes, project_repo_path, project_user_git_email
from app.services.project_template_snapshot import ProjectTemplateSnapshotError, sanitize_template_scope
from app.services.skill_market import MAX_SKILL_FILES, validate_skill_files
from app.services.storage import get_storage_backend, normalize_storage_key

_ASSET_CONFIG_KEY = "skill_asset"
_ASSET_KEYS_V1 = {
    "schema_version",
    "version",
    "sha256",
    "path",
    "source",
    "source_agent_id",
    "file_count",
    "size_bytes",
}
_ASSET_KEYS = _ASSET_KEYS_V1 | {"asset_id"}
_PACKAGE_KEYS = {
    "schema_version",
    "name",
    "version",
    "folder_name",
    "digital_employee_index",
    "is_enabled",
    "scope",
    "sha256",
    "file_count",
    "size_bytes",
    "files",
}
_PACKAGE_FILE_KEYS = {"path", "size", "sha256", "content_base64"}
_FRONTMATTER_FIELD = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$")


@dataclass(slots=True)
class _SkillBackfillPlan:
    binding: ProjectCapabilityBinding
    previous_schema_version: int
    previous_asset_id: str | None
    metadata: dict
    source_path: Path
    target_path: Path
    other_config_fingerprint: str


async def _read_storage_skill(prefix: str) -> list[dict[str, str]]:
    storage = get_storage_backend()
    files: list[dict[str, str]] = []

    async def walk(key: str) -> None:
        for entry in await storage.list_dir(key):
            if entry.is_dir:
                await walk(entry.key)
                continue
            relative = entry.key.removeprefix(prefix.rstrip("/") + "/")
            relative = _validate_relative_path(relative)
            raw = await storage.read_bytes(entry.key)
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(status_code=422, detail=f"Project Skills must be UTF-8 text: {relative}") from exc
            files.append({"path": relative, "content": content})

    await walk(prefix)
    files.sort(key=lambda item: item["path"])
    try:
        validate_skill_files(files)
    except HTTPException as exc:
        raise HTTPException(status_code=422, detail=str(exc.detail)) from exc
    return files


def _read_local_skill(
    project_root: Path,
    agent_id: uuid.UUID,
    relative_path: str,
) -> list[dict[str, str]]:
    root = resolve_project_agent_path(project_root, agent_id, relative_path)
    return _read_skill_root(root, relative_path)


def _read_skill_root(root: Path, display_path: str) -> list[dict[str, str]]:
    if root.is_symlink() or not root.is_dir():
        raise ProjectTemplateSnapshotError(f"Project Skill asset is missing: {display_path}")
    files: list[dict[str, str]] = []
    for source in sorted(root.rglob("*")):
        if source.is_symlink():
            raise ProjectTemplateSnapshotError(f"Project Skill cannot contain symbolic links: {display_path}")
        if source.is_dir():
            continue
        if not source.is_file():
            raise ProjectTemplateSnapshotError(f"Project Skill contains an unsupported asset: {display_path}")
        path = _validate_relative_path(source.relative_to(root).as_posix())
        try:
            content = source.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectTemplateSnapshotError(f"Project Skill files must be UTF-8 text: {path}") from exc
        files.append({"path": path, "content": content})
    try:
        validate_skill_files(files)
    except HTTPException as exc:
        raise ProjectTemplateSnapshotError(str(exc.detail)) from exc
    return files


def _asset_metadata(
    files: list[dict[str, str]],
    *,
    path: str,
    version: str,
    source: str,
    source_agent_id: uuid.UUID | str | None,
    asset_id: str | None = None,
) -> dict:
    total_size = sum(len(item["content"].encode("utf-8")) for item in files)
    digest = _skill_hash(files)
    return {
        "schema_version": 2,
        "asset_id": asset_id or str(uuid.uuid4()),
        "version": str(version)[:80] or "1",
        "sha256": digest,
        "path": path,
        "source": source,
        "source_agent_id": str(source_agent_id) if source_agent_id else None,
        "file_count": len(files),
        "size_bytes": total_size,
    }


def _backfill_item(
    binding: ProjectCapabilityBinding,
    status: str,
    reason: str | None = None,
    *,
    previous_schema_version: int | None = None,
) -> dict:
    result = {
        "binding_id": str(binding.id),
        "member_agent_id": str(binding.inherited_from_agent_id) if binding.inherited_from_agent_id else None,
        "name": binding.capability_name,
        "status": status,
    }
    if reason:
        result["reason"] = reason
    if previous_schema_version is not None:
        result["previous_schema_version"] = previous_schema_version
        result["target_schema_version"] = 2
    return result


def _other_config_fingerprint(config: object) -> str:
    value = dict(config) if isinstance(config, dict) else {}
    value.pop(_ASSET_CONFIG_KEY, None)
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _skill_backfill_git_paths(project: Project, moved: list[tuple[Path, Path]]) -> list[str]:
    root = project_repo_path(project.tenant_id, project.id)
    paths = set()
    for pair in moved:
        for path in pair:
            relative = path.relative_to(root)
            if len(relative.parts) >= 3 and relative.parts[0] == ".agents":
                paths.add(PurePosixPath(*relative.parts[:3]).as_posix())
            else:
                paths.add(relative.as_posix())
    return sorted(paths)


async def _prepare_skill_backfill_plan(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
    raw_metadata: dict | None,
    root: Path,
    asset_ids: dict[tuple, str],
) -> _SkillBackfillPlan:
    if binding.inherited_from_agent_id is None:
        raise HTTPException(status_code=409, detail="Legacy shared Skill has no project member")
    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == binding.inherited_from_agent_id,
            )
        )
    ).scalar_one_or_none()
    agent = await db.get(Agent, binding.inherited_from_agent_id)
    if (
        member is None
        or agent is None
        or agent.tenant_id != project.tenant_id
        or agent.scope != "project"
        or agent.project_id != project.id
        or agent.is_deleted
    ):
        raise HTTPException(status_code=409, detail="Legacy Skill project member is unavailable")

    if raw_metadata is not None:
        normalized = _binding_asset_metadata(binding)
        if normalized is None:
            raise HTTPException(status_code=409, detail="Legacy Skill metadata is unavailable")
        if normalized["source"] not in {"agent", "library", "template", "workspace"}:
            raise HTTPException(status_code=409, detail="Legacy Skill source is invalid")
        source_path = _binding_asset_path(root, binding, normalized)
        files = await asyncio.to_thread(_read_skill_root, source_path, normalized["path"])
        folder = PurePosixPath(normalized["path"]).name
        _name, version = _skill_identity(folder, files, default_version=normalized["version"])
        metadata = _asset_metadata(
            files,
            path=normalized["path"],
            version=version,
            source=normalized["source"],
            source_agent_id=normalized.get("source_agent_id"),
        )
        if any(
            metadata[key] != normalized[key]
            for key in (
                "version",
                "sha256",
                "path",
                "source",
                "source_agent_id",
                "file_count",
                "size_bytes",
            )
        ):
            raise HTTPException(status_code=409, detail="Legacy Skill metadata does not match its files")
        previous_schema_version = 1
        previous_asset_id = str(raw_metadata["sha256"])
    else:
        folder, files, source, source_agent_id, default_version = await _discover_legacy_skill(
            db,
            project,
            binding,
            root,
        )
        relative_path = f"skills/{folder}"
        source_path = resolve_project_agent_path(root, binding.inherited_from_agent_id, relative_path)
        _name, version = _skill_identity(folder, files, default_version=default_version)
        metadata = _asset_metadata(
            files,
            path=relative_path,
            version=version,
            source=source,
            source_agent_id=source_agent_id,
        )
        previous_schema_version = 0
        previous_asset_id = None

    group_key = (
        binding.capability_id,
        metadata["source"],
        metadata.get("source_agent_id"),
        metadata["version"],
        metadata["path"],
        metadata["sha256"],
    )
    metadata["asset_id"] = asset_ids.setdefault(group_key, str(uuid.uuid4()))
    target_path = (
        resolve_project_agent_path(root, binding.inherited_from_agent_id, metadata["path"])
        if binding.is_enabled
        else resolve_project_agent_path(
            root,
            binding.inherited_from_agent_id,
            _disabled_asset_path(metadata),
        )
    )
    if target_path != source_path and target_path.exists():
        raise HTTPException(status_code=409, detail="Project Skill backfill path is occupied")
    return _SkillBackfillPlan(
        binding=binding,
        previous_schema_version=previous_schema_version,
        previous_asset_id=previous_asset_id,
        metadata=metadata,
        source_path=source_path,
        target_path=target_path,
        other_config_fingerprint=_other_config_fingerprint(binding.config),
    )


async def _discover_legacy_skill(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
    root: Path,
) -> tuple[str, list[dict[str, str]], str, uuid.UUID | None, str]:
    if binding.inherited_from_agent_id is None:
        raise HTTPException(status_code=409, detail="Legacy Skill has no project member")
    if binding.capability_id is not None:
        skill = (
            await db.execute(
                select(Skill).where(
                    Skill.id == binding.capability_id,
                    (Skill.tenant_id == project.tenant_id) | Skill.tenant_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if skill is None:
            raise HTTPException(status_code=409, detail="Legacy Skill source is unavailable")
        folder = _validate_folder_name(skill.folder_name)
        files = await asyncio.to_thread(
            _read_local_skill,
            root,
            binding.inherited_from_agent_id,
            f"skills/{folder}",
        )
        return folder, files, "library", skill.publisher_agent_id, str(skill.version or 1)

    skills_root = resolve_project_agent_path(root, binding.inherited_from_agent_id, "skills")
    if skills_root.is_symlink() or not skills_root.is_dir():
        raise HTTPException(status_code=409, detail="Legacy Skill files are unavailable")
    candidates: list[tuple[str, list[dict[str, str]]]] = []
    for entry in sorted(skills_root.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            folder = _validate_folder_name(entry.name)
            files = await asyncio.to_thread(_read_skill_root, entry, f"skills/{folder}")
            name, _version = _skill_identity(folder, files)
        except (HTTPException, OSError, ProjectTemplateSnapshotError):
            continue
        if name == binding.capability_name:
            candidates.append((folder, files))
    if len(candidates) != 1:
        raise HTTPException(status_code=409, detail="Legacy Skill files cannot be identified safely")
    folder, files = candidates[0]
    return folder, files, "workspace", None, "1"


def _binding_asset_metadata(binding: ProjectCapabilityBinding) -> dict | None:
    config = binding.config if isinstance(binding.config, dict) else {}
    raw = config.get(_ASSET_CONFIG_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProjectTemplateSnapshotError("Project Skill asset metadata is invalid")
    if set(raw) == _ASSET_KEYS_V1 and raw.get("schema_version") == 1:
        raw = {**raw, "schema_version": 2, "asset_id": str(raw.get("sha256") or "")}
    elif set(raw) != _ASSET_KEYS or raw.get("schema_version") != 2:
        raise ProjectTemplateSnapshotError("Project Skill asset metadata is invalid")
    path = str(raw.get("path") or "")
    pure = PurePosixPath(path)
    if len(pure.parts) != 2 or pure.parts[0] != "skills":
        raise ProjectTemplateSnapshotError("Project Skill asset path is invalid")
    _validate_folder_name(pure.parts[1])
    if not isinstance(raw.get("file_count"), int) or not isinstance(raw.get("size_bytes"), int):
        raise ProjectTemplateSnapshotError("Project Skill asset size metadata is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(raw.get("sha256") or "")):
        raise ProjectTemplateSnapshotError("Project Skill asset checksum is invalid")
    try:
        uuid.UUID(str(raw.get("asset_id") or ""))
    except ValueError:
        if str(raw.get("asset_id") or "") != str(raw.get("sha256") or ""):
            raise ProjectTemplateSnapshotError("Project Skill asset identity is invalid")
    return dict(raw)


async def _skill_bindings(
    db: AsyncSession,
    project: Project,
) -> list[ProjectCapabilityBinding]:
    return list(
        (
            await db.execute(
                select(ProjectCapabilityBinding)
                .where(
                    ProjectCapabilityBinding.project_id == project.id,
                    ProjectCapabilityBinding.tenant_id == project.tenant_id,
                    ProjectCapabilityBinding.capability_type == "skill",
                )
                .order_by(ProjectCapabilityBinding.created_at, ProjectCapabilityBinding.id)
            )
        ).scalars()
    )


def _disabled_asset_path(metadata: dict) -> str:
    folder = PurePosixPath(metadata["path"]).name
    return f".disabled-skills/{metadata['asset_id']}/{folder}"


def _binding_asset_path(
    project_root: Path,
    binding: ProjectCapabilityBinding,
    metadata: dict,
) -> Path:
    if binding.inherited_from_agent_id is None:
        raise ProjectTemplateSnapshotError("Project Skill binding member is unavailable")
    relative = metadata["path"] if binding.is_enabled else _disabled_asset_path(metadata)
    return resolve_project_agent_path(project_root, binding.inherited_from_agent_id, relative)


def _read_binding_skill(
    project_root: Path,
    binding: ProjectCapabilityBinding,
    metadata: dict,
) -> list[dict[str, str]]:
    root = _binding_asset_path(project_root, binding, metadata)
    if root.is_symlink() or not root.is_dir():
        raise ProjectTemplateSnapshotError(f"Project Skill asset is missing: {metadata['path']}")
    return _read_skill_root(root, metadata["path"])


async def _find_reusable_asset(
    db: AsyncSession,
    project: Project,
    *,
    capability_id: uuid.UUID | None,
    metadata: dict,
) -> tuple[ProjectCapabilityBinding, dict] | None:
    root = project_repo_path(project.tenant_id, project.id)
    for binding in await _skill_bindings(db, project):
        if binding.capability_id != capability_id:
            continue
        try:
            existing = _binding_asset_metadata(binding)
        except ProjectTemplateSnapshotError:
            continue
        if existing is None or any(
            existing[key] != metadata[key]
            for key in ("source", "source_agent_id", "version", "path", "sha256")
        ):
            continue
        try:
            observed = await asyncio.to_thread(_read_binding_skill, root, binding, existing)
        except (HTTPException, OSError, ProjectTemplateSnapshotError):
            continue
        if _skill_hash(observed) == existing["sha256"]:
            return binding, existing
    return None


def _require_skill_asset(binding: ProjectCapabilityBinding) -> dict:
    if binding.capability_type != "skill":
        raise HTTPException(status_code=422, detail="Capability is not a project Skill")
    try:
        metadata = _binding_asset_metadata(binding)
    except ProjectTemplateSnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if metadata is None or binding.inherited_from_agent_id is None:
        raise HTTPException(status_code=409, detail="Project Skill asset is unavailable")
    return metadata


def _asset_group_key(binding: ProjectCapabilityBinding, metadata: dict) -> tuple:
    return (
        metadata["asset_id"],
        binding.capability_id,
        metadata["source"],
        metadata.get("source_agent_id"),
        metadata["version"],
        metadata["path"],
    )


async def _shared_asset_bindings(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
    metadata: dict,
) -> list[tuple[ProjectCapabilityBinding, dict]]:
    expected = _asset_group_key(binding, metadata)
    result = []
    for candidate in await _skill_bindings(db, project):
        try:
            candidate_metadata = _binding_asset_metadata(candidate)
        except ProjectTemplateSnapshotError:
            continue
        if candidate_metadata is not None and _asset_group_key(candidate, candidate_metadata) == expected:
            result.append((candidate, candidate_metadata))
    if not result:
        raise HTTPException(status_code=409, detail="Project Skill asset associations are unavailable")
    return result


async def _load_refresh_source(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
    metadata: dict,
) -> tuple[list[dict[str, str]], str, str, str]:
    if metadata["source"] == "library" and binding.capability_id is not None:
        skill = (
            await db.execute(
                select(Skill)
                .where(
                    Skill.id == binding.capability_id,
                    (Skill.tenant_id == project.tenant_id) | Skill.tenant_id.is_(None),
                )
                .options(selectinload(Skill.files))
            )
        ).scalar_one_or_none()
        if skill is None:
            raise HTTPException(status_code=409, detail="Project Skill source is no longer available")
        files = [
            {"path": _validate_relative_path(file.path), "content": file.content or ""}
            for file in sorted(skill.files, key=lambda item: item.path)
        ]
        try:
            validate_skill_files(files)
        except HTTPException as exc:
            raise HTTPException(status_code=422, detail=str(exc.detail)) from exc
        return files, skill.name, str(skill.version or 1), _validate_folder_name(skill.folder_name)
    if metadata["source"] == "agent" and metadata.get("source_agent_id"):
        try:
            source_agent_id = uuid.UUID(str(metadata["source_agent_id"]))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Project Skill source is invalid") from exc
        folder = PurePosixPath(metadata["path"]).name
        prefix = normalize_storage_key(f"{source_agent_id}/skills/{folder}")
        storage = get_storage_backend()
        if not await storage.is_dir(prefix):
            raise HTTPException(status_code=409, detail="Project Skill source is no longer available")
        files = await _read_storage_skill(prefix)
        name, version = _skill_identity(folder, files)
        return files, name, version, folder
    raise HTTPException(
        status_code=409,
        detail="This project Skill has no refreshable source; edit its project Agent workspace directly",
    )


def _validate_packages(raw: object, agent_count: int) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 512:
        raise ProjectTemplateSnapshotError("Project template Skill list is invalid")
    result: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != _PACKAGE_KEYS or item.get("schema_version") != 1:
            raise ProjectTemplateSnapshotError("Project template Skill entry is invalid")
        index = item.get("digital_employee_index")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < agent_count:
            raise ProjectTemplateSnapshotError("Project template Skill member is invalid")
        name = str(item.get("name") or "").strip()
        if not name or len(name) > 200:
            raise ProjectTemplateSnapshotError("Project template Skill name is invalid")
        folder = _validate_folder_name(str(item.get("folder_name") or ""))
        key = (index, folder)
        if key in seen:
            raise ProjectTemplateSnapshotError("Project template contains a duplicate Skill path")
        seen.add(key)
        decoded = _decode_package_files(item.get("files"))
        metadata = _asset_metadata(
            decoded,
            path=f"skills/{folder}",
            version=str(item.get("version") or "1"),
            source="template",
            source_agent_id=None,
        )
        if (
            item.get("file_count") != metadata["file_count"]
            or item.get("size_bytes") != metadata["size_bytes"]
            or item.get("sha256") != metadata["sha256"]
        ):
            raise ProjectTemplateSnapshotError("Project template Skill manifest does not match its files")
        result.append(
            {
                "name": name,
                "version": str(item.get("version") or "1")[:80],
                "folder_name": folder,
                "digital_employee_index": index,
                "is_enabled": bool(item.get("is_enabled", True)),
                "scope": sanitize_template_scope(item.get("scope") or {}),
                "decoded_files": decoded,
                "sha256": metadata["sha256"],
            }
        )
    return result


def _decode_package_files(raw_files: object) -> list[dict[str, str]]:
    if not isinstance(raw_files, list) or len(raw_files) > MAX_SKILL_FILES:
        raise ProjectTemplateSnapshotError("Project template Skill files are invalid")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict) or set(raw) != _PACKAGE_FILE_KEYS:
            raise ProjectTemplateSnapshotError("Project template Skill file entry is invalid")
        path = _validate_relative_path(str(raw.get("path") or ""))
        if path in seen:
            raise ProjectTemplateSnapshotError("Project template Skill contains duplicate files")
        seen.add(path)
        try:
            content_bytes = base64.b64decode(str(raw.get("content_base64") or ""), validate=True)
            content = content_bytes.decode("utf-8")
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise ProjectTemplateSnapshotError(f"Project template Skill file content is invalid: {path}") from exc
        if raw.get("size") != len(content_bytes) or raw.get("sha256") != hashlib.sha256(content_bytes).hexdigest():
            raise ProjectTemplateSnapshotError(f"Project template Skill file manifest is invalid: {path}")
        result.append({"path": path, "content": content})
    try:
        validate_skill_files(result)
    except HTTPException as exc:
        raise ProjectTemplateSnapshotError(str(exc.detail)) from exc
    return result


def _package_file(file: dict[str, str]) -> dict:
    content = file["content"].encode("utf-8")
    return {
        "path": file["path"],
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def _skill_hash(files: list[dict[str, str]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda value: value["path"]):
        content = item["content"].encode("utf-8")
        digest.update(item["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _skill_identity(
    folder: str,
    files: list[dict[str, str]],
    *,
    default_version: str = "1",
) -> tuple[str, str]:
    manifest = next((item["content"] for item in files if item["path"] == "SKILL.md"), "")
    fields: dict[str, str] = {}
    if manifest.startswith("---\n"):
        for line in manifest.splitlines()[1:]:
            if line == "---":
                break
            match = _FRONTMATTER_FIELD.match(line)
            if match:
                fields[match.group(1).casefold()] = match.group(2).strip().strip("'\"")
    name = (fields.get("name") or folder.replace("-", " ").replace("_", " ").title()).strip()
    return name[:200], (fields.get("version") or default_version)[:80]


def _skill_description(files: list[dict[str, str]]) -> str:
    manifest = next((item["content"] for item in files if item["path"] == "SKILL.md"), "")
    if not manifest.startswith("---\n"):
        return ""
    for line in manifest.splitlines()[1:]:
        if line == "---":
            break
        match = _FRONTMATTER_FIELD.match(line)
        if match and match.group(1).casefold() == "description":
            return match.group(2).strip().strip("'\"")[:2000]
    return ""


def _validate_folder_name(folder: str) -> str:
    normalized = folder.strip()
    if (
        not normalized
        or len(normalized.encode("utf-8")) > 100
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
        or is_sensitive_project_asset_path(PurePosixPath("skills") / normalized)
    ):
        raise ProjectTemplateSnapshotError("Project Skill folder name is invalid")
    return normalized


def _validate_relative_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or pure.as_posix() != normalized
        or any(part in {"", ".", ".."} for part in pure.parts)
        or len(normalized.encode("utf-8")) > 500
        or is_sensitive_project_asset_path(pure)
    ):
        raise ProjectTemplateSnapshotError(f"Project Skill file path is invalid: {path}")
    return normalized


def _write_prepared_skills(
    staging: Path,
    prepared: list[tuple[str, str, str, list[dict[str, str]], dict, Path | None]],
) -> None:
    staging.mkdir(parents=True, exist_ok=False)
    for folder, _name, _version, files, _metadata, source_path in prepared:
        if source_path is None:
            _write_files(staging / folder, files)
        else:
            _link_files(source_path, staging / folder, files)


def _write_files(root: Path, files: list[dict[str, str]]) -> None:
    root.mkdir(parents=True, exist_ok=False)
    for item in files:
        target = root.joinpath(*PurePosixPath(item["path"]).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")


def _link_files(source_root: Path, target_root: Path, files: list[dict[str, str]]) -> None:
    target_root.mkdir(parents=True, exist_ok=False)
    for item in files:
        relative = PurePosixPath(item["path"])
        source = source_root.joinpath(*relative.parts)
        if source.is_symlink() or not source.is_file():
            raise ProjectTemplateSnapshotError(f"Shared project Skill file is unavailable: {item['path']}")
        target = target_root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, target)
