"""Project-owned Skill snapshots and portable template packages."""

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


async def bind_library_skill_to_project_agent(
    db: AsyncSession,
    project: Project,
    *,
    skill_id: uuid.UUID | None,
    project_agent_id: uuid.UUID | None,
    is_enabled: bool,
    scope: dict,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> ProjectCapabilityBinding:
    """Copy one visible library Skill into a project Agent and create its binding."""

    if skill_id is None or project_agent_id is None:
        raise HTTPException(
            status_code=422,
            detail="Project Skills require a Skill and a project digital employee",
        )
    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == project_agent_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    agent = await db.get(Agent, project_agent_id)
    if (
        member is None
        or agent is None
        or agent.tenant_id != project.tenant_id
        or agent.scope != "project"
        or agent.project_id != project.id
        or agent.is_deleted
    ):
        raise HTTPException(status_code=422, detail="Project Skill target must be an active project digital employee")
    skill = (
        await db.execute(
            select(Skill)
            .where(
                Skill.id == skill_id,
                (Skill.tenant_id == project.tenant_id) | Skill.tenant_id.is_(None),
            )
            .options(selectinload(Skill.files))
        )
    ).scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=422, detail="Skill is unavailable in this tenant")
    folder = _validate_folder_name(skill.folder_name)
    files = [
        {"path": _validate_relative_path(file.path), "content": file.content or ""}
        for file in sorted(skill.files, key=lambda item: item.path)
    ]
    try:
        validate_skill_files(files)
    except HTTPException as exc:
        raise HTTPException(status_code=422, detail=str(exc.detail)) from exc
    root = project_repo_path(project.tenant_id, project.id)
    relative_path = f"skills/{folder}"
    metadata = _asset_metadata(
        files,
        path=relative_path,
        version=str(skill.version or 1),
        source="library",
        source_agent_id=skill.publisher_agent_id,
    )
    reusable = await _find_reusable_asset(
        db,
        project,
        capability_id=skill.id,
        metadata=metadata,
    )
    if reusable is not None:
        reusable_binding, reusable_metadata = reusable
        metadata["asset_id"] = reusable_metadata["asset_id"]
        source_path = _binding_asset_path(root, reusable_binding, reusable_metadata)
    else:
        source_path = None
    target_relative = relative_path if is_enabled else _disabled_asset_path(metadata)
    target = resolve_project_agent_path(root, project_agent_id, target_relative)
    if target.exists():
        raise HTTPException(status_code=409, detail=f"Project Skill path already exists: {relative_path}")
    staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.binding")
    created = False
    try:
        if source_path is None:
            await asyncio.to_thread(_write_files, staging, files)
        else:
            await asyncio.to_thread(_link_files, source_path, staging, files)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, target)
        created = True
        binding = ProjectCapabilityBinding(
            tenant_id=project.tenant_id,
            project_id=project.id,
            capability_type="skill",
            capability_id=skill.id,
            capability_name=skill.name,
            source="inherited",
            inherited_from_agent_id=project_agent_id,
            is_enabled=is_enabled,
            scope=sanitize_template_scope(scope or {}),
            config={_ASSET_CONFIG_KEY: metadata},
        )
        db.add(binding)
        await db.flush()
        await commit_project_changes(
            project,
            f"Add project Skill: {skill.name}",
            [f".agents/{project_agent_id}/{target_relative}"],
            author_name=actor_display_name,
            author_email=project_user_git_email(actor_user_id),
        )
        return binding
    except Exception:
        if created:
            await asyncio.to_thread(shutil.rmtree, target, True)
        raise
    finally:
        await asyncio.to_thread(shutil.rmtree, staging, True)


async def snapshot_source_agent_skills(
    db: AsyncSession,
    project: Project,
    *,
    source_agent_id: uuid.UUID,
    project_agent_id: uuid.UUID,
) -> list[ProjectCapabilityBinding]:
    """Copy valid source-Agent Skills into one project Agent and bind them."""

    source_agent = await db.get(Agent, source_agent_id)
    project_agent = await db.get(Agent, project_agent_id)
    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == project_agent_id,
                ProjectMemberSnapshot.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    if (
        source_agent is None
        or source_agent.tenant_id != project.tenant_id
        or source_agent.scope != "standard"
        or source_agent.is_deleted
        or project_agent is None
        or project_agent.tenant_id != project.tenant_id
        or project_agent.scope != "project"
        or project_agent.project_id != project.id
        or project_agent.is_deleted
        or member is None
    ):
        raise HTTPException(status_code=422, detail="Project Skill snapshot source or target is unavailable")

    source_prefix = normalize_storage_key(f"{source_agent_id}/skills")
    storage = get_storage_backend()
    if not await storage.is_dir(source_prefix):
        return []

    project_root = project_repo_path(project.tenant_id, project.id)
    project_agent_workspace(project_root, project_agent_id)
    prepared: list[tuple[str, str, str, list[dict[str, str]], dict, Path | None]] = []
    for entry in await storage.list_dir(source_prefix):
        if not entry.is_dir:
            continue
        folder = _validate_folder_name(entry.name)
        files = await _read_storage_skill(entry.key)
        name, version = _skill_identity(folder, files)
        metadata = _asset_metadata(
            files,
            path=f"skills/{folder}",
            version=version,
            source="agent",
            source_agent_id=source_agent_id,
        )
        reusable = await _find_reusable_asset(
            db,
            project,
            capability_id=None,
            metadata=metadata,
        )
        source_path = None
        if reusable is not None:
            reusable_binding, reusable_metadata = reusable
            metadata["asset_id"] = reusable_metadata["asset_id"]
            source_path = _binding_asset_path(project_root, reusable_binding, reusable_metadata)
        prepared.append((folder, name, version, files, metadata, source_path))

    if not prepared:
        return []

    skills_root = resolve_project_agent_path(project_root, project_agent_id, "skills")
    if skills_root.exists():
        raise HTTPException(status_code=409, detail="Project Agent Skill directory already exists")
    staging = skills_root.with_name(f".{skills_root.name}.{uuid.uuid4().hex}.importing")
    created = False
    try:
        await asyncio.to_thread(_write_prepared_skills, staging, prepared)
        skills_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, skills_root)
        created = True
        bindings = []
        for _folder, name, _version, _files, metadata, _source_path in prepared:
            binding = ProjectCapabilityBinding(
                tenant_id=project.tenant_id,
                project_id=project.id,
                capability_type="skill",
                capability_id=None,
                capability_name=name,
                source="inherited",
                inherited_from_agent_id=project_agent_id,
                is_enabled=True,
                scope={},
                config={_ASSET_CONFIG_KEY: metadata},
            )
            db.add(binding)
            bindings.append(binding)
        await db.flush()
        return bindings
    except Exception:
        if created:
            await asyncio.to_thread(shutil.rmtree, skills_root, True)
        raise
    finally:
        await asyncio.to_thread(shutil.rmtree, staging, True)


async def plan_project_skill_backfill(
    db: AsyncSession,
    project: Project,
) -> tuple[dict, list[_SkillBackfillPlan]]:
    """Inspect legacy Skill bindings without changing database or workspace state."""

    bindings = await _skill_bindings(db, project)
    root = project_repo_path(project.tenant_id, project.id)
    ready: list[_SkillBackfillPlan] = []
    items: list[dict] = []
    asset_ids: dict[tuple, str] = {}
    for binding in bindings:
        raw_config = binding.config if isinstance(binding.config, dict) else {}
        raw_metadata = raw_config.get(_ASSET_CONFIG_KEY)
        if isinstance(raw_metadata, dict) and raw_metadata.get("schema_version") == 2:
            try:
                _binding_asset_metadata(binding)
            except ProjectTemplateSnapshotError as exc:
                items.append(_backfill_item(binding, "blocked", str(exc)))
            else:
                items.append(_backfill_item(binding, "current"))
            continue
        if raw_metadata is not None and not (
            isinstance(raw_metadata, dict)
            and raw_metadata.get("schema_version") == 1
            and set(raw_metadata) == _ASSET_KEYS_V1
        ):
            items.append(_backfill_item(binding, "blocked", "Legacy Skill metadata is invalid"))
            continue
        try:
            plan = await _prepare_skill_backfill_plan(
                db,
                project,
                binding,
                raw_metadata,
                root,
                asset_ids,
            )
        except (HTTPException, OSError, ProjectTemplateSnapshotError) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            items.append(_backfill_item(binding, "blocked", str(detail)))
            continue
        ready.append(plan)
        items.append(
            _backfill_item(
                binding,
                "ready",
                previous_schema_version=plan.previous_schema_version,
            )
        )
    summary = {
        "project_id": str(project.id),
        "ready_count": sum(item["status"] == "ready" for item in items),
        "current_count": sum(item["status"] == "current" for item in items),
        "blocked_count": sum(item["status"] == "blocked" for item in items),
        "items": items,
    }
    return summary, ready


async def apply_project_skill_backfill(
    db: AsyncSession,
    project: Project,
    *,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> tuple[dict, list[dict]]:
    """Upgrade every safe legacy Skill binding as one compensating operation."""

    summary, plans = await plan_project_skill_backfill(db, project)
    if summary["blocked_count"]:
        raise HTTPException(
            status_code=409,
            detail={"code": "project_skill_backfill_blocked", **summary},
        )
    moved: list[tuple[Path, Path]] = []
    previous_configs = [(plan.binding, dict(plan.binding.config or {})) for plan in plans]
    try:
        for plan in plans:
            if plan.source_path != plan.target_path:
                plan.target_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(plan.source_path, plan.target_path)
                moved.append((plan.source_path, plan.target_path))
            plan.binding.config = {
                **dict(plan.binding.config or {}),
                _ASSET_CONFIG_KEY: dict(plan.metadata),
            }
        await db.flush()
        if moved:
            await commit_project_changes(
                project,
                f"Normalize {len(plans)} legacy project Skills",
                _skill_backfill_git_paths(project, moved),
                author_name=actor_display_name,
                author_email=project_user_git_email(actor_user_id),
            )
    except Exception:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, source)
        for binding, config in previous_configs:
            binding.config = config
        raise
    rollback_entries = [
        {
            "binding_id": str(plan.binding.id),
            "previous_schema_version": plan.previous_schema_version,
            "previous_asset_id": plan.previous_asset_id,
            "applied_asset_id": plan.metadata["asset_id"],
            "other_config_fingerprint": plan.other_config_fingerprint,
        }
        for plan in plans
    ]
    return {**summary, "applied_count": len(plans)}, rollback_entries


async def rollback_project_skill_backfill(
    db: AsyncSession,
    project: Project,
    entries: object,
    *,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> dict:
    """Restore the exact legacy metadata shape when assets remain unchanged."""

    if not isinstance(entries, list):
        raise HTTPException(status_code=409, detail="Project Skill backfill audit data is invalid")
    bindings_by_id = {binding.id: binding for binding in await _skill_bindings(db, project)}
    prepared: list[tuple[ProjectCapabilityBinding, dict, dict | None, Path, Path]] = []
    root = project_repo_path(project.tenant_id, project.id)
    for entry in entries:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=409, detail="Project Skill backfill audit data is invalid")
        try:
            binding_id = uuid.UUID(str(entry.get("binding_id")))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="Project Skill backfill audit data is invalid") from exc
        binding = bindings_by_id.get(binding_id)
        if binding is None:
            raise HTTPException(status_code=409, detail="A backfilled Project Skill binding is unavailable")
        metadata = _require_skill_asset(binding)
        if metadata["asset_id"] != entry.get("applied_asset_id"):
            raise HTTPException(status_code=409, detail="Project Skill changed after backfill")
        if _other_config_fingerprint(binding.config) != entry.get("other_config_fingerprint"):
            raise HTTPException(status_code=409, detail="Project Skill configuration changed after backfill")
        source = _binding_asset_path(root, binding, metadata)
        files = await asyncio.to_thread(_read_skill_root, source, metadata["path"])
        if _skill_hash(files) != metadata["sha256"]:
            raise HTTPException(status_code=409, detail="Project Skill files changed after backfill")
        previous_schema = entry.get("previous_schema_version")
        if previous_schema == 0:
            previous_metadata = None
            if binding.inherited_from_agent_id is None:
                raise HTTPException(status_code=409, detail="Project Skill member is unavailable")
            target = resolve_project_agent_path(
                root,
                binding.inherited_from_agent_id,
                metadata["path"],
            )
        elif previous_schema == 1:
            previous_metadata = {
                key: value
                for key, value in metadata.items()
                if key != "asset_id"
            }
            previous_metadata["schema_version"] = 1
            old_asset_id = str(entry.get("previous_asset_id") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", old_asset_id):
                raise HTTPException(status_code=409, detail="Project Skill backfill audit data is invalid")
            target = (
                resolve_project_agent_path(root, binding.inherited_from_agent_id, metadata["path"])
                if binding.is_enabled
                else resolve_project_agent_path(
                    root,
                    binding.inherited_from_agent_id,
                    f".disabled-skills/{old_asset_id}/{PurePosixPath(metadata['path']).name}",
                )
            )
        else:
            raise HTTPException(status_code=409, detail="Project Skill backfill audit data is invalid")
        if target != source and target.exists():
            raise HTTPException(status_code=409, detail="Project Skill rollback path is occupied")
        prepared.append((binding, dict(binding.config or {}), previous_metadata, source, target))

    moved: list[tuple[Path, Path]] = []
    try:
        for binding, current_config, previous_metadata, source, target in prepared:
            if source != target:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                moved.append((source, target))
            restored_config = dict(current_config)
            if previous_metadata is None:
                restored_config.pop(_ASSET_CONFIG_KEY, None)
            else:
                restored_config[_ASSET_CONFIG_KEY] = previous_metadata
            binding.config = restored_config
        await db.flush()
        if moved:
            await commit_project_changes(
                project,
                f"Roll back {len(prepared)} project Skill normalizations",
                _skill_backfill_git_paths(project, moved),
                author_name=actor_display_name,
                author_email=project_user_git_email(actor_user_id),
            )
    except Exception:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, source)
        for binding, current_config, _previous_metadata, _source, _target in prepared:
            binding.config = current_config
        raise
    return {
        "project_id": str(project.id),
        "rolled_back_count": len(prepared),
        "binding_ids": [str(binding.id) for binding, *_rest in prepared],
    }


async def register_project_workspace_skill(
    db: AsyncSession,
    project: Project,
    *,
    project_agent_id: uuid.UUID,
    folder_name: str,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> ProjectCapabilityBinding:
    """Register the existing project Agent Skill directory without copying it."""

    folder = _validate_folder_name(folder_name)
    relative_path = f"skills/{folder}"
    root = project_repo_path(project.tenant_id, project.id)
    files = await asyncio.to_thread(_read_local_skill, root, project_agent_id, relative_path)
    existing = next(
        (
            binding
            for binding in await _skill_bindings(db, project)
            if binding.inherited_from_agent_id == project_agent_id
            and (metadata := _binding_asset_metadata(binding)) is not None
            and metadata["path"] == relative_path
        ),
        None,
    )
    existing_metadata = _binding_asset_metadata(existing) if existing is not None else None
    name, version = _skill_identity(
        folder,
        files,
        default_version=existing_metadata["version"] if existing_metadata is not None else "1",
    )
    if existing is None:
        metadata = _asset_metadata(
            files,
            path=relative_path,
            version=version,
            source="workspace",
            source_agent_id=None,
        )
        existing = ProjectCapabilityBinding(
            tenant_id=project.tenant_id,
            project_id=project.id,
            capability_type="skill",
            capability_id=None,
            capability_name=name,
            source="inherited",
            inherited_from_agent_id=project_agent_id,
            is_enabled=True,
            scope={},
            config={_ASSET_CONFIG_KEY: metadata},
        )
        db.add(existing)
    else:
        old_metadata = _require_skill_asset(existing)
        group = await _shared_asset_bindings(db, project, existing, old_metadata)
        for shared_binding, shared_metadata in group:
            shared_binding.capability_name = name
            shared_binding.config = {
                _ASSET_CONFIG_KEY: _asset_metadata(
                    files,
                    path=shared_metadata["path"],
                    version=version,
                    source=shared_metadata["source"],
                    source_agent_id=shared_metadata.get("source_agent_id"),
                    asset_id=shared_metadata["asset_id"],
                )
            }
    await db.flush()
    await commit_project_changes(
        project,
        f"Update project Skill: {name}",
        [f".agents/{project_agent_id}/{relative_path}"],
        author_name=actor_display_name,
        author_email=project_user_git_email(actor_user_id),
    )
    return existing


async def project_skill_manifest(
    db: AsyncSession,
    project: Project,
    *,
    project_root: Path | None = None,
) -> list[dict]:
    """Return owner-facing, per-binding Skill asset metadata."""

    bindings = await _skill_bindings(db, project)
    agent_ids = {binding.inherited_from_agent_id for binding in bindings if binding.inherited_from_agent_id}
    members = {}
    if agent_ids:
        rows = (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id.in_(agent_ids),
                )
            )
        ).scalars()
        members = {row.agent_id: row for row in rows}

    root = project_root or project_repo_path(project.tenant_id, project.id)
    result: list[dict] = []
    for binding in bindings:
        metadata = _binding_asset_metadata(binding)
        if metadata is None or binding.inherited_from_agent_id is None:
            continue
        files = await asyncio.to_thread(_read_binding_skill, root, binding, metadata)
        observed = _asset_metadata(
            files,
            path=metadata["path"],
            version=_skill_identity(
                PurePosixPath(metadata["path"]).name,
                files,
                default_version=metadata["version"],
            )[1],
            source=metadata["source"],
            source_agent_id=metadata.get("source_agent_id"),
            asset_id=metadata["asset_id"],
        )
        member = members.get(binding.inherited_from_agent_id)
        if member is None:
            raise ProjectTemplateSnapshotError("Project Skill binding member is unavailable")
        result.append(
            {
                "binding_id": str(binding.id),
                "asset_id": metadata["asset_id"],
                "member_id": str(member.id),
                "member_agent_id": str(member.agent_id),
                "member_name": member.name_snapshot,
                "member_role": member.role_snapshot,
                "name": binding.capability_name,
                "version": observed["version"],
                "path": metadata["path"],
                "source": metadata["source"],
                "is_enabled": binding.is_enabled,
                "file_count": observed["file_count"],
                "size_bytes": observed["size_bytes"],
                "sha256": observed["sha256"],
            }
        )
    return result


async def observe_project_skill_binding(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
) -> dict:
    """Return UI-safe availability and size metadata for one Skill binding."""

    try:
        metadata = _binding_asset_metadata(binding)
    except ProjectTemplateSnapshotError:
        metadata = None
    if metadata is None or binding.inherited_from_agent_id is None:
        return {
            "asset_id": None,
            "availability": "missing",
            "description": "",
            "version": None,
            "file_count": 0,
            "size_bytes": 0,
        }
    member = (
        await db.execute(
            select(ProjectMemberSnapshot).where(
                ProjectMemberSnapshot.project_id == project.id,
                ProjectMemberSnapshot.tenant_id == project.tenant_id,
                ProjectMemberSnapshot.agent_id == binding.inherited_from_agent_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        availability = "restricted"
        files: list[dict[str, str]] = []
    else:
        try:
            files = await asyncio.to_thread(
                _read_binding_skill,
                project_repo_path(project.tenant_id, project.id),
                binding,
                metadata,
            )
            observed = _asset_metadata(
                files,
                path=metadata["path"],
                version=_skill_identity(
                    PurePosixPath(metadata["path"]).name,
                    files,
                    default_version=metadata["version"],
                )[1],
                source=metadata["source"],
                source_agent_id=metadata.get("source_agent_id"),
                asset_id=metadata["asset_id"],
            )
            availability = "available"
        except (HTTPException, OSError, ProjectTemplateSnapshotError):
            files = []
            availability = "missing"
    affected_member_count = len(await _shared_asset_bindings(db, project, binding, metadata))
    return {
        "asset_id": metadata["asset_id"],
        "affected_member_count": affected_member_count,
        "availability": availability,
        "description": _skill_description(files),
        "version": observed["version"] if files else metadata["version"],
        "file_count": observed["file_count"] if files else metadata["file_count"],
        "size_bytes": observed["size_bytes"] if files else metadata["size_bytes"],
    }


async def set_project_skill_enabled(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
    *,
    enabled: bool,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> None:
    """Move one member projection in or out of its active Skill directory."""

    metadata = _require_skill_asset(binding)
    if binding.is_enabled == enabled:
        return
    root = project_repo_path(project.tenant_id, project.id)
    source = _binding_asset_path(root, binding, metadata)
    target_relative = metadata["path"] if enabled else _disabled_asset_path(metadata)
    if binding.inherited_from_agent_id is None:
        raise HTTPException(status_code=422, detail="Project Skill member is unavailable")
    target = resolve_project_agent_path(root, binding.inherited_from_agent_id, target_relative)
    if not source.is_dir():
        raise HTTPException(status_code=409, detail="Project Skill files are unavailable")
    if target.exists():
        raise HTTPException(status_code=409, detail=f"Project Skill path already exists: {target_relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    previous = binding.is_enabled
    binding.is_enabled = enabled
    try:
        await db.flush()
        await commit_project_changes(
            project,
            f"{'Enable' if enabled else 'Disable'} project Skill: {binding.capability_name}",
            [
                f".agents/{binding.inherited_from_agent_id}/{metadata['path']}",
                f".agents/{binding.inherited_from_agent_id}/{_disabled_asset_path(metadata)}",
            ],
            author_name=actor_display_name,
            author_email=project_user_git_email(actor_user_id),
        )
    except Exception:
        binding.is_enabled = previous
        source.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not source.exists():
            os.replace(target, source)
        raise


async def project_skill_deletion_impact(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
) -> dict:
    """Describe every member association removed with one shared Skill asset."""

    metadata = _require_skill_asset(binding)
    group = await _shared_asset_bindings(db, project, binding, metadata)
    agent_ids = {item.inherited_from_agent_id for item, _metadata in group if item.inherited_from_agent_id}
    members = {}
    if agent_ids:
        rows = (
            await db.execute(
                select(ProjectMemberSnapshot).where(
                    ProjectMemberSnapshot.project_id == project.id,
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    ProjectMemberSnapshot.agent_id.in_(agent_ids),
                )
            )
        ).scalars()
        members = {row.agent_id: row for row in rows}
    affected = []
    for item, _item_metadata in group:
        member = members.get(item.inherited_from_agent_id)
        if member is None:
            raise HTTPException(status_code=409, detail="Project Skill has an unavailable member association")
        affected.append(
            {
                "binding_id": str(item.id),
                "member_id": str(member.id),
                "agent_id": str(member.agent_id),
                "name": member.name_snapshot,
                "role": member.role_snapshot,
                "is_active": member.is_enabled,
                "skill_enabled": item.is_enabled,
            }
        )
    return {
        "asset_id": metadata["asset_id"],
        "skill_name": binding.capability_name,
        "version": metadata["version"],
        "affected_member_count": len(affected),
        "affected_members": affected,
    }


async def delete_project_skill_asset(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
    *,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> dict:
    """Delete one shared asset and all of its member associations without dangling bindings."""

    metadata = _require_skill_asset(binding)
    impact = await project_skill_deletion_impact(db, project, binding)
    group = await _shared_asset_bindings(db, project, binding, metadata)
    root = project_repo_path(project.tenant_id, project.id)
    moved: list[tuple[Path, Path]] = []
    changed_paths: list[str] = []
    try:
        for item, item_metadata in group:
            source = _binding_asset_path(root, item, item_metadata)
            if not source.is_dir():
                raise HTTPException(status_code=409, detail="Project Skill files are unavailable")
            backup = source.with_name(f".{source.name}.{uuid.uuid4().hex}.deleting")
            os.replace(source, backup)
            moved.append((source, backup))
            relative = item_metadata["path"] if item.is_enabled else _disabled_asset_path(item_metadata)
            changed_paths.append(f".agents/{item.inherited_from_agent_id}/{relative}")
            await db.delete(item)
        await db.flush()
        await commit_project_changes(
            project,
            f"Delete project Skill: {binding.capability_name}",
            changed_paths,
            author_name=actor_display_name,
            author_email=project_user_git_email(actor_user_id),
        )
    except Exception:
        for source, backup in reversed(moved):
            if backup.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, source)
        raise
    for _source, backup in moved:
        await asyncio.to_thread(shutil.rmtree, backup, True)
    return impact


async def refresh_project_skill_asset(
    db: AsyncSession,
    project: Project,
    binding: ProjectCapabilityBinding,
    *,
    actor_user_id: uuid.UUID,
    actor_display_name: str,
) -> dict:
    """Refresh a shared asset from its existing library or source-Agent origin."""

    metadata = _require_skill_asset(binding)
    files, name, version, folder = await _load_refresh_source(db, project, binding, metadata)
    new_metadata = _asset_metadata(
        files,
        path=f"skills/{folder}",
        version=version,
        source=metadata["source"],
        source_agent_id=metadata.get("source_agent_id"),
        asset_id=metadata["asset_id"],
    )
    group = await _shared_asset_bindings(db, project, binding, metadata)
    impact = await project_skill_deletion_impact(db, project, binding)
    changed = any(
        new_metadata[key] != metadata[key] for key in ("version", "path", "sha256", "file_count", "size_bytes")
    ) or name != binding.capability_name
    if not changed:
        return {"changed": False, **impact}

    root = project_repo_path(project.tenant_id, project.id)
    prepared: list[tuple[ProjectCapabilityBinding, dict, Path, Path, Path, Path]] = []
    staging_paths: list[Path] = []
    first_staging: Path | None = None
    try:
        for item, old_metadata in group:
            old_path = _binding_asset_path(root, item, old_metadata)
            relative = new_metadata["path"] if item.is_enabled else _disabled_asset_path(new_metadata)
            if item.inherited_from_agent_id is None:
                raise HTTPException(status_code=409, detail="Project Skill member is unavailable")
            target = resolve_project_agent_path(root, item.inherited_from_agent_id, relative)
            if target != old_path and target.exists():
                raise HTTPException(status_code=409, detail=f"Project Skill path already exists: {relative}")
            staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.refreshing")
            if first_staging is None:
                await asyncio.to_thread(_write_files, staging, files)
                first_staging = staging
            else:
                await asyncio.to_thread(_link_files, first_staging, staging, files)
            staging_paths.append(staging)
            backup = old_path.with_name(f".{old_path.name}.{uuid.uuid4().hex}.previous")
            prepared.append((item, old_metadata, old_path, target, backup, staging))

        moved: list[tuple[Path, Path]] = []
        installed: list[Path] = []
        old_states = [(item, item.capability_name, dict(item.config or {})) for item, *_rest in prepared]
        try:
            for item, _old_metadata, old_path, target, backup, staging in prepared:
                os.replace(old_path, backup)
                moved.append((old_path, backup))
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, target)
                installed.append(target)
                item.capability_name = name
                item.config = {_ASSET_CONFIG_KEY: dict(new_metadata)}
            await db.flush()
            changed_paths = []
            for item, old_metadata, _old_path, _target, _backup, _staging in prepared:
                old_relative = old_metadata["path"] if item.is_enabled else _disabled_asset_path(old_metadata)
                new_relative = new_metadata["path"] if item.is_enabled else _disabled_asset_path(new_metadata)
                changed_paths.extend(
                    [
                        f".agents/{item.inherited_from_agent_id}/{old_relative}",
                        f".agents/{item.inherited_from_agent_id}/{new_relative}",
                    ]
                )
            await commit_project_changes(
                project,
                f"Refresh project Skill: {name}",
                changed_paths,
                author_name=actor_display_name,
                author_email=project_user_git_email(actor_user_id),
            )
        except Exception:
            for target in installed:
                await asyncio.to_thread(shutil.rmtree, target, True)
            for old_path, backup in reversed(moved):
                if backup.exists() and not old_path.exists():
                    old_path.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, old_path)
            for item, old_name, old_config in old_states:
                item.capability_name = old_name
                item.config = old_config
            raise
        for _old_path, backup in moved:
            await asyncio.to_thread(shutil.rmtree, backup, True)
    finally:
        for path in staging_paths:
            await asyncio.to_thread(shutil.rmtree, path, True)
    return {"changed": True, **impact, "version": new_metadata["version"]}


async def export_project_skills_for_template(
    db: AsyncSession,
    project: Project,
    *,
    included_binding_ids: list[uuid.UUID],
    project_root: Path | None = None,
) -> list[dict]:
    """Package only explicitly selected project Skill snapshots."""

    if not included_binding_ids:
        return []
    if len(set(included_binding_ids)) != len(included_binding_ids):
        raise ProjectTemplateSnapshotError("Selected project Skill bindings must be unique")
    manifest = await project_skill_manifest(db, project, project_root=project_root)
    by_id = {uuid.UUID(item["binding_id"]): item for item in manifest}
    missing = set(included_binding_ids) - set(by_id)
    if missing:
        raise ProjectTemplateSnapshotError("One or more selected project Skills are unavailable")
    template_agent_ids = list(
        (
            await db.execute(
                select(Agent.id)
                .join(
                    ProjectMemberSnapshot,
                    (ProjectMemberSnapshot.project_id == project.id)
                    & (ProjectMemberSnapshot.agent_id == Agent.id),
                )
                .where(
                    ProjectMemberSnapshot.tenant_id == project.tenant_id,
                    Agent.tenant_id == project.tenant_id,
                    Agent.is_deleted.is_(False),
                )
                .order_by(
                    ProjectMemberSnapshot.is_leader.desc(),
                    ProjectMemberSnapshot.created_at,
                    ProjectMemberSnapshot.id,
                )
            )
        ).scalars()
    )
    agent_index = {agent_id: index for index, agent_id in enumerate(template_agent_ids)}
    bindings_by_id = {binding.id: binding for binding in await _skill_bindings(db, project)}
    root = project_root or project_repo_path(project.tenant_id, project.id)
    packages: list[dict] = []
    for binding_id in included_binding_ids:
        item = by_id[binding_id]
        member_agent_id = uuid.UUID(item["member_agent_id"])
        if member_agent_id not in agent_index:
            raise ProjectTemplateSnapshotError("Selected project Skill does not belong to a packaged digital employee")
        binding = bindings_by_id[binding_id]
        metadata = _binding_asset_metadata(binding)
        if metadata is None:
            raise ProjectTemplateSnapshotError("Selected project Skill metadata is unavailable")
        files = await asyncio.to_thread(_read_binding_skill, root, binding, metadata)
        packages.append(
            {
                "schema_version": 1,
                "name": item["name"],
                "version": item["version"],
                "folder_name": PurePosixPath(item["path"]).name,
                "digital_employee_index": agent_index[member_agent_id],
                "is_enabled": item["is_enabled"],
                "scope": {},
                "sha256": item["sha256"],
                "file_count": item["file_count"],
                "size_bytes": item["size_bytes"],
                "files": [_package_file(file) for file in files],
            }
        )
    return packages


async def instantiate_project_skills_from_template(
    db: AsyncSession,
    project: Project,
    owner_display_name: str,
    owner_user_id: uuid.UUID,
    raw_skill_assets: object,
    created_agent_ids: list[uuid.UUID],
) -> list[ProjectCapabilityBinding]:
    """Restore packaged files and fresh project-local bindings without source Skill rows."""

    packages = _validate_packages(raw_skill_assets, len(created_agent_ids))
    if not packages:
        return []
    root = project_repo_path(project.tenant_id, project.id)
    created_paths: list[Path] = []
    changed_paths: list[str] = []
    bindings: list[ProjectCapabilityBinding] = []
    restored_assets: dict[tuple[str, str, str, str], tuple[Path, str]] = {}
    try:
        for package in packages:
            agent_id = created_agent_ids[package["digital_employee_index"]]
            relative_path = f"skills/{package['folder_name']}"
            identity = (
                package["name"],
                package["version"],
                package["folder_name"],
                package["sha256"],
            )
            reusable = restored_assets.get(identity)
            asset_id = reusable[1] if reusable is not None else str(uuid.uuid4())
            target_relative = (
                relative_path
                if package["is_enabled"]
                else _disabled_asset_path({"asset_id": asset_id, "path": relative_path})
            )
            target = resolve_project_agent_path(root, agent_id, target_relative)
            if target.exists():
                raise ProjectTemplateSnapshotError(f"Project Skill path already exists: {target_relative}")
            staging = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restoring")
            try:
                if reusable is None:
                    await asyncio.to_thread(_write_files, staging, package["decoded_files"])
                else:
                    await asyncio.to_thread(_link_files, reusable[0], staging, package["decoded_files"])
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, target)
            finally:
                await asyncio.to_thread(shutil.rmtree, staging, True)
            created_paths.append(target)
            changed_paths.append(f".agents/{agent_id}/{target_relative}")
            restored_assets.setdefault(identity, (target, asset_id))
            metadata = _asset_metadata(
                package["decoded_files"],
                path=relative_path,
                version=package["version"],
                source="template",
                source_agent_id=None,
                asset_id=asset_id,
            )
            binding = ProjectCapabilityBinding(
                tenant_id=project.tenant_id,
                project_id=project.id,
                capability_type="skill",
                capability_id=None,
                capability_name=package["name"],
                source="inherited",
                inherited_from_agent_id=agent_id,
                is_enabled=package["is_enabled"],
                scope=package["scope"],
                config={_ASSET_CONFIG_KEY: metadata},
            )
            db.add(binding)
            bindings.append(binding)
        await db.flush()
        await commit_project_changes(
            project,
            f"Restore {len(bindings)} project Skills from template",
            changed_paths,
            author_name=owner_display_name,
            author_email=project_user_git_email(owner_user_id),
        )
        return bindings
    except Exception:
        for path in created_paths:
            await asyncio.to_thread(shutil.rmtree, path, True)
        raise


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
